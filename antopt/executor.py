"""Executors: how a candidate becomes measured metrics.

Two implementations:

* ``SyntheticExecutor`` — analytic problem, used by the offline acceptance tests;
* ``GuardExecutor`` — the only real path: it never imports PyAEDT, it submits
  through the MCP guarded-run tools, so every measurement carries the guard's
  evidence chain (preflight, token, save/solution check, convergence gate).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from .core import Candidate, CaseSpec
from .optimize import Evaluation, evaluate_constraints

TERMINAL = {"COMPLETED", "FAILED", "BUDGET_EXCEEDED", "RECOVERY_REQUIRED"}


class Executor(Protocol):
    def evaluate(self, candidate: Candidate) -> Evaluation: ...


class SyntheticExecutor:
    def __init__(self, case: CaseSpec, problem: Any):
        self.case = case
        self.problem = problem

    def evaluate(self, candidate: Candidate) -> Evaluation:
        metrics = self.problem.metrics(candidate.params)
        feasible, violations, total = evaluate_constraints(metrics, self.case)
        return Evaluation(
            candidate_id=candidate.candidate_id,
            family_id=candidate.family_id,
            params=dict(candidate.params),
            metrics=metrics,
            feasible=feasible,
            violations=violations,
            total_violation=total,
            status="COMPLETED",
            origin=dict(candidate.origin),
        )


@dataclass
class GuardConfig:
    campaign_dir: str
    project_name: str
    design_name: str
    setup_name: str = "Setup1"
    cuts: Sequence[str] = ("theta_phi0", "phi_theta90")
    frequencies_ghz: Sequence[float] | None = None
    sweep_name: str | None = None
    pid: int | None = None
    port: int | None = None
    num_cores: int | None = None
    poll_interval_s: float = 5.0
    timeout_s: float = 1800.0
    prepare: Callable[[Candidate], Any] | None = None


class GuardExecutor:
    """Submits candidates through mcp_server's guarded-run tools."""

    def __init__(self, case: CaseSpec, config: GuardConfig, mcp_module: Any = None):
        self.case = case
        self.config = config
        if mcp_module is None:  # local import keeps antopt importable on its own
            import mcp_server as mcp_module  # noqa: PLC0415
        if config.pid is None and config.port is None:
            raise ValueError("GuardConfig needs an explicit AEDT pid or grpc port")
        self.mcp = mcp_module
        self._kind_by_id = {
            metric["id"]: metric["kind"] for metric in case.to_spec()["metrics"]
        }

    # ------------------------------------------------------------------ helpers
    def _metrics_by_kind(self, metrics: dict[str, Any]) -> dict[str, float | None]:
        kind_values: dict[str, float | None] = {}
        for metric_id, payload in (metrics or {}).items():
            kind = self._kind_by_id.get(metric_id, metric_id)
            value = payload.get("value") if isinstance(payload, dict) else payload
            kind_values[kind] = None if value is None else float(value)
        return kind_values

    def _wait(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + float(self.config.timeout_s) + 600.0
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = self.mcp.get_run_status(self.config.campaign_dir, run_id)
            if status.get("terminal"):
                return status
            time.sleep(max(0.01, float(self.config.poll_interval_s)))
        return status

    # ---------------------------------------------------------------- evaluate
    def evaluate(self, candidate: Candidate) -> Evaluation:
        if self.config.prepare is not None:
            self.config.prepare(candidate)
        submitted = self.mcp.submit_guarded_run(
            campaign_dir=self.config.campaign_dir,
            project_name=self.config.project_name,
            design_name=self.config.design_name,
            setup_name=self.config.setup_name,
            params=dict(candidate.params),
            cuts=list(self.config.cuts),
            frequencies_ghz=list(self.config.frequencies_ghz) if self.config.frequencies_ghz else None,
            sweep_name=self.config.sweep_name,
            pid=self.config.pid,
            port=self.config.port,
            num_cores=self.config.num_cores,
            poll_interval_s=self.config.poll_interval_s,
            timeout_s=self.config.timeout_s,
        )
        if submitted.get("cached") and submitted.get("status") == "COMPLETED":
            metrics = self._metrics_by_kind(
                {name: {"value": value} for name, value in (submitted.get("metrics") or {}).items()}
            )
            status = "COMPLETED"
            run_id = submitted.get("run_id")
        else:
            run_id = submitted.get("run_id")
            final = self._wait(run_id) if run_id else {}
            status = str(final.get("status") or "FAILED")
            metrics = self._metrics_by_kind(final.get("metrics") or {})
        feasible, violations, total = evaluate_constraints(metrics, self.case)
        return Evaluation(
            candidate_id=candidate.candidate_id,
            family_id=candidate.family_id,
            params=dict(candidate.params),
            metrics=metrics,
            feasible=feasible if status == "COMPLETED" else False,
            violations=violations,
            total_violation=total,
            run_id=run_id,
            status=status,
            origin=dict(candidate.origin),
        )


def run_campaign(
    case: CaseSpec,
    executor: Executor,
    *,
    budget: int,
    batch: int = 4,
    seed: int = 0,
    strategy: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Minimal loop: propose → evaluate → archive. Strategies plug in later."""
    from .core import latin_hypercube
    from .optimize import ParetoArchive

    archive = ParetoArchive(case.objectives)
    evaluated = 0
    round_index = 0
    history: list[dict[str, Any]] = []
    sampler = strategy or (lambda case, n, seed: latin_hypercube(case.family.param_space, n, seed=seed))
    while evaluated < budget:
        count = min(batch, budget - evaluated)
        params_list = sampler(case, count, seed + round_index)
        for params in params_list:
            candidate = case.make_candidate(params, algorithm="lhs", round=round_index)
            entry = executor.evaluate(candidate)
            archive.add(entry)
            evaluated += 1
        history.append(
            {
                "round": round_index,
                "evaluated": evaluated,
                "feasible": len(archive.feasible()),
                "front": len(archive.feasible_front()),
                "best_gain": (
                    archive.best_by("realized_gain_h", maximize=True).metrics.get("realized_gain_h")
                    if archive.best_by("realized_gain_h", maximize=True)
                    else None
                ),
            }
        )
        round_index += 1
    summary = archive.improvement_summary()
    summary.update({"case": case.case_id, "history": history, "archive": archive})
    return summary

