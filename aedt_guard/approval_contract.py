# -*- coding: utf-8 -*-
"""Durable builder-approval requests.

The AI never needs to know the sidecar/request schema: it calls
``request_builder_approval`` with a candidate path and a reason.  The guard
computes the hashes, creates the request record, writes it for the approver GUI
and returns the request id/template.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .builders import load_allowlist
from .managed import state_dir

CONTRACT_VERSION = 2
REQUEST_TYPES = {"builder_change", "schema_only"}
STATUSES = {"pending", "approved", "denied", "expired", "cancelled"}


class ApprovalRequestError(ValueError):
    pass


def requests_root() -> Path:
    override = os.environ.get("AEDT_APPROVAL_REQUESTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return state_dir() / "approval_requests"


def pending_dir() -> Path:
    return requests_root() / "pending"


def archive_dir() -> Path:
    return requests_root() / "archive"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _get_builder_entry(builder_id: str) -> dict[str, Any]:
    if not isinstance(builder_id, str) or not builder_id.strip():
        raise ApprovalRequestError("builder_id is required")
    builders = load_allowlist().get("builders") or {}
    entry = builders.get(builder_id)
    if not isinstance(entry, dict):
        raise ApprovalRequestError("unknown builder: %s" % builder_id)
    old = str(entry.get("sha256") or "").lower()
    if len(old) != 64 or any(c not in "0123456789abcdef" for c in old):
        raise ApprovalRequestError("allowlist has no valid sha256 for %s" % builder_id)
    return entry


def _validate_candidate(candidate_path: str) -> Path:
    if not isinstance(candidate_path, str) or not candidate_path.strip():
        raise ApprovalRequestError("candidate_path is required")
    candidate = Path(candidate_path).expanduser().resolve()
    if candidate.suffix.lower() != ".py" or not candidate.is_file():
        raise ApprovalRequestError("candidate_path must be an existing .py file: %s" % candidate)
    return candidate


def build_request(
    builder_id: str,
    candidate_path: str,
    reason: str,
    *,
    request_type: str = "builder_change",
    change_summary: Iterable[str] | None = None,
    new_params: Iterable[dict[str, Any]] | None = None,
    new_enum_values: dict[str, list[Any]] | None = None,
    platform: str = "",
    session_id: str = "",
    session_title: str = "",
    tested: Iterable[str] | None = None,
    risk_notes: str = "",
) -> tuple[dict[str, Any], Path]:
    if request_type not in REQUEST_TYPES:
        raise ApprovalRequestError("request_type must be one of %s" % sorted(REQUEST_TYPES))
    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalRequestError("reason is required")
    entry = _get_builder_entry(builder_id)
    candidate = _validate_candidate(candidate_path)
    candidate_sha = _sha256(candidate)
    # Idempotency: repeated agent calls for the same pending candidate return
    # the existing request instead of flooding the operator inbox.
    for existing in list_requests():
        try:
            same_candidate = Path(str(existing.get("candidate") or "")).resolve() == candidate
        except OSError:
            same_candidate = False
        if (
            existing.get("builder_id") == builder_id
            and existing.get("request_type") == request_type
            and existing.get("candidate_sha256") == candidate_sha
            and same_candidate
        ):
            return existing, Path(str(existing["request_path"]))
    request_id = uuid.uuid4().hex
    record: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "request_id": request_id,
        "status": "pending",
        "builder_id": builder_id,
        "request_type": request_type,
        "candidate": str(candidate),
        "candidate_sha256": candidate_sha,
        "old_approved_sha256": str(entry["sha256"]).lower(),
        "platform": str(platform or ""),
        "session_id": str(session_id or ""),
        "session_title": str(session_title or ""),
        "requested_at": _now(),
        "reason": reason.strip(),
        "change_summary": [str(item) for item in (change_summary or [])],
        "new_params": [dict(item) for item in (new_params or []) if isinstance(item, dict)],
        "new_enum_values": {
            str(name): list(values)
            for name, values in (new_enum_values or {}).items()
            if isinstance(values, list)
        },
        "tested": [str(item) for item in (tested or [])],
        "risk_notes": str(risk_notes or ""),
        "operator_action": "review_candidate_and_schema",
        "created_by": "aedt_guard",
    }
    path = pending_dir() / (request_id + ".json")
    _atomic_json(path, record)
    # Best-effort compatibility sidecar next to the candidate. The primary
    # request record is the file under approval_requests/pending.
    try:
        sidecar = candidate.with_name(candidate.stem + ".approval-request.json")
        _atomic_json(sidecar, record)
    except OSError:
        pass
    return record, path


def list_requests(status: str = "pending") -> list[dict[str, Any]]:
    root = pending_dir() if status == "pending" else requests_root() / status
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("status") == status:
            data["request_path"] = str(path)
            result.append(data)
    return result


def get_request(request_id: str) -> tuple[dict[str, Any], Path] | None:
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    path = pending_dir() / (request_id + ".json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (data, path) if isinstance(data, dict) else None


def decide_request(
    request_id: str,
    status: str,
    *,
    decided_by: str = "",
    note: str = "",
) -> dict[str, Any]:
    if status not in {"approved", "denied", "expired", "cancelled"}:
        raise ApprovalRequestError("invalid decision status: %s" % status)
    found = get_request(request_id)
    if found is None:
        raise ApprovalRequestError("request not found: %s" % request_id)
    data, path = found
    data.update(
        {
            "status": status,
            "decided_at": _now(),
            "decided_by": str(decided_by or ""),
            "decision_note": str(note or ""),
        }
    )
    archive = archive_dir() / (request_id + ".json")
    _atomic_json(archive, data)
    path.unlink(missing_ok=True)
    return data
