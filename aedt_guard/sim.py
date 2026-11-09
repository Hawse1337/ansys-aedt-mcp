"""Guarded simulation state machine and orchestration.

The runner owns the run lifecycle and is the only place that may advance a run
to ``COMPLETED``. Metrics become visible only after ``VALIDATING`` finished, so
an agent can never read numbers from an unfinished or unsaved solve.

Stages: CREATED -> PREPARED -> SOLVING -> SOLVED -> EXPORTING -> VALIDATING
        -> COMPLETED (feasible or infeasible, both are valid completions)
Failures: FAILED (hard error, nothing to recover) or RECOVERY_REQUIRED (the
solver may still be running or the project was not saved; re-enter and recover).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

STAGES = (
    "CREATED",
    "PREPARED",
    "SOLVING",
    "SOLVED",
    "EXPORTING",
    "VALIDATING",
    "COMPLETED",
)
FAILED = "FAILED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
TERMINAL = {FAILED, RECOVERY_REQUIRED, "COMPLETED"}


class GuardError(RuntimeError):
    """Raised when the guarded workflow cannot continue."""


class Adapter(Protocol):
    """Everything the runner needs from the outside world (MCP/PyAEDT)."""

    def preflight(self) -> dict[str, Any]: ...

    def submit(self) -> dict[str, Any]: ...

    def wait(self, timeout_s: float, poll_interval_s: float) -> dict[str, Any]: ...

    def finalize(self) -> dict[str, Any]: ...

    def export_touchstone(self, path: Path) -> dict[str, Any]: ...

    def export_farfield(self, output_dir: Path, cuts: list[str]) -> dict[str, Any]: ...


def named_metrics(result: dict[str, Any], spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map evaluator output (keyed by metric kind) onto spec metric ids."""
    metrics = result.get("metrics") or {}
    named: dict[str, dict[str, Any]] = {}
    for metric in spec.get("metrics", []):
        kind = metric["kind"]
        payload = metrics.get(kind)
        if not isinstance(payload, dict):
            continue
        entry = dict(payload)
        entry["unit"] = metric.get("unit")
        if kind == "s11":
            entry["value"] = payload.get("worst_db")
        named[metric["id"]] = entry
    return named


def hard_results(result: dict[str, Any], spec: dict[str, Any]) -> dict[str, bool]:
    hard = result.get("hard") or {}
    return {metric["id"]: bool(hard.get(metric["id"], False)) for metric in spec.get("metrics", [])}


