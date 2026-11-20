"""Candidate archive: best-feasible selection, Pareto front, overwrite guard.

The archive answers "what is the best candidate we have" and refuses to replace
a feasible best with a candidate that violates a hard constraint — the failure
mode where an optimisation loop reports a nicer-looking but unusable design.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BEST_FILE = Path("archive") / "best.json"


class ArchiveError(RuntimeError):
    pass


def default_objectives(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Primary objective: declared in the spec, else realised gain (maximize)."""
    declared = spec.get("objectives")
    if isinstance(declared, list) and declared:
        clean = [
            {"metric": str(item["metric"]), "direction": str(item["direction"])}
            for item in declared
            if isinstance(item, dict)
            and item.get("metric")
            and item.get("direction") in {"maximize", "minimize"}
        ]
        if clean:
            return clean
    for metric in spec.get("metrics", []):
        if metric.get("kind") == "realized_gain_h":
            return [{"metric": metric["id"], "direction": "maximize"}]
    return [{"metric": spec["metrics"][0]["id"], "direction": "maximize"}]


def hard_metric_ids(spec: dict[str, Any]) -> list[str]:
    return [m["id"] for m in spec.get("metrics", []) if m.get("hard", True)]


def is_feasible(metrics: dict[str, dict[str, Any]], spec: dict[str, Any]) -> bool:
    """Feasible means every hard metric has an explicit passing value."""
    for metric_id in hard_metric_ids(spec):
        row = metrics.get(metric_id)
        if not isinstance(row, dict):
            return False
        if row.get("passed") not in (1, True):
            return False
    return True


def _entry_values(entry: dict[str, Any], objectives: list[dict[str, str]]) -> dict[str, float | None]:
    metrics = entry.get("metrics") or {}
    values: dict[str, float | None] = {}
    for objective in objectives:
        row = metrics.get(objective["metric"])
        value = None
        if isinstance(row, dict):
            value = row.get("value")
        elif isinstance(row, (int, float)):
            value = row
        values[objective["metric"]] = float(value) if isinstance(value, (int, float)) else None
    return values


def dominates(candidate: dict[str, Any], other: dict[str, Any],
              objectives: list[dict[str, str]]) -> bool:
    """True when candidate is at least as good in all objectives and better in one."""
    cand = _entry_values(candidate, objectives)
    base = _entry_values(other, objectives)
    better = False
    for objective in objectives:
        name = objective["metric"]
        a, b = cand.get(name), base.get(name)
        if a is None or b is None:
            return False
        if objective["direction"] == "maximize":
            if a < b:
                return False
            better = better or a > b
        else:
            if a > b:
                return False
            better = better or a < b
    return better


def pareto_front(entries: list[dict[str, Any]], objectives: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Non-dominated feasible entries (infeasible candidates are excluded)."""
    pool = [e for e in entries if e.get("feasible")]
    front: list[dict[str, Any]] = []
    for candidate in pool:
        if any(
            dominates(other, candidate, objectives)
            for other in pool
            if other is not candidate
        ):
            continue
        front.append(candidate)
    return front


def rank(entries: list[dict[str, Any]], objectives: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Feasible entries first, ordered by the declared objectives."""
    feasible = [e for e in entries if e.get("feasible")]
    infeasible = [e for e in entries if not e.get("feasible")]

    def sort_key(entry: dict[str, Any]):
        key: list[float] = []
        values = _entry_values(entry, objectives)
        for objective in objectives:
            value = values.get(objective["metric"])
            if value is None:
                value = float("-inf") if objective["direction"] == "maximize" else float("inf")
            key.append(-value if objective["direction"] == "maximize" else value)
        return tuple(key)

    return sorted(feasible, key=sort_key) + infeasible


def read_best(campaign_dir: str | Path) -> dict[str, Any] | None:
    target = Path(campaign_dir) / BEST_FILE
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def commit_best(campaign_dir: str | Path, entry: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Record ``entry`` as the best candidate when it is allowed to replace it."""
    root = Path(campaign_dir)
    current = read_best(root)
    if not entry.get("feasible"):
        if current is not None and current.get("feasible"):
            return {
                "updated": False,
                "reason": "refused_infeasible_overwrite",
                "current_run_id": current.get("run_id"),
            }
        if current is not None:
            return {
                "updated": False,
                "reason": "kept_existing_infeasible_best",
                "current_run_id": current.get("run_id"),
            }
        # nothing feasible yet: still record the best-known infeasible attempt
        payload = {**entry, "feasible": False, "recorded_utc": datetime.now(timezone.utc).isoformat()}
        target = root / BEST_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"updated": True, "reason": "recorded_infeasible_only", "run_id": entry.get("run_id")}

    objectives = default_objectives(spec)
    payload = {**entry, "objectives": objectives,
               "recorded_utc": datetime.now(timezone.utc).isoformat()}
    if current is not None and current.get("feasible"):
        better = dominates(payload, current, objectives)
        if not better:
            return {
                "updated": False,
                "reason": "not_better_than_best",
                "current_run_id": current.get("run_id"),
            }
    target = root / BEST_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"updated": True, "reason": "new_best", "run_id": entry.get("run_id")}
