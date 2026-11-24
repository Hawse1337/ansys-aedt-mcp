# -*- coding: utf-8 -*-
"""Atomic operator approval for builder candidates.

One approval action updates the approved script hash **and** any missing
parameter schema entries in the same allowlist write. This removes the old
"approved the hash but forgot the params" failure mode.

The module is intentionally GUI-side (no AEDT/guard imports) so the standalone
approver exe can use it, and the automatic low-risk path uses exactly the same
code as a manual click.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from approval_inbox import archive_dir, file_lock, inbox_transaction, pending_dir, requests_root
from builder_schema import apply_suggestions, detect_candidate_schema, validate_schema


class ApprovalApplyError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_allowlist(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalApplyError("cannot read allowlist: %s" % exc) from exc
    if not isinstance(data, dict) or not isinstance(data.get("builders"), dict):
        raise ApprovalApplyError("allowlist must contain a builders object")
    return data


def save_allowlist(path: str | Path, data: dict[str, Any]) -> None:
    target = Path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="builders-", suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def merge_schema(
    entry: dict[str, Any],
    candidate: Path,
    request: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return (merged params, added/changed summary, errors).

    Detection is deliberately additive: existing operator choices (type, range,
    required flag) win, and enum values are only appended. A type conflict is an
    error rather than a silent replacement.
    """
    current = entry.get("params") if isinstance(entry.get("params"), dict) else {}
    try:
        detected = detect_candidate_schema(candidate, current)
        merged = apply_suggestions(current, detected)
    except Exception as exc:  # noqa: BLE001 - a malformed candidate is a hard error
        return dict(current), [], ["schema detection failed: %s: %s" % (type(exc).__name__, exc)]
    changes: list[str] = []
    for name in detected:
        if name not in current:
            changes.append("+ %s" % name)
    # A request may explicitly declare a parameter the heuristic did not detect.
    for spec in (request or {}).get("new_params") or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        existing = merged.get(name)
        if name not in current:
            # An inferred type/default is only a suggestion. The operator's
            # explicit declaration supplies the bounds and allowed enum values.
            merged[name] = {key: value for key, value in spec.items() if key != "name"}
            if name not in detected:
                changes.append("+ %s (request)" % name)
        elif existing.get("type") != spec.get("type"):
            return merged, changes, ["type conflict for existing parameter %r" % name]
        elif existing.get("type") == "enum":
            values = list(existing.get("values") or [])
            for value in spec.get("values") or []:
                if value not in values:
                    values.append(value)
            existing["values"] = values
        else:
            # An inferred/heuristic entry may already exist. The operator's
            # explicit declaration still wins for bounds/default/required.
            changed = False
            for key in ("min", "max", "default", "required"):
                if key in spec and existing.get(key) != spec[key]:
                    existing[key] = spec[key]
                    changed = True
            if changed:
                changes.append("~ %s declaration" % name)
    for name, values in ((request or {}).get("new_enum_values") or {}).items():
        spec = merged.get(name)
        if not isinstance(spec, dict) or spec.get("type") != "enum":
            return merged, changes, ["enum addition for unknown/non-enum parameter %r" % name]
        current_values = list(spec.get("values") or [])
        for value in values or []:
            if value not in current_values:
                current_values.append(value)
                changes.append("~ %s += %r" % (name, value))
        spec["values"] = current_values
    errors = validate_schema(merged)
    return merged, changes, errors


def commit_entry(allowlist_path, builder_id, previous, updated):
    """Replace one entry after checking that the reviewed entry is still current."""
    path = Path(allowlist_path)
    with file_lock(path.with_name(path.name + ".lock")):
        data = load_allowlist(path)
        if data["builders"].get(builder_id) != previous:
            raise ApprovalApplyError("builder entry changed since review; refresh before approving")
        if path.is_file():
            backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            shutil.copy2(path, backup)
        data["builders"][builder_id] = updated
        save_allowlist(path, data)


