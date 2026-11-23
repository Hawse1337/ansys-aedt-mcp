# -*- coding: utf-8 -*-
"""Conservative low-risk auto-approval policy for the approver GUI.

Manual approval stays the default. When the operator enables the GUI switch,
only candidates that pass every check below are applied automatically:

* candidate hash still matches the request;
* the file compiles;
* imports are unchanged;
* the top-level function/class set is unchanged;
* no dangerous call/attribute (network, subprocess, eval/exec, file mutation);
* diff is small;
* the inferred parameter schema is valid.

Everything else stays pending for human review. Auto-approval is still a
same-user integrity control, not an OS sandbox: the operator opts in explicitly.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
from pathlib import Path
from typing import Any

from approval_apply import ApprovalApplyError, apply_request, load_allowlist, merge_schema
from approval_inbox import list_pending

_DANGEROUS_NAMES = {
    "eval", "exec", "compile", "__import__", "open",
    "system", "popen", "spawn", "spawnl", "spawnv", "kill", "terminate",
    "remove", "unlink", "rmdir", "removedirs", "rename", "replace",
    "write_text", "write_bytes", "mkdir", "makedirs", "chmod", "chown",
}
_DANGEROUS_ATTR_PREFIXES = (
    "subprocess.",
    "socket.",
    "urllib.",
    "requests.",
    "httpx.",
    "ctypes.",
    "shutil.",
    "os.",
    "sys.modules",
)
_MAX_DIFF_LINES = 300


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _dangerous_usage(tree: ast.AST) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _DANGEROUS_NAMES:
                findings.append("call %s()" % name)
            if isinstance(node.func, ast.Attribute):
                chain = _attr_chain(node.func)
                if any(chain.startswith(prefix) for prefix in _DANGEROUS_ATTR_PREFIXES):
                    findings.append("attribute %s" % chain)
        elif isinstance(node, ast.Attribute):
            chain = _attr_chain(node)
            if any(chain.startswith(prefix) for prefix in _DANGEROUS_ATTR_PREFIXES):
                findings.append("attribute %s" % chain)
    return sorted(set(findings))


def _attr_chain(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * int(node.level or 0) + (node.module or "")
            names.update(module + "." + alias.name for alias in node.names)
    return names


def _top_level_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    return functions, classes


def _diff_line_count(before: str, after: str) -> int:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
    return sum(1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))


def low_risk(request: dict[str, Any], entry: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (safe, reasons). An empty reason list means safe."""
    reasons: list[str] = []
    candidate = Path(str(request.get("candidate") or ""))
    if not candidate.is_file():
        return False, ["candidate file missing"]
    expected = str(request.get("candidate_sha256") or "").lower()
    content = candidate.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        return False, ["candidate changed after request"]
    source = content.decode("utf-8-sig", errors="replace")
    current_path = Path(str(entry.get("script") or ""))
    if not current_path.is_file():
        return False, ["approved script missing"]
    current_content = current_path.read_bytes()
    if hashlib.sha256(current_content).hexdigest() != str(entry.get("sha256") or "").lower():
        return False, ["approved script differs from the allowlist"]
    current = current_content.decode("utf-8-sig", errors="replace")
    try:
        compile(source, str(candidate), "exec")
    except SyntaxError as exc:
        return False, ["candidate does not compile: %s" % exc]
    try:
        tree = ast.parse(source)
        old_tree = ast.parse(current)
    except SyntaxError as exc:
        return False, ["AST parse failed: %s" % exc]
    old_imports = _imports(old_tree)
    new_imports = _imports(tree)
    added_imports = sorted(new_imports - old_imports)
    if added_imports:
        reasons.append("new imports: %s" % ", ".join(added_imports))
    old_funcs, old_classes = _top_level_names(old_tree)
    new_funcs, new_classes = _top_level_names(tree)
    if new_funcs != old_funcs:
        reasons.append("top-level function set changed")
    if new_classes != old_classes:
        reasons.append("top-level class set changed")
    dangerous = _dangerous_usage(tree)
    if dangerous:
        reasons.append("dangerous calls/attributes: %s" % ", ".join(dangerous[:8]))
    changed = _diff_line_count(current, source)
    if changed > _MAX_DIFF_LINES:
        reasons.append("diff too large: %d changed lines" % changed)
    merged, _changes, errors = merge_schema(entry, candidate, request)
    if errors:
        reasons.append("schema invalid: %s" % "; ".join(errors))
    return (not reasons), reasons


def auto_approve_pending(
    *,
    allowlist_path: str | Path,
    approved_dir: str | Path,
    decided_by: str = "auto-low-risk",
) -> dict[str, Any]:
    """Apply every currently pending low-risk request; return a report."""
    data = load_allowlist(allowlist_path)
    builders = data.get("builders") or {}
    approved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for request in list_pending():
        builder_id = str(request.get("builder_id") or "")
        entry = builders.get(builder_id)
        if not isinstance(entry, dict):
            skipped.append({"request_id": request.get("request_id"), "reasons": ["unknown builder"]})
            continue
        safe, reasons = low_risk(request, entry)
        if not safe:
            skipped.append({
                "request_id": request.get("request_id"),
                "builder_id": builder_id,
                "reasons": reasons,
            })
            continue
        try:
            result = apply_request(
                request,
                allowlist_path=allowlist_path,
                approved_dir=approved_dir,
                decided_by=decided_by,
                note="auto-approved low risk",
                auto_approved=True,
                expected_entry=entry,
            )
        except ApprovalApplyError as exc:
            skipped.append({
                "request_id": request.get("request_id"),
                "builder_id": builder_id,
                "reasons": [str(exc)],
            })
            continue
        approved.append(result)
    return {"approved": approved, "skipped": skipped}
