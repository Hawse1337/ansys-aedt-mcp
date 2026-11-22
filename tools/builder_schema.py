# -*- coding: utf-8 -*-
"""Form-friendly builder parameter schema helpers.

The approver GUI edits schemas as a table; this module is the single place that
validates a table and turns it into the strict JSON schema consumed by the guard.

It deliberately has no AEDT/guard dependencies so the GUI can be built as a
standalone exe.
"""
from __future__ import annotations

import ast
import math
import re
from pathlib import Path
from typing import Any, Mapping

PARAM_TYPES = ("number", "enum", "string")
RESERVED_NAMES = {"port", "project", "outdir"}
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SKIP_RUN_ARGS = {"self", "port", "project", "outdir", "aedt_process_id", "save"}
# ``mc_steps`` is accepted by the p0 script's Python API as an iterable, but the
# current guard parameter types cannot represent it safely; do not suggest it.
_SKIP_DETECT = {"mc_steps"}


class _Missing:
    pass


_MISSING = _Missing()


def summarize_spec(spec: Mapping[str, Any]) -> str:
    kind = spec.get("type")
    if kind == "number":
        low = spec.get("min")
        high = spec.get("max")
        if low is None and high is None:
            return "任意数值"
        if low is None:
            return "≤ %s" % high
        if high is None:
            return "≥ %s" % low
        return "%s – %s" % (low, high)
    if kind == "enum":
        return " / ".join(str(v) for v in (spec.get("values") or []))
    if kind == "string":
        return "任意文本"
    return ""


def validate_schema(params: Any) -> list[str]:
    """Return human-readable errors; an empty list means valid."""
    errors: list[str] = []
    if not isinstance(params, dict):
        return ["参数 schema 必须是对象"]
    for name, spec in params.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append("参数名 %r 不合法（只能使用字母、数字、下划线）" % (name,))
            continue
        if name in RESERVED_NAMES:
            errors.append("参数名 %s 是系统保留名（port/project/outdir）" % name)
            continue
        if not isinstance(spec, dict):
            errors.append("参数 %s 的配置必须是对象" % name)
            continue
        kind = spec.get("type")
        if kind not in PARAM_TYPES:
            errors.append("参数 %s 的类型必须是 number / enum / string" % name)
            continue
        if "required" in spec and not isinstance(spec["required"], bool):
            errors.append("参数 %s 的“必填”必须是 true/false" % name)
        if kind == "number":
            low, high = spec.get("min"), spec.get("max")
            for label, value in (("最小值", low), ("最大值", high)):
                if value is not None and not _finite_number(value):
                    errors.append("参数 %s 的%s必须是有限数字" % (name, label))
            if _finite_number(low) and _finite_number(high) and float(low) > float(high):
                errors.append("参数 %s 的最小值不能大于最大值" % name)
        elif kind == "enum":
            values = spec.get("values")
            if not isinstance(values, list) or not values:
                errors.append("参数 %s 的可选值不能为空" % name)
            elif len({_value_key(v) for v in values}) != len(values):
                errors.append("参数 %s 的可选值有重复" % name)
            elif any(
                isinstance(v, (dict, list)) or v is None or not isinstance(v, (str, int, float, bool))
                for v in values
            ):
                errors.append("参数 %s 的可选值必须是文本/数字/布尔值" % name)
    return errors


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _value_key(value: Any) -> tuple[type, Any]:
    return (type(value), value)


def _infer_type(annotation: Any) -> str | None:
    if annotation is None:
        return None
    text = ast.unparse(annotation).lower()
    if "str" in text:
        return "string"
    if "float" in text or "int" in text:
        return "number"
    return None


def _extract_enum_values(tree: ast.AST) -> dict[str, list[str]]:
    """Best-effort extraction of enum-like values from comparisons/argparse."""
    found: dict[str, list[str]] = {}

    def add(name: str, values: list[str]) -> None:
        bucket = found.setdefault(name, [])
        for value in values:
            if value not in bucket:
                bucket.append(value)

    for node in ast.walk(tree):
        # family.upper() == "APEX" / feed == "gap" / feed in ("a", "b")
        if isinstance(node, ast.Compare):
            left = node.left
            base = None
            if isinstance(left, ast.Name):
                base = left.id
            elif isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute):
                if isinstance(left.func.value, ast.Name):
                    base = left.func.value.id
            if base:
                values: list[str] = []
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        values.append(comparator.value)
                    elif isinstance(comparator, (ast.Tuple, ast.List)):
                        values.extend(
                            item.value for item in comparator.elts
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        )
                if values:
                    add(base, values)

        # argparse add_argument("--feed", choices=[...])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            options = [
                arg.value for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                and arg.value.startswith("--")
            ]
            if not options:
                continue
            dest = options[0].lstrip("-").replace("-", "_")
            for keyword in node.keywords:
                if keyword.arg == "choices" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    values = [
                        item.value for item in keyword.value.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    ]
                    if values:
                        add(dest, values)
    return found


