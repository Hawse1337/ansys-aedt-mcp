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

from .approval_policy import PreflightFailure, preflight_candidate
from .builders import load_allowlist
from .managed import state_dir
from guard_file_lock import inbox_transaction

CONTRACT_VERSION = 2
REQUEST_TYPES = {"builder_change", "schema_only", "new_builder"}
STATUSES = {"pending", "approved", "denied", "expired", "cancelled", "superseded"}


class ApprovalRequestError(ValueError):
    pass


class ApprovalPreflightError(ApprovalRequestError):
    def __init__(self, message: str, preflight: dict[str, Any] | None = None):
        super().__init__(message)
        self.preflight = preflight or {}


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


def _get_builder_entry(builder_id: str, *, required: bool = True) -> dict[str, Any] | None:
    if not isinstance(builder_id, str) or not builder_id.strip():
        raise ApprovalRequestError("builder_id is required")
    builders = load_allowlist().get("builders") or {}
    entry = builders.get(builder_id)
    if not isinstance(entry, dict):
        if required:
            raise ApprovalRequestError(
                "unknown builder: %s (first registration needs request_type='new_builder')"
                % builder_id
            )
        return None
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


def _new_builder_metadata(output_roots: Iterable[str] | None, targets: Iterable[dict[str, Any]] | None,
                          entrypoint: str) -> dict[str, Any]:
    roots: list[str] = []
    for value in output_roots or []:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ApprovalRequestError("new builder output_roots must be absolute paths")
        roots.append(str(path.resolve()))
    if not roots:
        raise ApprovalRequestError("new builder needs at least one absolute output_roots entry")
    target_rows: list[dict[str, Any]] = []
    for value in targets or []:
        if not isinstance(value, dict):
            raise ApprovalRequestError("new builder targets must be objects")
        target_rows.append(dict(value))
    name = str(entrypoint or "run").strip()
    if not name.isidentifier():
        raise ApprovalRequestError("new builder entrypoint must be a Python identifier")
    return {"entrypoint": name, "output_roots": roots, "targets": target_rows}


def _sidecar_path(candidate: Path) -> Path:
    return candidate.with_name(candidate.stem + ".approval-request.json")


def _write_sidecar(candidate: str | Path, record: dict[str, Any]) -> None:
    """Best-effort sidecar; stale sidecars are worse than a missing one."""
    try:
        if not candidate:
            return
        _atomic_json(_sidecar_path(Path(candidate)), record)
    except (OSError, ValueError):
        pass


def _archive(data: dict[str, Any], status: str, **extra: Any) -> Path:
    payload = dict(data)
    payload.update({"status": status, "decided_at": _now()})
    payload.update(extra)
    path = archive_dir() / (str(payload["request_id"]) + ".json")
    _atomic_json(path, payload)
    return path


def _supersede_pending(builder_id: str, new_request_id: str) -> list[str]:
    """One active request per builder. Keep a tombstone the waiter can read."""
    superseded: list[str] = []
    for existing in list_requests("pending"):
        if existing.get("builder_id") != builder_id:
            continue
        old_id = str(existing.get("request_id") or "")
        if not old_id or old_id == new_request_id:
            continue
        tombstone = {
            "schema_version": existing.get("schema_version", CONTRACT_VERSION),
            "request_id": old_id,
            "builder_id": builder_id,
            "status": "superseded",
            "superseded_by": new_request_id,
            "superseded_at": _now(),
            "candidate": existing.get("candidate"),
            "candidate_sha256": existing.get("candidate_sha256"),
            "reason": existing.get("reason", ""),
            "note": "a newer request for this builder is now the active one",
        }
        _archive(existing, "superseded", superseded_by=new_request_id)
        try:
            Path(str(existing["request_path"])).unlink(missing_ok=True)
        except OSError:
            pass
        _write_sidecar(str(existing.get("candidate") or ""), tombstone)
        superseded.append(old_id)
    return superseded