def last_stage(stage_log: list[dict[str, Any]] | None) -> str | None:
    """Return the most recent stage name from a persisted stage log."""
    stages = [entry.get("stage") for entry in (stage_log or []) if entry.get("stage")]
    return str(stages[-1]) if stages else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CampaignRunner:
    """Advance one run through the state machine and persist every transition."""

    def __init__(
        self,
        *,
        store: Any,
        adapter: Adapter,
        spec: dict[str, Any],
        run_id: str,
        artifact_dir: str | Path,
        params: dict[str, Any] | None = None,
        cuts: list[str] | None = None,
        timeout_s: float = 3600.0,
        poll_interval_s: float = 5.0,
        frequency_ghz: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        evaluator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.adapter = adapter
        self.spec = spec
        self.run_id = run_id
        self.artifact_dir = Path(artifact_dir)
        self.params = params or {}
        self.cuts = list(cuts or ["theta_phi0", "phi_theta90"])
        self.timeout_s = float(timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        self.frequency_ghz = frequency_ghz
        self.sleep = sleep
        self.evaluator = evaluator
        self.stage_log: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- helpers
    def _stage(self, stage: str, **detail: Any) -> None:
        self.stage_log.append({"stage": stage, "at": _now(), **detail})
        notes = json.dumps(self.stage_log, ensure_ascii=False)
        self.store.set_status(self.run_id, stage, notes=notes)

    def _fail(self, stage: str, error: Any) -> None:
        self._stage(stage, error=str(error)[:600])
        self.store.set_status(self.run_id, stage, notes=json.dumps(self.stage_log, ensure_ascii=False))

    # -------------------------------------------------------------------- run
    def run(self) -> str:
        try:
            pre = self.adapter.preflight()
        except Exception as exc:  # noqa: BLE001
            self._fail(FAILED, f"preflight failed: {exc}")
            return FAILED
        self._stage("PREPARED", **{k: pre.get(k) for k in ("validation_passed", "ff_sphere_present")})
        if not pre.get("validation_passed", True):
            self._fail(FAILED, "preflight validation failed")
            return FAILED

        try:
            submitted = self.adapter.submit()
        except Exception as exc:  # noqa: BLE001
            self._fail(FAILED, f"submit failed: {exc}")
            return FAILED
        self._stage("SOLVING", started=bool(submitted.get("started")))

        try:
            waited = self.adapter.wait(self.timeout_s, self.poll_interval_s)
        except Exception as exc:  # noqa: BLE001
            self._fail(RECOVERY_REQUIRED, f"wait failed: {exc}")
            return RECOVERY_REQUIRED
        if waited.get("timed_out"):
            self._fail(
                RECOVERY_REQUIRED,
                f"solver still running after {self.timeout_s:.0f}s; "
                "poll again or restart AEDT before re-submitting",
            )
            return RECOVERY_REQUIRED
        self._stage("SOLVED", poll_count=waited.get("poll_count"))
        return self._finish()

    def resume(self, last_stage: str | None = None) -> str:
        """Continue a run after a crash without re-solving.

        Called when the previous process died after the solve. Everything from
        the save/solution-data verification onwards is re-executed; the solve
        itself is never repeated here. If the solve evidence is missing, the
        finish path fails closed instead of pretending the run completed.
        """
        status = str(last_stage or "UNKNOWN")
        if status in TERMINAL:
            return status
        # Keep the pre-crash stage history instead of overwriting it, so the
        # final run log shows SOLVING/SOLVED once and the resumed stages after.
        try:
            detail = self.store.get_run(self.run_id) or {}
            prior = json.loads(detail.get("notes") or "[]")
        except Exception:  # noqa: BLE001
            prior = []
        if isinstance(prior, list) and prior:
            self.stage_log = prior
        return self._finish(resumed_from=status)

    def _finish(self, resumed_from: str | None = None) -> str:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        try:
            final = self.adapter.finalize()
        except Exception as exc:  # noqa: BLE001
            self._fail(RECOVERY_REQUIRED, f"finalize failed (project may be unsaved): {exc}")
            return RECOVERY_REQUIRED
        if not final.get("saved"):
            self._fail(RECOVERY_REQUIRED, "project was not saved after the solve")
            return RECOVERY_REQUIRED
        if final.get("solution_data_complete") is False:
            self._fail(FAILED, "solution data missing for: "
                       + ", ".join(final.get("incomplete_setups") or []))
            return FAILED

        extra = {"resumed_from": resumed_from} if resumed_from else {}
        self._stage("EXPORTING", **extra)
        artifacts: dict[str, Any] = {}
        if self.frequency_ghz is not None:
            artifacts["frequency_ghz"] = float(self.frequency_ghz)
        try:
            touch = self.adapter.export_touchstone(self.artifact_dir / "result.s1p")
            artifacts["s11"] = str(touch.get("path") or self.artifact_dir / "result.s1p")
            far = self.adapter.export_farfield(self.artifact_dir / "farfield", self.cuts)
            theta = {
                name: info.get("path")
                for name, info in (far.get("cuts") or {}).items()
                if str(name).startswith("theta")
            }
            phi = {
                name: info.get("path")
                for name, info in (far.get("cuts") or {}).items()
                if str(name).startswith("phi")
            }
            if theta:
                artifacts["theta_cuts"] = theta
            if phi:
                artifacts["phi_cut"] = next(iter(phi.values()))
        except Exception as exc:  # noqa: BLE001
            self._fail(FAILED, f"export failed: {exc}")
            return FAILED

        self._stage("VALIDATING")
        if self.evaluator is None:
            from .evaluator import evaluate as evaluator  # noqa: PLC0415
        else:
            evaluator = self.evaluator
        try:
            result = evaluator(artifacts, self.spec)
        except Exception as exc:  # noqa: BLE001
            self._fail(FAILED, f"evaluation failed: {exc}")
            return FAILED
        self.store.add_metrics(self.run_id, named_metrics(result, self.spec),
                               hard_results(result, self.spec))
        detail = {
            "feasible": bool(result.get("feasible")),
            "invalid": result.get("invalid"),
            "unmet": result.get("unmet"),
            "artifacts": artifacts,
        }
        self._stage("COMPLETED", **detail)
        return "COMPLETED"

