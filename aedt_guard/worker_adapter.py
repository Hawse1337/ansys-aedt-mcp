"""Worker-backed adapter used by guarded runs (MCP tools and the daemon).

All AEDT access goes through the existing PyAEDT broker (``worker_client``), so
the adapter inherits the same attach hardening and run-token enforcement.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def _target(pid: int | None, port: int | None) -> Any:
    from aedt_target import AedtTarget

    return AedtTarget.from_values(pid=pid, port=port)


def _default_client() -> Any:
    from worker_client import WorkerClient

    return WorkerClient()


class GuardedAdapter:
    """Bridge the runner to the PyAEDT worker over the existing broker."""

    def __init__(
        self,
        *,
        pid: int | None,
        port: int | None,
        project_name: str,
        design_name: str,
        setup_name: str,
        cuts: list[str],
        worker_timeout: float,
        sweep_name: str | None = None,
        farfield_setup: str | None = None,
        extra_setups: list[str] | None = None,
        run_token: str | None = None,
        solver_settings: dict[str, Any] | None = None,
        client: Any = None,
    ):
        self.pid = pid
        self.port = port
        self.project_name = project_name
        self.design_name = design_name
        self.setup_name = setup_name
        self.cuts = cuts
        self.worker_timeout = worker_timeout
        self.sweep_name = sweep_name
        self.farfield_setup = farfield_setup
        self.extra_setups = list(extra_setups or [])
        self.run_token = run_token
        self.solver_settings = solver_settings or {}
        self.client = client

    def _call(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = _target(self.pid, self.port)
        result = (self.client or _default_client()).execute(
            target, command, arguments, timeout=self.worker_timeout
        )
        if not isinstance(result, dict):
            raise TypeError(f"AEDT worker command {command} returned a non-object result")
        return result

    def preflight(self) -> dict[str, Any]:
        return self._call(
            "preflight_design",
            {
                "project_name": self.project_name,
                "design_name": self.design_name,
                "setup_name": self.setup_name,
            },
        )

    def submit(self) -> dict[str, Any]:
        return self._call(
            "analyze_design",
            {
                "project_name": self.project_name,
                "design_name": self.design_name,
                "setup_name": self.setup_name,
                "save_before_solve": False,
                **({"run_token": self.run_token} if self.run_token else {}),
                **{
                    key: value
                    for key, value in (self.solver_settings.get("effective") or {}).items()
                    if value not in (None, 0)
                },
            },
        )

    def wait(self, timeout_s: float, poll_interval_s: float) -> dict[str, Any]:
        started = time.monotonic()
        polls = 0
        saw_running = False
        settle_s = min(float(timeout_s), max(2.0, 4.0 * float(poll_interval_s)))
        while True:
            status = self._call(
                "analysis_status",
                {
                    "project_name": self.project_name,
                    "design_name": self.design_name,
                    "setup_name": self.setup_name,
                },
            )
            polls += 1
            running = bool(status.get("running"))
            if running:
                saw_running = True
            if not running:
                # AEDT can report idle for a few seconds before the solver shows
                # up; do not treat that as completion until either the solver was
                # observed running, or the settle window has elapsed.
                if saw_running or (time.monotonic() - started) >= settle_s:
                    return {"running": False, "poll_count": polls, "last": status,
                            "saw_running": saw_running}
            if time.monotonic() - started >= timeout_s:
                return {"running": True, "timed_out": True, "poll_count": polls,
                        "saw_running": saw_running}
            time.sleep(poll_interval_s)

    def finalize(self) -> dict[str, Any]:
        # Probe only the setup this run was told to solve: a reused project may
        # contain other setups/sweeps that belong to different experiments, and
        # their absence of solution data must not fail this run.
        arguments: dict[str, Any] = {
            "project_name": self.project_name,
            "design_name": self.design_name,
        }
        if self.setup_name:
            arguments["setups"] = [self.setup_name]
        return self._call("finalize_solve", arguments)

    def export_touchstone(self, path: Path) -> dict[str, Any]:
        return self._call(
            "export_results",
            {
                "output_path": str(path),
                "export_type": "touchstone",
                "setup_name": self.setup_name,
                **({"sweep_name": self.sweep_name} if self.sweep_name else {}),
            },
        )

    def export_farfield(self, output_dir: Path, cuts: list[str],
                        frequencies: list[float] | None = None) -> dict[str, Any]:
        arguments = {
            "project_name": self.project_name,
            "design_name": self.design_name,
            # far fields often live on a dedicated setup (e.g. FF_Swp), not on
            # the S11 setup that produced the touchstone.
            "setup_name": self.farfield_setup or self.setup_name,
            "output_dir": str(output_dir),
            "cuts": cuts,
        }
        if self.sweep_name:
            arguments["sweep_name"] = self.sweep_name
        if frequencies:
            arguments["frequencies_ghz"] = list(frequencies)
        return self._call("export_farfield_report", arguments)

    def analyze_setup(self, setup_name: str) -> dict[str, Any]:
        """Solve an additional setup needed by this run (e.g. far-field setup)."""
        return self._call(
            "analyze_design",
            {
                "project_name": self.project_name,
                "design_name": self.design_name,
                "setup_name": setup_name,
                "save_before_solve": False,
                **({
                    key: value
                    for key, value in (self.solver_settings.get("effective") or {}).items()
                    if value not in (None, 0)
                }),
            },
        )

    def wait_setup(self, setup_name: str, timeout_s: float,
                   poll_interval_s: float) -> dict[str, Any]:
        started = time.monotonic()
        polls = 0
        saw_running = False
        settle_s = min(float(timeout_s), max(2.0, 4.0 * float(poll_interval_s)))
        while True:
            status = self._call(
                "analysis_status",
                {"project_name": self.project_name, "design_name": self.design_name,
                 "setup_name": setup_name},
            )
            polls += 1
            running = bool(status.get("running"))
            if running:
                saw_running = True
            if not running and (saw_running or (time.monotonic() - started) >= settle_s):
                return {"running": False, "poll_count": polls, "last": status,
                        "saw_running": saw_running}
            if time.monotonic() - started >= timeout_s:
                return {"running": True, "timed_out": True, "poll_count": polls,
                        "saw_running": saw_running}
            time.sleep(poll_interval_s)

    def abort(self) -> dict[str, Any]:
        return self._call("abort_analysis", {"project_name": self.project_name,
                                             "design_name": self.design_name})

    def export_convergence(self, path: Path) -> dict[str, Any]:
        return self._call(
            "export_results",
            {
                "output_path": str(path),
                "export_type": "convergence",
                "setup_name": self.setup_name,
            },
        )




