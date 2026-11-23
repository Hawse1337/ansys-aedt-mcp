# -*- coding: utf-8 -*-
"""Read/decide guard-generated approval requests from the approver GUI."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from guard_file_lock import file_lock, inbox_transaction
except ModuleNotFoundError:  # Running python tools/builder_approver_gui.py from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from guard_file_lock import file_lock, inbox_transaction


def requests_root() -> Path:
    override = os.environ.get("AEDT_APPROVAL_REQUESTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    override = os.environ.get("AEDT_MCP_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser() / "approval_requests"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ansys-aedt-mcp" / "approval_requests"


def pending_dir() -> Path:
    return requests_root() / "pending"


def archive_dir() -> Path:
    return requests_root() / "archive"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_pending() -> list[dict[str, Any]]:
    root = pending_dir()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        data = _read(path)
        if data and data.get("status") == "pending":
            data["request_path"] = str(path)
            try:
                data["_mtime"] = path.stat().st_mtime
            except OSError:
                data["_mtime"] = 0.0
            rows.append(data)
    rows.sort(key=lambda row: row.get("_mtime", 0.0), reverse=True)
    return rows


@inbox_transaction(requests_root)
def reconcile_requests(allowlist_path: str | Path | None = None) -> dict[str, list[str]]:
    """Retire requests that are already applied or replaced by a newer one.

    This is the GUI-side counterpart of the guard's reconciliation: it never
    changes an allowlist, it only moves stale pending files out of the inbox.
    """
    current: dict[str, dict] = {}
    try:
        if allowlist_path and Path(allowlist_path).is_file():
            data = json.loads(Path(allowlist_path).read_text(encoding="utf-8"))
            for builder_id, entry in (data.get("builders") or {}).items():
                if isinstance(entry, dict) and entry.get("sha256"):
                    current[str(builder_id)] = entry
    except (OSError, ValueError):
        current = {}
    archives: list[dict[str, Any]] = []
    if archive_dir().is_dir():
        for path in archive_dir().glob("*.json"):
            data = _read(path)
            if not data:
                continue
            data["request_path"] = str(path)
            try:
                data["_mtime"] = path.stat().st_mtime
            except OSError:
                data["_mtime"] = 0.0
            archives.append(data)
    applied: list[str] = []
    superseded: list[str] = []
    # Retire tombstones left by older versions in pending/.
    if pending_dir().is_dir():
        for path in pending_dir().glob("*.json"):
            data = _read(path)
            if data and data.get("status") == "superseded":
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
    for pending in list_pending():
        request_id = str(pending.get("request_id") or "")
        builder_id = str(pending.get("builder_id") or "")
        if not request_id:
            continue
        newer = [
            row for row in archives
            if row.get("builder_id") == builder_id
            and row.get("status") == "approved"
            and float(row.get("_mtime") or 0.0) > float(pending.get("_mtime") or 0.0)
        ]
        if newer:
            tomb = {
                "request_id": request_id,
                "builder_id": builder_id,
                "status": "superseded",
                "superseded_by": newer[0].get("request_id"),
                "superseded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "candidate": pending.get("candidate"),
                "candidate_sha256": pending.get("candidate_sha256"),
                "note": "a newer approved request for this builder exists",
            }
            _atomic_json(archive_dir() / (request_id + ".json"), {**pending, **tomb})
            try:
                Path(str(pending["request_path"])).unlink(missing_ok=True)
            except OSError:
                pass
            superseded.append(request_id)
            continue
        if (
            current.get(builder_id)
            and str(current[builder_id]["sha256"]).lower() == str(pending.get("candidate_sha256") or "").lower()
            and ((not pending.get("new_params") and not pending.get("new_enum_values"))
                 or (current[builder_id].get("approval") or {}).get("request_id") == request_id)
        ):
            record = {
                **pending,
                "status": "approved",
                "decided_by": "auto-reconcile",
                "decision_note": "allowlist already matches candidate",
                "decided_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            }
            _atomic_json(archive_dir() / (request_id + ".json"), record)
            try:
                Path(str(pending["request_path"])).unlink()
            except OSError:
                pass
            applied.append(request_id)
    return {"superseded": superseded, "already_applied": applied}


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


@inbox_transaction(requests_root)
def decide_request(
    request_id: str,
    status: str,
    *,
    decided_by: str = "",
    note: str = "",
) -> dict[str, Any]:
    if status not in {"approved", "denied", "expired", "cancelled"}:
        raise ValueError("invalid decision status: %s" % status)
    path = pending_dir() / (request_id + ".json")
    data = _read(path)
    if data is None:
        raise ValueError("request not found: %s" % request_id)
    if str(data.get("status") or "") != "pending":
        raise ValueError("request %s is already %s" % (request_id, data.get("status")))
    data.update(
        {
            "status": status,
            "decided_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "decided_by": str(decided_by or ""),
            "decision_note": str(note or ""),
        }
    )
    _atomic_json(archive_dir() / (request_id + ".json"), data)
    try:
        path.unlink()
    except OSError:
        pass
    return data
