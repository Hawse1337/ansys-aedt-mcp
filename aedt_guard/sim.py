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

    def export_farfield(self, output_dir: Path, cuts: list[str],
                        frequencies: list[float] | None = None,
                        include_directivity: bool = False) -> dict[str, Any]: ...

    def export_convergence(self, path: Path) -> dict[str, Any]: ...

    def abort(self) -> dict[str, Any]:
        """Best-effort stop of the running solve (budget enforcement)."""


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


def _export_with_retry(export, *args: Any, attempts: int = 2,
                       delay_s: float = 5.0, on_retry: Any = None,
                       **kwargs: Any) -> Any:
    """Export immediately after a solve; AEDT occasionally refuses the first call.

    Report creation / file export can transiently fail when they arrive before
    AEDT has refreshed the solution context (observed as CreateReport /
    ExportToFile gRPC errors right after a solve). One bounded retry removes
    the flakiness without hiding a real export failure.
    """
    last_error: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            return export(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - retried once, then re-raised
            last_error = exc
            if attempt + 1 < max(1, int(attempts)):
                if on_retry is not None:
                    try:
                        on_retry()
                    except Exception:  # noqa: BLE001 - the retry still runs
                        pass
                time.sleep(delay_s)
    raise last_error  # type: ignore[misc]


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
        frequencies: list[float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        evaluator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        on_completed: Callable[[dict[str, Any]], None] | None = None,
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
        self.frequencies = list(frequencies) if frequencies else None
        self.sleep = sleep
        self.evaluator = evaluator
        self.on_completed = on_completed
        self.stage_log: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- helpers
    def _stage(self, stage: str, **detail: Any) -> None:
        self.stage_log.append({"stage": stage, "at": _now(), **detail})
        notes = json.dumps(self.stage_log, ensure_ascii=False)
        self.store.set_status(self.run_id, stage, notes=notes)

    def _abort_and_fail(self, message: str) -> None:
        """Best-effort stop of a running solve, then mark the run failed."""
        abort = getattr(self.adapter, "abort", None)
        detail: dict[str, Any] = {}
        if callable(abort):
            try:
                detail = dict(abort() or {})
            except Exception as exc:  # noqa: BLE001
                detail = {"abort_error": str(exc)[:200]}
        self._fail(FAILED, message, **detail)

    def _fail(self, stage: str, error: Any, **extra: Any) -> None:
        self._stage(stage, error=str(error)[:600], **extra)
        self.store.set_status(self.run_id, stage, notes=json.dumps(self.stage_log, ensure_ascii=False))

    # -------------------------------------------------------------------- run
    def run(self) -> str:
        policy = self.spec.get("execution_policy") or {}
        budget_s = None
        if policy.get("max_run_minutes"):
            budget_s = float(policy["max_run_minutes"]) * 60.0
        started = time.monotonic()

        def budget_exceeded() -> bool:
            return budget_s is not None and (time.monotonic() - started) >= budget_s

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
            waited = self.adapter.wait(
                min(self.timeout_s, budget_s) if budget_s is not None else self.timeout_s,
                self.poll_interval_s,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(RECOVERY_REQUIRED, f"wait failed: {exc}")
            return RECOVERY_REQUIRED
        if waited.get("timed_out"):
            if budget_exceeded():
                self._abort_and_fail(
                    f"BUDGET_EXCEEDED: run exceeded max_run_minutes="
                    f"{policy.get('max_run_minutes')} and was stopped"
                )
                return FAILED
            self._fail(
                RECOVERY_REQUIRED,
                f"solver still running after {self.timeout_s:.0f}s; "
                "poll again or restart AEDT before re-submitting",
            )
            return RECOVERY_REQUIRED
        if budget_exceeded():
            self._abort_and_fail(
                f"BUDGET_EXCEEDED: run exceeded max_run_minutes="
                f"{policy.get('max_run_minutes')} before post-processing"
            )
            return FAILED
        self._stage("SOLVED", poll_count=waited.get("poll_count"))
        extra = list(getattr(self.adapter, "extra_setups", None) or [])
        if extra:
            self._stage("SOLVING_EXTRA", setups=extra)
            for name in extra:
                try:
                    self.adapter.analyze_setup(name)
                    extra_wait = self.adapter.wait_setup(
                        name, self.timeout_s, self.poll_interval_s
                    )
                except Exception as exc:  # noqa: BLE001
                    self._fail(RECOVERY_REQUIRED,
                               f"extra setup {name!r} failed: {exc}")
                    return RECOVERY_REQUIRED
                if extra_wait.get("timed_out"):
                    self._fail(RECOVERY_REQUIRED,
                               f"extra setup {name!r} still solving after "
                               f"{self.timeout_s:.0f}s")
                    return RECOVERY_REQUIRED
            if budget_exceeded():
                self._abort_and_fail(
                    f"BUDGET_EXCEEDED: extra setups exceeded max_run_minutes="
                    f"{policy.get('max_run_minutes')}"
                )
                return FAILED
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

        policy = self.spec.get("execution_policy") or {}
        require_convergence = bool(policy.get("require_convergence", True))
        convergence_evidence: dict[str, Any] | None = None
        if require_convergence:
            exporter = getattr(self.adapter, "export_convergence", None)
            if callable(exporter):
                try:
                    convergence = exporter(self.artifact_dir / "convergence.conv")
                except Exception as exc:  # noqa: BLE001
                    self._fail(FAILED, f"convergence export failed (E_NOT_CONVERGED): {exc}")
                    return FAILED
                evidence = (
                    convergence.get("evidence")
                    or convergence.get("evidence_summary")
                    or {}
                )
                convergence_evidence = evidence
                assessment = str(evidence.get("assessment") or "").lower()
                native = evidence.get("native_converged")
                if assessment != "converged" and native is not True:
                    self._fail(
                        FAILED,
                        "adaptive convergence was not proven for this solve "
                        f"(assessment={assessment or 'missing'}, native={native}); "
                        "E_NOT_CONVERGED",
                    )
                    return FAILED
        artifacts: dict[str, Any] = {}
        if self.frequency_ghz is not None:
            artifacts["frequency_ghz"] = float(self.frequency_ghz)
        try:
            # Far fields first: exporting the touchstone changes the design's
            # active solution context, and a far-field report created right
            # after it can come back as an unresolved draft (observed as
            # 'could not resolve the report name').
            # opt-in: only pass the flag when the task asked for it, so adapters that do not
            # support the argument (all the fakes in the tests) keep working unchanged
            farfield_extra = {}
            if bool((self.spec.get("evaluation") or {}).get("include_directivity", False)):
                farfield_extra["include_directivity"] = True
            far = _export_with_retry(
                self.adapter.export_farfield,
                self.artifact_dir / "farfield", self.cuts, frequencies=self.frequencies,
                **farfield_extra,
                on_retry=getattr(self.adapter, "reset_connection", None),
            )
            touch = _export_with_retry(
                self.adapter.export_touchstone, self.artifact_dir / "result.s1p",
                on_retry=getattr(self.adapter, "reset_connection", None),
            )
            artifacts["s11"] = str(touch.get("path") or self.artifact_dir / "result.s1p")
            # A swept run must export a curve. A single-row touchstone used to
            # slip through and made every bandwidth metric silently read 0 GHz,
            # which looks like a design result instead of an export failure.
            details = touch.get("details") or {}
            rows = details.get("row_count")
            sweep_name = getattr(self.adapter, "sweep_name", None)
            expected = details.get("expected_rows")
            if (sweep_name and isinstance(rows, int) and isinstance(expected, int)
                    and 0 < expected and rows < expected):
                raise GuardError(
                    "E_SWEEP_EXPORT_INCOMPLETE: touchstone for sweep %r has %d of %d "
                    "requested rows" % (sweep_name, rows, expected)
                )
            if sweep_name and isinstance(rows, int) and rows < 3:
                raise GuardError(
                    "E_SWEEP_EXPORT_INCOMPLETE: touchstone for sweep %r has %d row(s) "
                    "after %s attempt(s); refusing to report a bandwidth"
                    % (sweep_name, rows, details.get("attempts"))
                )
            by_frequency: dict[str, dict[str, str]] = {}
            singles: dict[str, str] = {}
            for name, info in (far.get("cuts") or {}).items():
                base, _, label = str(name).partition("@")
                path = info.get("path")
                if not path:
                    continue
                if label:
                    key = label.replace("GHz", "").strip()
                    slot = by_frequency.setdefault(key, {})
                    if base.startswith("theta"):
                        slot["theta"] = path
                        # the directivity is exported as its own report file, so hand it to
                        # the evaluator alongside the gain cut it belongs to
                        if info.get("directivity_path"):
                            slot["theta_directivity"] = info["directivity_path"]
                    elif base.startswith("phi"):
                        slot["phi"] = path
                else:
                    singles[base] = path
            if by_frequency:
                artifacts["farfield"] = by_frequency
            theta = {name: path for name, path in singles.items() if name.startswith("theta")}
            phi = {name: path for name, path in singles.items() if name.startswith("phi")}
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
            "convergence": convergence_evidence,
        }
        self._stage("COMPLETED", **detail)
        if self.on_completed is not None:
            try:
                self.on_completed({
                    "run_id": self.run_id,
                    "feasible": bool(result.get("feasible")),
                    "metrics": named_metrics(result, self.spec),
                    "params": self.params,
                    "artifacts": artifacts,
                    "convergence": convergence_evidence,
                })
            except Exception:  # noqa: BLE001 - archiving must not fail the run
                pass
        return "COMPLETED"