def detect_candidate_schema(
    source: str | Path,
    current: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Infer a suggested params schema from a candidate builder script."""
    path = Path(source)
    text = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(text)
    run_fn = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"),
        None,
    )
    if run_fn is None:
        return {}
    enums = _extract_enum_values(tree)
    args = list(run_fn.args.posonlyargs) + list(run_fn.args.args) + list(run_fn.args.kwonlyargs)
    defaults: dict[str, Any] = {}
    positional = list(run_fn.args.posonlyargs) + list(run_fn.args.args)
    default_count = len(run_fn.args.defaults)
    if default_count:
        for arg, default in zip(positional[-default_count:], run_fn.args.defaults):
            defaults[arg.arg] = _const_value(default)
    for arg, default in zip(run_fn.args.kwonlyargs, run_fn.args.kw_defaults):
        defaults[arg.arg] = _const_value(default)

    current = current or {}
    schema: dict[str, dict[str, Any]] = {}
    for arg in args:
        name = arg.arg
        if name in _SKIP_RUN_ARGS or name in _SKIP_DETECT:
            continue
        if isinstance(current.get(name), dict):
            # Preserve the operator's existing type/range/required choices; only
            # merge newly detected enum values (e.g. family/feed).
            spec = dict(current[name])
            detected_values = enums.get(name)
            if spec.get("type") == "enum" and detected_values:
                values = list(spec.get("values") or [])
                for value in detected_values:
                    if value not in values:
                        values.append(value)
                spec["values"] = values
            schema[name] = spec
            continue
        if name == "design":
            schema[name] = {"type": "string", "required": True}
            continue
        enum_values = enums.get(name)
        if enum_values:
            schema[name] = {"type": "enum", "values": enum_values}
            continue
        default = defaults.get(name, _MISSING)
        kind = _infer_type(arg.annotation)
        if kind is None:
            kind = "number" if (_numeric_default(default) or _numeric_name(name)) else "string"
        schema[name] = {"type": kind, "required": default is _MISSING}
    return schema


def _numeric_default(value: Any) -> bool:
    return type(value) in (int, float) and not isinstance(value, bool)


def _numeric_name(name: str) -> bool:
    return name in {"lens_steps", "curve_k"} or name.endswith(("_mm", "_deg", "_scale"))


def _const_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _const_value(node.operand)
        return -inner if isinstance(inner, (int, float)) else _MISSING
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_const_value(item) for item in node.elts)
    return _MISSING


def detect_suggestions(
    current: Mapping[str, Any],
    detected: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare a detected schema with the current one."""
    new: dict[str, dict[str, Any]] = {}
    enum_additions: dict[str, list[str]] = {}
    type_conflicts: dict[str, tuple[str, str]] = {}
    for name, spec in detected.items():
        if name not in current:
            new[name] = dict(spec)
            continue
        old = current.get(name) or {}
        if old.get("type") != spec.get("type"):
            type_conflicts[name] = (str(old.get("type", "?")), str(spec.get("type", "?")))
            continue
        if spec.get("type") == "enum" and old.get("type") == "enum":
            old_values = list(old.get("values") or [])
            additions = [v for v in (spec.get("values") or []) if v not in old_values]
            if additions:
                enum_additions[name] = additions
    return {"new": new, "enum_additions": enum_additions, "type_conflicts": type_conflicts}


def apply_suggestions(
    current: Mapping[str, Any],
    detected: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return a merged schema from current + detected suggestions."""
    merged = {name: dict(spec) for name, spec in current.items()}
    for name, spec in detected.items():
        if name not in merged:
            merged[name] = dict(spec)
            continue
        if merged[name].get("type") == "enum" and spec.get("type") == "enum":
            values = list(merged[name].get("values") or [])
            for value in spec.get("values") or []:
                if value not in values:
                    values.append(value)
            merged[name]["values"] = values
    return merged
