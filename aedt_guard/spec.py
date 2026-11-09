"""Design-specification management for the guarded antenna workflow.

The spec is the single authoritative source of design goals. It lives in a
JSON file, is versioned, and can only be changed through ``update_spec`` which
records the reason, the approver and a machine-readable diff.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "project",
    "design_type",
    "version",
    "frequency_ghz",
    "metrics",
    "evaluation",
    "execution_policy",
)

METRIC_KINDS = {
    "s11",
    "realized_gain_h",
    "peak_gain",
    "ripple",
    "sll",
    "beam_theta",
}

OPERATORS = {"<=", ">=", "=="}

DEFAULT_EVALUATION = {
    "theta_cut_phi_deg": [0, 90],
    "horiz_theta_deg": 90,
    "main_lobe_exclusion_deg": 10,
    "min_freq_points": 9,
    "realized_gain_mode": "gain_plus_mismatch",
}

DEFAULT_EXECUTION_POLICY = {
    "max_run_minutes": 60,
    "save_before_solve": False,
    "require_ff_sphere": True,
    "blocking_timeout_s": 3600,
    "require_convergence": True,
}


class SpecError(ValueError):
    """Raised when a specification is unusable."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """Return a list of problems; an empty list means the spec is usable."""
    problems: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    for key in REQUIRED_TOP_LEVEL:
        if key not in spec:
            problems.append(f"missing required field: {key}")
    if problems:
        return problems

    if spec["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(spec["project"], str) or not spec["project"].strip():
        problems.append("project must be a non-empty string")
    if not isinstance(spec["design_type"], str) or not spec["design_type"].strip():
        problems.append("design_type must be a non-empty string")
    if not isinstance(spec["version"], int) or spec["version"] < 1:
        problems.append("version must be a positive integer")

    band = spec["frequency_ghz"]
    if (
        not isinstance(band, (list, tuple))
        or len(band) != 2
        or not all(isinstance(v, (int, float)) for v in band)
        or band[0] >= band[1]
    ):
        problems.append("frequency_ghz must be [low, high] with low < high")

    metrics = spec["metrics"]
    if not isinstance(metrics, list) or not metrics:
        problems.append("metrics must be a non-empty list")
        return problems
    seen: set[str] = set()
    for index, metric in enumerate(metrics):
        where = f"metrics[{index}]"
        if not isinstance(metric, dict):
            problems.append(f"{where} must be an object")
            continue
        mid = metric.get("id")
        if not isinstance(mid, str) or not mid.strip():
            problems.append(f"{where}.id must be a non-empty string")
        elif mid in seen:
            problems.append(f"duplicate metric id: {mid}")
        else:
            seen.add(mid)
        kind = metric.get("kind")
        if kind not in METRIC_KINDS:
            problems.append(f"{where}.kind must be one of {sorted(METRIC_KINDS)}")
        if not isinstance(metric.get("unit"), str):
            problems.append(f"{where}.unit must be a string")
        if metric.get("operator") not in OPERATORS:
            problems.append(f"{where}.operator must be one of {sorted(OPERATORS)}")
        if not isinstance(metric.get("target"), (int, float)):
            problems.append(f"{where}.target must be a number")
        if not isinstance(metric.get("hard", True), bool):
            problems.append(f"{where}.hard must be a boolean")
        mband = metric.get("band_ghz")
        if mband is not None:
            if (
                not isinstance(mband, (list, tuple))
                or len(mband) != 2
                or not all(isinstance(v, (int, float)) for v in mband)
                or mband[0] >= mband[1]
            ):
                problems.append(f"{where}.band_ghz must be [low, high] with low < high")

    evaluation = spec["evaluation"]
    if not isinstance(evaluation, dict):
        problems.append("evaluation must be an object")
    else:
        for key, default in DEFAULT_EVALUATION.items():
            value = evaluation.get(key, default)
            if key == "theta_cut_phi_deg":
                if not isinstance(value, (list, tuple)) or not value:
                    problems.append("evaluation.theta_cut_phi_deg must be a non-empty list")
            elif key == "min_freq_points":
                if not isinstance(value, int) or value < 2:
                    problems.append("evaluation.min_freq_points must be an integer >= 2")
            elif key == "realized_gain_mode":
                if value not in {"gain_plus_mismatch", "reported"}:
                    problems.append(
                        "evaluation.realized_gain_mode must be 'gain_plus_mismatch' or 'reported'"
                    )
            elif not isinstance(value, (int, float)):
                problems.append(f"evaluation.{key} must be a number")

    policy = spec["execution_policy"]
    if not isinstance(policy, dict):
        problems.append("execution_policy must be an object")
    else:
        for key in ("max_run_minutes", "blocking_timeout_s"):
            value = policy.get(key, DEFAULT_EXECUTION_POLICY[key])
            if not isinstance(value, (int, float)) or value <= 0:
                problems.append(f"execution_policy.{key} must be a positive number")
        for key in ("save_before_solve", "require_ff_sphere", "require_convergence"):
            value = policy.get(key, DEFAULT_EXECUTION_POLICY[key])
            if not isinstance(value, bool):
                problems.append(f"execution_policy.{key} must be a boolean")

    objectives = spec.get("objectives")
    if objectives is not None:
        if not isinstance(objectives, list) or not objectives:
            problems.append("objectives must be a non-empty list when present")
        else:
            metric_ids = {
                m.get("id") for m in metrics if isinstance(m, dict) and m.get("id")
            }
            for index, objective in enumerate(objectives):
                where = f"objectives[{index}]"
                if not isinstance(objective, dict):
                    problems.append(f"{where} must be an object")
                    continue
                if objective.get("metric") not in metric_ids:
                    problems.append(f"{where}.metric must reference an existing metric id")
                if objective.get("direction") not in {"maximize", "minimize"}:
                    problems.append(f"{where}.direction must be maximize or minimize")
    return problems


def new_spec(
    project: str,
    design_type: str,
    frequency_ghz: Iterable[float],
    metrics: list[dict[str, Any]],
    evaluation: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
    objectives: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a version-1 spec and validate it before returning."""
    spec = {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "design_type": design_type,
        "version": 1,
        "frequency_ghz": [float(v) for v in frequency_ghz],
        "metrics": metrics,
        "evaluation": {**DEFAULT_EVALUATION, **(evaluation or {})},
        "execution_policy": {**DEFAULT_EXECUTION_POLICY, **(execution_policy or {})},
        "history": [],
    }
    if objectives:
        spec["objectives"] = objectives
    problems = validate_spec(spec)
    if problems:
        raise SpecError("; ".join(problems))
    return spec


