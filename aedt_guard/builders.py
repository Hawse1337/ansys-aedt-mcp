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


def validate_params(entry: Mapping[str, Any], params: Mapping[str, Any] | None) -> dict[str, Any]:
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
            if minimum is not None and value < minimum:
                raise BuilderError("parameter %r below minimum %s" % (name, minimum))
            if maximum is not None and value > maximum:
                raise BuilderError("parameter %r above maximum %s" % (name, maximum))
            cleaned[name] = float(value)
        elif kind == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or value not in values:
                raise BuilderError(
                    "parameter %r must be one of %s" % (name, values)
                )
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


def target_digest(entry: Mapping[str, Any], project_path: str, design: Any) -> str | None:
    """Check an optional operator-pinned source/design and return its content identity."""
    if "targets" not in entry:
        return None
    targets = entry["targets"]
    if not isinstance(targets, list) or not targets or not isinstance(design, str) or not design:
        raise BuilderError("a declared target requires a project and design")
    matches = []
    for item in targets:
        if not isinstance(item, dict) or not isinstance(item.get("project_path"), str):
            raise BuilderError("invalid builder target declaration")
        path = Path(item["project_path"]).expanduser()
        if not path.is_absolute() or not isinstance(item.get("design"), str) or not item["design"]:
            raise BuilderError("invalid builder target path or design")
        expected = item.get("project_sha256")
        if expected is not None and (not isinstance(expected, str) or len(expected) != 64
                or any(c not in "0123456789abcdefABCDEF" for c in expected)):
            raise BuilderError("invalid target project digest")
        if path.resolve() == Path(project_path) and item["design"] == design:
            matches.append(expected.lower() if expected else None)
    if not matches:
        raise BuilderError("project and design are outside the declared targets")
    actual = sha256_file(project_path)
    if None not in matches and actual not in matches:
        raise BuilderError("target project content differs from the declared digest")
    return actual