def _already_applied_record(
    builder_id: str,
    candidate: Path,
    candidate_sha: str,
    entry: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    request_id = uuid.uuid4().hex
    record: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "request_id": request_id,
        "status": "approved",
        "already_approved": True,
        "builder_id": builder_id,
        "request_type": "builder_change",
        "candidate": str(candidate),
        "candidate_sha256": candidate_sha,
        "old_approved_sha256": str(entry["sha256"]).lower(),
        "approved_sha256": str(entry["sha256"]).lower(),
        "reason": "allowlist already matches this candidate; no approval was needed",
        "requested_at": _now(),
        "decided_at": _now(),
        "decided_by": "auto-reconcile",
        "decision_note": "allowlist already matches candidate and no schema additions were requested",
    }
    path = archive_dir() / (request_id + ".json")
    _atomic_json(path, record)
    _write_sidecar(candidate, record)
    return record, path


@inbox_transaction(requests_root)
def reconcile_requests() -> dict[str, list[str]]:
    return _reconcile()


def _reconcile() -> dict[str, list[str]]:
    """Retire stale requests without changing any allowlist or candidate.

    * A newer archived/approved request for the same builder supersedes an older
      pending request (this is what happened when 949a was replaced by 23ac).
    * A pending request whose candidate is already the approved hash and needs no
      schema additions is archived as ``approved`` (already applied).
    """
    result = {"superseded": [], "already_applied": []}
    # Older versions left superseded tombstones in pending/; retire them.
    try:
        for path in pending_dir().glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("status") == "superseded":
                path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        builders = load_allowlist().get("builders") or {}
    except Exception:  # noqa: BLE001 - reconciliation must never block the GUI
        builders = {}
    for pending in list_requests("pending"):
        builder_id = str(pending.get("builder_id") or "")
        request_id = str(pending.get("request_id") or "")
        entry = builders.get(builder_id) if isinstance(builders, dict) else None
        if not request_id or not isinstance(entry, dict):
            continue
        pending_mtime = 0.0
        try:
            pending_mtime = Path(str(pending.get("request_path") or "")).stat().st_mtime
        except OSError:
            pass
        # Newer approved request for the same builder -> old one is obsolete.
        newer = [
            row for row in list_requests("approved")
            if row.get("builder_id") == builder_id
            and str(row.get("decided_at") or row.get("requested_at") or "") > str(
                pending.get("requested_at") or ""
            )
        ]
        if not newer:
            try:
                if Path(str(pending.get("request_path") or "")).stat().st_mtime < max(
                    (
                        Path(str(row.get("request_path") or "")).stat().st_mtime
                        for row in list_requests("approved")
                        if row.get("builder_id") == builder_id
                    ),
                    default=0.0,
                ):
                    newer = [row for row in list_requests("approved") if row.get("builder_id") == builder_id]
            except OSError:
                pass
        if newer:
            tombstone = {
                "schema_version": pending.get("schema_version", CONTRACT_VERSION),
                "request_id": request_id,
                "builder_id": builder_id,
                "status": "superseded",
                "superseded_by": newer[0].get("request_id"),
                "superseded_at": _now(),
                "candidate": pending.get("candidate"),
                "candidate_sha256": pending.get("candidate_sha256"),
                "reason": pending.get("reason", ""),
                "note": "a newer approved request for this builder exists",
            }
            _archive(pending, "superseded", superseded_by=newer[0].get("request_id"))
            try:
                Path(str(pending["request_path"])).unlink(missing_ok=True)
            except OSError:
                pass
            _write_sidecar(str(pending.get("candidate") or ""), tombstone)
            result["superseded"].append(request_id)
            continue
        if (
            str(entry.get("sha256") or "").lower() == str(pending.get("candidate_sha256") or "").lower()
            and ((not pending.get("new_params") and not pending.get("new_enum_values"))
                 or (entry.get("approval") or {}).get("request_id") == request_id)
        ):
            _archive(pending, "approved", decided_by="auto-reconcile",
                     decision_note="allowlist already matches candidate")
            try:
                Path(str(pending["request_path"])).unlink(missing_ok=True)
            except OSError:
                pass
            _write_sidecar(str(pending.get("candidate") or ""), {
                **pending, "status": "approved",
                "decided_by": "auto-reconcile",
                "decision_note": "allowlist already matches candidate",
            })
            result["already_applied"].append(request_id)
    return result