def save_spec(spec: dict[str, Any], path: str | Path) -> Path:
    problems = validate_spec(spec)
    if problems:
        raise SpecError("; ".join(problems))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def load_spec(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise SpecError(f"spec not found: {target}")
    try:
        spec = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec is not valid JSON: {exc}") from exc
    problems = validate_spec(spec)
    if problems:
        raise SpecError("; ".join(problems))
    return spec


def _diff(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(old) | set(new)):
        if key == "history":
            continue
        path = f"{prefix}{key}"
        before, after = old.get(key, "<absent>"), new.get(key, "<absent>")
        if isinstance(before, dict) and isinstance(after, dict):
            changes.extend(_diff(before, after, prefix=f"{path}."))
        elif before != after:
            changes.append({"field": path, "before": before, "after": after})
    return changes


def update_spec(
    spec: dict[str, Any],
    changes: dict[str, Any],
    *,
    reason: str,
    approver: str,
) -> dict[str, Any]:
    """Return a new spec version with ``changes`` applied and an audit entry."""
    if not isinstance(reason, str) or not reason.strip():
        raise SpecError("reason is required to change a design specification")
    if not isinstance(approver, str) or not approver.strip():
        raise SpecError("approver is required to change a design specification")
    updated = json.loads(json.dumps(spec))
    for key, value in changes.items():
        if key in {"schema_version", "version", "history"}:
            raise SpecError(f"{key} cannot be changed through update_spec")
        updated[key] = value
    updated["version"] = int(spec.get("version", 0)) + 1
    entry = {
        "version": updated["version"],
        "previous_version": spec.get("version"),
        "changed_utc": _now(),
        "reason": reason.strip(),
        "approver": approver.strip(),
        "diff": _diff(spec, updated),
    }
    updated["history"] = list(spec.get("history", [])) + [entry]
    problems = validate_spec(updated)
    if problems:
        raise SpecError("; ".join(problems))
    return updated


def hard_metrics(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in spec.get("metrics", []) if m.get("hard", True)]


def soft_metrics(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in spec.get("metrics", []) if not m.get("hard", True)]


def task_context(spec: dict[str, Any], latest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact, machine-generated status summary for an agent prompt."""
    context: dict[str, Any] = {
        "project": spec["project"],
        "design_type": spec["design_type"],
        "spec_version": spec["version"],
        "frequency_ghz": spec["frequency_ghz"],
        "hard_constraints": [
            {"id": m["id"], "kind": m["kind"], "operator": m["operator"],
             "target": m["target"], "unit": m["unit"]}
            for m in hard_metrics(spec)
        ],
        "soft_objectives": [
            {"id": m["id"], "kind": m["kind"], "operator": m["operator"],
             "target": m["target"], "unit": m["unit"]}
            for m in soft_metrics(spec)
        ],
        "execution_policy": spec["execution_policy"],
    }
    if latest:
        context["latest_run"] = latest
        unmet = latest.get("unmet_constraints") or []
        status = str(latest.get("status") or "")
        context["unmet_constraints"] = unmet
        if status and status != "COMPLETED":
            context["next_step_hint"] = (
                f"latest run ended in {status}; inspect its stage_log, fix the cause, "
                "and resubmit before optimising further"
            )
        elif unmet:
            context["next_step_hint"] = (
                "satisfy the remaining hard constraints before optimising soft objectives"
            )
        else:
            context["next_step_hint"] = (
                "all hard constraints met; improve soft objectives while preserving them"
            )
    else:
        context["latest_run"] = None
        context["next_step_hint"] = "no evaluated run yet; submit a candidate through the guarded workflow"
    return context
