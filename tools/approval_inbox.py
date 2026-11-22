# -*- coding: utf-8 -*-
"""Read/decide guard-generated approval requests from the approver GUI."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