def recent_request_count(builder_id: str, *, hours: float = 24.0) -> int:
    """Count this builder's requests created in the recent window."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(hours)))
    rows = list_requests("pending") + list_requests("approved")
    rows += list_requests("denied") + list_requests("superseded")
    count = 0
    for row in rows:
        if row.get("builder_id") != builder_id:
            continue
        try:
            created = datetime.fromisoformat(str(row.get("requested_at") or ""))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created >= cutoff:
            count += 1
    return count


def approval_churn(builder_id: str, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe recent approval traffic for one builder.

    This is informational by default. An operator can set
    ``approval_churn_limit`` (hard cap) and/or ``approval_churn_warn_after``
    (default 3) in the builder entry when a production builder is being used as
    a development branch.
    """
    entry = entry or {}
    used = recent_request_count(builder_id)
    try:
        warn_after = int(entry.get("approval_churn_warn_after") or 3)
    except (TypeError, ValueError):
        warn_after = 3
    try:
        limit = int(entry.get("approval_churn_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    level = "ok"
    if used >= warn_after:
        level = "warning"
    if limit > 0 and used >= limit:
        level = "blocked"
    message = ""
    if level != "ok":
        message = (
            "%s has %d approval request(s) in the last 24h; this looks like "
            "incremental builder debugging. Consolidate the remaining fixes or "
            "move the experiment to a dev builder before requesting again."
            % (builder_id, used)
        )
    return {
        "builder_id": builder_id,
        "requests_24h": used,
        "warn_after": warn_after,
        "limit": limit or None,
        "level": level,
        "message": message,
    }


def _enforce_budget(builder_id: str, entry: dict[str, Any]) -> None:
    try:
        budget = int(entry.get("approval_budget_per_day") or 0)
    except (TypeError, ValueError):
        budget = 0
    used = recent_request_count(builder_id)
    if budget > 0 and used >= budget:
        raise ApprovalRequestError(
            "approval budget exceeded for %s: %d requests in the last 24h (limit %d); "
            "consolidate the candidate and include all required parameters/test evidence "
            "in one request instead of submitting incremental fixes"
            % (builder_id, used, budget)
        )
    try:
        churn_limit = int(entry.get("approval_churn_limit") or 0)
    except (TypeError, ValueError):
        churn_limit = 0
    if churn_limit > 0 and used >= churn_limit:
        raise ApprovalRequestError(
            "approval churn limit reached for %s: %d requests in the last 24h "
            "(approval_churn_limit=%d). Consolidate the fix or use a dev builder; "
            "do not submit another incremental production-builder change."
            % (builder_id, used, churn_limit)
        )


@inbox_transaction(requests_root)
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
    output_roots: Iterable[str] | None = None,
    targets: Iterable[dict[str, Any]] | None = None,
    entrypoint: str = "run",
) -> tuple[dict[str, Any], Path]:
    if request_type not in REQUEST_TYPES:
        raise ApprovalRequestError("request_type must be one of %s" % sorted(REQUEST_TYPES))
    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalRequestError("reason is required")
    existing_entry = _get_builder_entry(builder_id, required=False)
    if request_type == "new_builder":
        if existing_entry is not None:
            raise ApprovalRequestError(
                "builder %r already exists; use request_type='builder_change'" % builder_id
            )
        entry: dict[str, Any] = {}
        new_builder = _new_builder_metadata(output_roots, targets, entrypoint)
    else:
        if existing_entry is None:
            raise ApprovalRequestError(
                "unknown builder: %s (use request_type='new_builder' for first registration)"
                % builder_id
            )
        entry = existing_entry
        new_builder = None
    candidate = _validate_candidate(candidate_path)
    candidate_sha = _sha256(candidate)
    preflight_request = {
        "tested": [str(item) for item in (tested or [])],
        "new_params": list(new_params or []),
        "new_enum_values": dict(new_enum_values or {}),
    }
    try:
        preflight = preflight_candidate(candidate, entry=entry, request=preflight_request)
    except PreflightFailure as exc:
        raise ApprovalPreflightError(str(exc), getattr(exc, "details", None)) from exc
    # Retire applied/superseded requests before deciding whether a new one is
    # needed. This is what prevents an operator from having to approve a code
    # change that is already active.
    _reconcile()
    # Idempotency: repeated agent calls for the same pending candidate return
    # the existing request instead of flooding the operator inbox.
    for existing in list_requests():
        try:
            same_candidate = Path(str(existing.get("candidate") or "")).resolve() == candidate
        except OSError:
            same_candidate = False
        old_hash = str((entry or {}).get("sha256") or "").lower()
        if (
            existing.get("builder_id") == builder_id
            and existing.get("request_type") == request_type
            and existing.get("candidate_sha256") == candidate_sha
            and existing.get("old_approved_sha256") == old_hash
            and (existing.get("new_params") or []) == preflight_request["new_params"]
            and (existing.get("new_enum_values") or {}) == preflight_request["new_enum_values"]
            and same_candidate
        ):
            return existing, Path(str(existing["request_path"]))
    if (
        entry is not None
        and str(entry.get("sha256") or "").lower() == candidate_sha
        and not preflight_request["new_params"]
        and not preflight_request["new_enum_values"]
    ):
        return _already_applied_record(builder_id, candidate, candidate_sha, entry)
    _enforce_budget(builder_id, entry or {})
    churn = approval_churn(builder_id, entry or {})
    # Include the request being created in the operator-facing count.
    churn["requests_24h"] = int(churn["requests_24h"]) + 1
    if churn["requests_24h"] >= int(churn["warn_after"]) and churn["level"] == "ok":
        churn["level"] = "warning"
        churn["message"] = (
            "%s has %d approval request(s) in the last 24h; this looks like "
            "incremental builder debugging. Consolidate the remaining fixes or "
            "move the experiment to a dev builder before requesting again."
            % (builder_id, churn["requests_24h"])
        )
    request_id = uuid.uuid4().hex
    superseded = _supersede_pending(builder_id, request_id)
    record: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "request_id": request_id,
        "status": "pending",
        "builder_id": builder_id,
        "request_type": request_type,
        "candidate": str(candidate),
        "candidate_sha256": candidate_sha,
        "old_approved_sha256": str((entry or {}).get("sha256") or "").lower(),
        "platform": str(platform or ""),
        "session_id": str(session_id or ""),
        "session_title": str(session_title or ""),
        "requested_at": _now(),
        "reason": reason.strip(),
        "change_summary": [str(item) for item in (change_summary or [])],
        **preflight_request,
        "risk_notes": str(risk_notes or ""),
        "operator_action": "review_candidate_and_schema",
        "created_by": "aedt_guard",
        "superseded_requests": superseded,
        "approval_churn": churn,
        **({"new_builder": new_builder} if new_builder is not None else {}),
        "preflight": preflight,
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
    root = pending_dir() if status == "pending" else archive_dir()
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
    for directory in (pending_dir(), archive_dir()):
        path = directory / (request_id + ".json")
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data, path
    return None


@inbox_transaction(requests_root)
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
    if str(data.get("status") or "") != "pending":
        raise ApprovalRequestError(
            "request %s is already %s; it cannot be decided again" % (request_id, data.get("status"))
        )
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