def stage_script(candidate, approved_dir, builder_id, expected):
    """Publish reviewed bytes at an immutable name before switching the allowlist."""
    content = Path(candidate).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected:
        raise ApprovalApplyError("candidate changed since review")
    root = Path(approved_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / (builder_id + "." + digest + ".py")
    fd, temp = tempfile.mkstemp(prefix=".builder-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    return target


@inbox_transaction(requests_root)
def apply_request(
    request: dict[str, Any],
    *,
    allowlist_path: str | Path,
    approved_dir: str | Path,
    decided_by: str = "",
    note: str = "",
    auto_approved: bool = False,
    expected_entry: dict | None = None,
    expected_params: dict | None = None,
) -> dict[str, Any]:
    """Approve one request atomically; never called by the guard itself."""
    if str(request.get("status") or "") != "pending":
        raise ApprovalApplyError("request is not pending")
    request_id = str(request.get("request_id") or "")
    builder_id = str(request.get("builder_id") or "")
    candidate = Path(str(request.get("candidate") or "")).expanduser()
    if not request_id or not builder_id or not candidate.is_file():
        raise ApprovalApplyError("request has no valid candidate/builder")
    path = pending_dir() / (request_id + ".json")
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApprovalApplyError("request is no longer pending; refresh the inbox") from exc
    if pending.get("status") != "pending" or any(
        pending.get(key) != request.get(key) for key in ("request_id", "builder_id", "candidate",
            "candidate_sha256", "old_approved_sha256", "new_params", "new_enum_values")
    ):
        raise ApprovalApplyError("pending request has changed; refresh the inbox")
    request = pending
    expected_candidate = str(request.get("candidate_sha256") or "").lower()
    actual_candidate = _sha256(candidate)
    if expected_candidate != actual_candidate:
        raise ApprovalApplyError("candidate changed after the request was created")
    data = load_allowlist(allowlist_path)
    entry = data["builders"].get(builder_id)
    if str(request.get("request_type") or "") == "new_builder":
        if isinstance(entry, dict):
            raise ApprovalApplyError("builder %r is already registered" % builder_id)
        if expected_entry is not None:
            raise ApprovalApplyError("new-builder request must not have an existing entry")
        meta = request.get("new_builder")
        if not isinstance(meta, dict):
            raise ApprovalApplyError("new-builder request has no entry metadata")
        output_roots = meta.get("output_roots")
        if (not isinstance(output_roots, list) or not output_roots
                or any(not isinstance(item, str) or not item.strip() for item in output_roots)):
            raise ApprovalApplyError("new-builder request needs absolute output_roots")
        target = stage_script(candidate, approved_dir, builder_id, expected_candidate)
        merged, changes, errors = merge_schema({"params": {}}, target, request)
        if expected_params is not None and expected_params != merged:
            raise ApprovalApplyError("parameter schema changed since review")
        if errors:
            raise ApprovalApplyError("schema is not valid: %s" % "; ".join(errors))
        digest = expected_candidate
        approved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_entry = {
            "script": str(target.resolve()),
            "sha256": digest,
            "entrypoint": str(meta.get("entrypoint") or "run"),
            "params": merged,
            "output_roots": list(output_roots),
            "approval": {
                **dict(request),
                "candidate": str(candidate),
                "candidate_sha256": expected_candidate,
                "approved_sha256": digest,
                "previous_sha256": "",
                "approved_at": approved_at,
                "approved_by": str(decided_by or ""),
                "schema_added": changes,
                "auto_approved": bool(auto_approved),
            },
        }
        if meta.get("targets"):
            new_entry["targets"] = list(meta["targets"])
        commit_entry(allowlist_path, builder_id, None, new_entry)
        archived = dict(request)
        archived.update({
            "status": "approved",
            "decided_at": _now(),
            "decided_by": str(decided_by or ""),
            "decision_note": str(note or "registered new builder"),
            "approved_sha256": digest,
            "schema_added": changes,
            "auto_approved": bool(auto_approved),
        })
        _atomic_json(archive_dir() / (request_id + ".json"), archived)
        try:
            (pending_dir() / (request_id + ".json")).unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "approved": True,
            "request_id": request_id,
            "builder_id": builder_id,
            "sha256": digest,
            "approved_at": approved_at,
            "schema_added": changes,
            "new_builder": True,
        }
    if not isinstance(entry, dict):
        raise ApprovalApplyError("unknown builder %r" % builder_id)
    if str(entry.get("sha256") or "").lower() != str(request.get("old_approved_sha256") or "").lower():
        raise ApprovalApplyError("approved builder changed since this request")
    if expected_entry is not None and expected_entry != entry:
        raise ApprovalApplyError("builder entry changed since review")
    previous_entry = dict(entry)
    target = stage_script(candidate, approved_dir, builder_id, expected_candidate)
    merged, changes, errors = merge_schema(entry, target, request)
    if expected_params is not None and expected_params != merged:
        raise ApprovalApplyError("parameter schema changed since review")
    if errors:
        raise ApprovalApplyError("schema is not valid: %s" % "; ".join(errors))

    digest = expected_candidate
    approved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    previous = str(entry.get("sha256") or "").lower()
    entry["script"] = str(target.resolve())
    entry["sha256"] = digest
    entry["params"] = merged
    meta = dict(request)
    meta.update(
        {
            "candidate": str(candidate),
            "candidate_sha256": expected_candidate,
            "approved_sha256": digest,
            "previous_sha256": previous,
            "approved_at": approved_at,
            "approved_by": str(decided_by or ""),
            "schema_added": changes,
            "auto_approved": bool(auto_approved),
        }
    )
    entry["approval"] = meta
    commit_entry(allowlist_path, builder_id, previous_entry, entry)

    archived = dict(request)
    archived.update(
        {
            "status": "approved",
            "decided_at": _now(),
            "decided_by": str(decided_by or ""),
            "decision_note": str(note or ("auto-approved low risk" if auto_approved else "approved via GUI")),
            "approved_sha256": digest,
            "schema_added": changes,
            "auto_approved": bool(auto_approved),
        }
    )
    _atomic_json(archive_dir() / (request_id + ".json"), archived)
    try:
        (pending_dir() / (request_id + ".json")).unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "approved": True,
        "request_id": request_id,
        "builder_id": builder_id,
        "sha256": digest,
        "schema_added": changes,
        "approved_at": approved_at,
        "auto_approved": bool(auto_approved),
    }
