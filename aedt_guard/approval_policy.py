# -*- coding: utf-8 -*-
"""Preflight for builder-approval requests.

An approval request is a contract change, not a debugging loop.  This module
runs cheap, deterministic checks before a request is written:

* the candidate exists, compiles, and has a callable ``run``;
* every ordinary ``run`` argument is already in the allowlist or is declared
  in the request's ``new_params`` / ``new_enum_values`` — this catches the
  "approved the hash but forgot the parameter table" failure before a human is
  asked to look at it;
* declared parameters have a valid guard schema shape;
* obvious dangerous imports/calls are flagged as high risk (visible to the
  operator; not silently approved).
"""
from __future__ import annotations

import ast
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PARAM_TYPES = {"number", "enum", "string"}
_NON_PARAM_ARGS = {
    "self", "port", "project", "outdir", "aedt_process_id", "save", "mc_steps",
}
_DANGEROUS_CALLS = {
    "eval", "exec", "compile", "__import__", "open",
    "system", "popen", "spawn", "kill", "terminate", "remove", "unlink",
    "rmdir", "removedirs", "rename", "replace", "write_text", "write_bytes",
}
_DANGEROUS_PREFIXES = (
    "subprocess.", "socket.", "urllib.", "requests.", "httpx.", "ctypes.",
    "shutil.", "os.", "sys.modules",
)


class PreflightFailure(ValueError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _attr_chain(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _run_arguments(tree: ast.AST) -> tuple[list[str], bool]:
    run = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"),
        None,
    )
    if run is None:
        return [], False
    args = [arg.arg for arg in list(run.args.posonlyargs) + list(run.args.args) + list(run.args.kwonlyargs)]
    return args, run.args.kwarg is not None


def _schema_names(entry: Mapping[str, Any], request: Mapping[str, Any]) -> set[str]:
    names = set((entry.get("params") or {}).keys())
    for spec in request.get("new_params") or []:
        if isinstance(spec, dict) and isinstance(spec.get("name"), str):
            names.add(spec["name"])
    names.update((request.get("new_enum_values") or {}).keys())
    return names


def _validate_declared_params(request: Mapping[str, Any], entry: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    declared: dict[str, Any] = {}
    for spec in request.get("new_params") or []:
        if not isinstance(spec, dict):
            errors.append("new_params contains a non-object entry")
            continue
        name = spec.get("name")
        kind = spec.get("type")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append("new parameter name %r is invalid" % (name,))
            continue
        if name in declared:
            errors.append("duplicate parameter declaration %r" % name)
        declared[name] = spec
        if "required" in spec and type(spec["required"]) is not bool:
            errors.append("parameter %r required must be a boolean" % name)
        if kind not in _PARAM_TYPES:
            errors.append("parameter %r has unsupported type %r" % (name, kind))
        if kind == "enum" and not (isinstance(spec.get("values"), list) and spec["values"]):
            errors.append("enum parameter %r has no values list" % name)
        if kind == "number":
            for bound in ("min", "max"):
                value = spec.get(bound)
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                    errors.append("number parameter %r has non-numeric %s" % (name, bound))
            low, high = spec.get("min"), spec.get("max")
            if type(low) in (int, float) and type(high) in (int, float) and low > high:
                errors.append("number parameter %r minimum exceeds maximum" % name)
    for name, values in (request.get("new_enum_values") or {}).items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append("new_enum_values key %r is invalid" % (name,))
        if not isinstance(values, list):
            errors.append("new_enum_values[%r] must be a list" % name)
        target = declared.get(name) or (entry.get("params") or {}).get(name) or {}
        if target.get("type") != "enum":
            errors.append("enum addition requires an existing or declared enum parameter %r" % name)
    return errors


def _risk(tree: ast.AST) -> tuple[str, list[str]]:
    findings: list[str] = []
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * int(node.level or 0) + (node.module or ""))
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _DANGEROUS_CALLS:
                findings.append("call %s()" % name)
            if isinstance(node.func, ast.Attribute):
                chain = _attr_chain(node.func)
                if any(chain.startswith(prefix) for prefix in _DANGEROUS_PREFIXES):
                    findings.append("attribute %s" % chain)
        elif isinstance(node, ast.Attribute):
            chain = _attr_chain(node)
            if any(chain.startswith(prefix) for prefix in _DANGEROUS_PREFIXES):
                findings.append("attribute %s" % chain)
    dangerous_imports = sorted(
        name for name in imports
        if name.split(".")[0] in {"subprocess", "socket", "urllib", "requests", "httpx", "ctypes", "shutil"}
    )
    findings.extend("import %s" % name for name in dangerous_imports)
    findings = sorted(set(findings))
    if dangerous_imports or any("subprocess" in item or "eval(" in item or "exec(" in item for item in findings):
        return "high", findings
    return ("medium" if findings else "low"), findings


def preflight_candidate(
    candidate_path: str | Path,
    *,
    entry: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a JSON-safe preflight report; raise ``PreflightFailure`` on hard errors."""
    candidate = Path(candidate_path).expanduser().resolve()
    checks: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []
    if not candidate.is_file() or candidate.suffix.lower() != ".py":
        raise PreflightFailure("candidate must be an existing .py file: %s" % candidate,
                               {"checks": {"exists": False}, "errors": ["candidate missing"]})
    checks["exists"] = True
    text = candidate.read_text(encoding="utf-8", errors="replace")
    try:
        compile(text, str(candidate), "exec")
        checks["compiles"] = True
    except SyntaxError as exc:
        raise PreflightFailure(
            "candidate does not compile: %s" % exc,
            {"checks": {"compiles": False}, "errors": ["%s line %s" % (exc.msg, exc.lineno)]},
        )
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PreflightFailure("candidate AST parse failed: %s" % exc,
                               {"checks": {"ast": False}, "errors": [str(exc)]})
    checks["ast"] = True
    run_args, has_kwargs = _run_arguments(tree)
    checks["has_run"] = bool(run_args) or has_kwargs
    if not checks["has_run"]:
        errors.append("candidate has no callable run(...) entrypoint")
    schema_names = _schema_names(entry, request)
    missing = [] if has_kwargs else [
        name for name in run_args if name not in _NON_PARAM_ARGS and name not in schema_names
    ]
    checks["schema_complete"] = not missing
    checks["missing_schema_names"] = missing
    if missing:
        errors.append(
            "run() arguments are not in allowlist/request: %s; add them to new_params "
            "in the same request" % ", ".join(missing)
        )
    declared_errors = _validate_declared_params(request, entry)
    checks["declared_params_valid"] = not declared_errors
    errors.extend(declared_errors)
    tested = [str(item) for item in (request.get("tested") or []) if str(item).strip()]
    checks["tested_evidence_present"] = bool(tested)
    if not tested:
        warnings.append("request has no tested evidence (py_compile/dry-run/end-to-end)")
    risk_level, risk_findings = _risk(tree)
    if risk_level == "high":
        warnings.append("high-risk static findings: %s" % ", ".join(risk_findings[:8]))
    checks["risk_level"] = risk_level
    checks["risk_findings"] = risk_findings
    report = {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "risk_level": risk_level,
        "risk_findings": risk_findings,
        "candidate": str(candidate),
    }
    if errors:
        raise PreflightFailure("builder preflight failed: " + "; ".join(errors), report)
    return report
