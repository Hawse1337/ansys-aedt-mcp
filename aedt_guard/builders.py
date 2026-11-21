# -*- coding: utf-8 -*-
"""Operator-approved model builders.

The guard's default-deny ``run_python_code`` protects against unbounded code. Real design
campaigns need parameterized modelling code that the operator has reviewed once. This module
validates an operator-managed allowlist (script path + SHA-256 + typed parameters) so an agent can
invoke ``run_approved_builder`` repeatedly without per-run approval.

Security boundary: the allowlist is a same-user integrity control, not an OS sandbox. Keep it
outside agent-writable locations or protect it with OS ACLs when stronger isolation is required.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

GUARD_REPO = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = GUARD_REPO / "builders.local.json"


class BuilderError(ValueError):
    """Invalid/missing builder configuration or arguments."""


def allowlist_path() -> Path:
    configured = os.environ.get("AEDT_GUARD_BUILDERS", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_ALLOWLIST


def writable_by_current_user(path: str | Path) -> bool:
    """Best-effort Windows/POSIX writability check for the allowlist/script warning.

    This is a warning signal only: same-user processes can still modify the file unless OS ACLs
    or account isolation remove write access.
    """
    try:
        target = Path(path)
        return target.exists() and os.access(target, os.W_OK)
    except OSError:
        return False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_allowlist(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else allowlist_path()
    if not target.is_file():
        raise BuilderError(
            "no builder allowlist at %s; the operator must create it (see docs/BUILDERS.md)"
            % target
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise BuilderError("cannot parse builder allowlist %s: %r" % (target, exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("builders"), dict):
        raise BuilderError("builder allowlist must contain a 'builders' object")
    return data


def get_builder(allowlist: Mapping[str, Any], builder_id: str) -> dict[str, Any]:
    entry = allowlist["builders"].get(builder_id) if builder_id else None
    if not isinstance(entry, dict):
        raise BuilderError("unknown builder_id %r" % (builder_id,))
    script = entry.get("script")
    digest = entry.get("sha256")
    entrypoint = entry.get("entrypoint")
    if not isinstance(script, str) or not script:
        raise BuilderError("builder %r has no script" % (builder_id,))
    if not isinstance(digest, str) or len(digest) != 64:
        raise BuilderError("builder %r has no valid sha256" % (builder_id,))
    if (
        not isinstance(entrypoint, str)
        or not entrypoint.isidentifier()
        or entrypoint.startswith("__")
        or entrypoint.endswith("__")
    ):
        raise BuilderError("builder %r has no valid entrypoint" % (builder_id,))
    path = Path(script)
    if not path.is_absolute():
        raise BuilderError("builder %r script must be an absolute path" % (builder_id,))
    if not path.is_file():
        raise BuilderError("builder script not found: %s" % path)
    digest = digest.lower()
    actual = sha256_file(path)
    if actual != digest:
        raise BuilderError(
            "builder script hash changed (expected %s, actual %s); the operator must review "
            "and re-approve the new code" % (digest, actual)
        )
    return dict(entry)


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_params(entry: Mapping[str, Any], params: Mapping[str, Any] | None,
                    *, deviations: list[str] | None = None) -> dict[str, Any]:
    schema = entry.get("params") or {}
    if not isinstance(schema, dict):
        raise BuilderError("builder params schema must be an object")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise BuilderError("params must be an object")
    cleaned: dict[str, Any] = {}
    reserved = {"port", "project", "outdir"}
    for name, value in params.items():
        if name in reserved:
            raise BuilderError("parameter %r is reserved by the builder contract" % (name,))
        spec = schema.get(name)
        if not isinstance(spec, dict):
            raise BuilderError("parameter %r is not allowed for this builder" % (name,))
        kind = spec.get("type")
        if kind == "number":
            if not _is_number(value):
                raise BuilderError("parameter %r must be a finite number" % (name,))
            minimum, maximum = spec.get("min"), spec.get("max")
            if minimum is not None and not _is_number(minimum):
                raise BuilderError("parameter %r has an invalid minimum" % (name,))
            if maximum is not None and not _is_number(maximum):
                raise BuilderError("parameter %r has an invalid maximum" % (name,))
            if minimum is not None and maximum is not None and minimum > maximum:
                raise BuilderError("parameter %r has reversed bounds" % name)
            if minimum is not None and value < minimum or maximum is not None and value > maximum:
                reason = ("parameter %r below minimum %s" % (name, minimum) if minimum is not None and value < minimum
                          else "parameter %r above maximum %s" % (name, maximum))
                if deviations is None:
                    raise BuilderError(reason)
                deviations.append(reason)
            cleaned[name] = float(value)
        elif kind == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values or type(value) not in (int, float, str, bool):
                raise BuilderError("parameter %r requires a scalar enum value" % name)
            if not any(type(value) is type(item) for item in values):
                raise BuilderError("invalid enum type for %r" % name)
            if not any(type(value) is type(item) and value == item for item in values):
                reason = "parameter %r must be one of %s" % (name, values)
                if deviations is None:
                    raise BuilderError(reason)
                deviations.append(reason)
            cleaned[name] = value
        elif kind == "string":
            if not isinstance(value, str) or not value:
                raise BuilderError("parameter %r must be a non-empty string" % (name,))
            cleaned[name] = value
        else:
            raise BuilderError("unsupported schema type for %r" % (name,))
    for name, spec in schema.items():
        if isinstance(spec, dict) and spec.get("required") and name not in cleaned:
            raise BuilderError("missing required parameter %r" % (name,))
    return cleaned


def validate_output_dir(entry: Mapping[str, Any], outdir: str) -> str:
    if not isinstance(outdir, str) or not outdir.strip():
        raise BuilderError("outdir is required")
    resolved = Path(outdir).expanduser().resolve()
    roots = entry.get("output_roots") or []
    if not isinstance(roots, list) or not roots:
        raise BuilderError("builder has no output_roots configured")
    allowed = False
    for root in roots:
        try:
            candidate = Path(root).expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        if resolved == candidate or candidate in resolved.parents:
            allowed = True
            break
    if not allowed:
        raise BuilderError("outdir %s is outside the builder's allowed output roots" % resolved)
    return str(resolved)


def validate_project(project_path: str) -> str:
    if not isinstance(project_path, str) or not project_path.strip():
        raise BuilderError("project_path is required")
    resolved = Path(project_path).expanduser().resolve()
    if resolved.suffix.lower() != ".aedt" or not resolved.is_file():
        raise BuilderError("project_path must be an existing .aedt file: %s" % resolved)
    return str(resolved)


def request_id(request: dict[str, Any]) -> str:
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def prepare_request(builder_id, project_path, outdir, params, session):
    """Reconstruct the authorization subject from operator policy and local files."""
    from .managed import state_dir

    entry = get_builder(load_allowlist(), builder_id)
    targets = entry.get("targets")
    if not isinstance(targets, list) or not targets:
        raise BuilderError("operator must configure exact targets (project_path, project_sha256, design)")
    if not isinstance(params, Mapping) or not isinstance(params.get("design"), str) or not params["design"].strip():
        raise BuilderError("params.design must identify the target design")
    design = params["design"]
    scoped_params = dict(params)
    if "design" not in (entry.get("params") or {}):
        scoped_params.pop("design")
    deviations = []
    cleaned = validate_params(entry, scoped_params, deviations=deviations)
    cleaned["design"] = design
    source = validate_project(project_path)
    destination = validate_output_dir(entry, outdir)
    for protected in [state_dir().resolve(), approval_directory(), allowlist_path(), Path(entry["script"]).resolve()]:
        dest = Path(destination)
        if protected == dest or protected in dest.parents or dest in protected.parents:
            raise BuilderError("output directory overlaps guard policy or state")
    source_sha = sha256_file(source)
    matched = False
    for row in targets:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k]
                for k in ("project_path", "project_sha256", "design")):
            raise BuilderError("invalid target in operator allowlist")
        expected_path = Path(row["project_path"])
        digest = row["project_sha256"].lower()
        if not expected_path.is_absolute() or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BuilderError("invalid target path or SHA-256 in operator allowlist")
        if Path(source) == expected_path.resolve() and source_sha == digest and design == row["design"]:
            matched = True
    if not matched:
        deviations.append("project path, source contents or design is outside the approved target")
    subject = {"builder_id": builder_id, "script_sha256": entry["sha256"].lower(),
               "entrypoint": entry["entrypoint"], "policy_sha256": request_id(entry),
               "project_path": source, "project_sha256": source_sha, "design": design,
               "outdir": destination, "params": cleaned, "session": session}
    return entry, subject, deviations


def approval_directory() -> Path:
    policy = allowlist_path()
    return policy.with_name(policy.name + ".approvals")


def approval_required(builder_id, reason, request=None):
    response = {"builder_id": builder_id, "status": "approval_required", "approval_required": True,
                "executed": False, "refused": True, "reason": reason,
                "approval_mode": "once" if request is not None else "review_policy_or_request"}
    if request is not None:
        response.update(request=request, request_id=request_id(request))
    return response


def approved_once(request: dict[str, Any], *, consume: bool = False) -> bool:
    """Read operator-owned grants. Only the worker claims their unique receipt."""
    from .managed import state_dir

    digest = request_id(request)
    grant_file = approval_directory() / (digest + ".json")
    if not grant_file.is_file():
        return False
    try:
        grant = json.loads(grant_file.read_text(encoding="utf-8"))
        nonce = grant["nonce"]
        if (grant.get("request_id") != digest or grant.get("ledger_root") != str(state_dir().resolve())
                or not isinstance(nonce, str) or len(nonce) != 32
                or any(c not in "0123456789abcdef" for c in nonce)):
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "builder_approval_receipts.sqlite")) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS used (nonce TEXT PRIMARY KEY, request_id TEXT NOT NULL)")
        if consume:
            return conn.execute("INSERT OR IGNORE INTO used VALUES (?, ?)", (nonce, digest)).rowcount == 1
        return conn.execute("SELECT 1 FROM used WHERE nonce=?", (nonce,)).fetchone() is None
