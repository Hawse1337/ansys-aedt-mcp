# -*- coding: utf-8 -*-
"""Offline tests for crash recovery / resume (P4 item 1)."""
from __future__ import annotations

import json
import os
os.environ.setdefault("AEDT_GUARD_NO_DAEMON", "1")
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import mcp_server
from aedt_guard.sim import FAILED, CampaignRunner, last_stage
from aedt_guard.store import Store


def _write_s1p(path: Path, level_db=-15.0) -> None:
    magnitude = 10 ** (level_db / 20.0)
    lines = ["# GHz S MA R 50"]
    for freq in np.arange(24.0, 30.01, 0.25):
        lines.append(f"{freq:.4f} {magnitude:.6f} 0.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_theta(path: Path) -> None:
    lines = ['"Theta [deg]","Phi [deg]","dB(GainTotal) []"']
    for theta in range(0, 181, 2):
        delta = abs(theta - 90.0)
        gain = 6.5 - 1.5 * delta if delta <= 12 else -18.0
        lines.append(f'{theta},"0.0",{gain:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_phi(path: Path) -> None:
    lines = ['"Phi [deg]","Theta [deg]","dB(GainTotal) []"']
    for phi in range(0, 361, 5):
        lines.append(f'{phi},"90.0",{6.0 if phi % 20 else 7.0:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metrics():
    return [
        {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=", "target": -10.0,
         "hard": True, "band_ghz": [24.0, 30.0]},
        {"id": "gain_h", "kind": "realized_gain_h", "unit": "dBi", "operator": ">=",
         "target": 4.0, "hard": True},
    ]


class ResumeAdapter:
    """Adapter for resume: solve-side methods must never be called."""

    def __init__(self, *, complete=True):
        self.complete = complete
        self.calls = []

    def preflight(self):
        self.calls.append("preflight")
        raise AssertionError("resume must not preflight")

    def submit(self):
        self.calls.append("submit")
        raise AssertionError("resume must not submit")

    def wait(self, timeout_s, poll_interval_s):
        self.calls.append("wait")
        raise AssertionError("resume must not wait")

    def finalize(self):
        self.calls.append("finalize")
        return {"saved": True, "solution_data_complete": self.complete,
                "incomplete_setups": [] if self.complete else ["Setup1"]}

    def export_touchstone(self, path):
        self.calls.append("touchstone")
        _write_s1p(path)
        return {"path": str(path)}

    def export_farfield(self, output_dir, cuts, frequencies=None):
        self.calls.append("farfield")
        theta = Path(output_dir) / "theta.csv"
        phi = Path(output_dir) / "phi.csv"
        _write_theta(theta)
        _write_phi(phi)
        return {"cuts": {"theta_phi0": {"path": str(theta)},
                         "phi_theta90": {"path": str(phi)}}}


class ResumeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = Path(self.tmp.name)
        self.db = self.root / "campaign.sqlite"
        self.spec = mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics())

    def tearDown(self):
        self.tmp.cleanup()

    def _store_and_runner(self, adapter):
        store = Store(self.db)
        store.add_run("r1", "lens", 1, {"project_name": "p", "design_name": "d"}, status="SOLVED")
        runner = CampaignRunner(
            store=store,
            adapter=adapter,
            spec=self.spec,
            run_id="r1",
            artifact_dir=self.root / "runs" / "r1" / "artifacts",
            frequency_ghz=28.0,
        )
        return store, runner

    def test_resume_skips_solve_and_completes(self):
        adapter = ResumeAdapter()
        store, runner = self._store_and_runner(adapter)
        status = runner.resume("SOLVED")
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(adapter.calls, ["finalize", "farfield", "touchstone"])
        detail = store.get_run("r1")
        self.assertEqual(detail["status"], "COMPLETED")
        self.assertIn("s11", detail["metrics"])
        log = json.loads(detail["notes"])
        export_entry = next(e for e in log if e["stage"] == "EXPORTING")
        self.assertEqual(export_entry["resumed_from"], "SOLVED")
        store.close()

    def test_resume_terminal_state_is_noop(self):
        adapter = ResumeAdapter()
        store, runner = self._store_and_runner(adapter)
        self.assertEqual(runner.resume("COMPLETED"), "COMPLETED")
        self.assertEqual(adapter.calls, [])
        store.close()

    def test_resume_fails_closed_without_solution_data(self):
        adapter = ResumeAdapter(complete=False)
        store, runner = self._store_and_runner(adapter)
        self.assertEqual(runner.resume("SOLVED"), FAILED)
        self.assertEqual(store.get_run("r1")["status"], FAILED)
        self.assertNotIn("touchstone", adapter.calls)
        store.close()

    def test_resume_preserves_prior_stage_history(self):
        adapter = ResumeAdapter()
        store = Store(self.db)
        prior = [{"stage": "PREPARED"}, {"stage": "SOLVING"}, {"stage": "SOLVED"},
                 {"stage": "EXPORTING"}]
        store.add_run("r1", "lens", 1, {"project_name": "p", "design_name": "d"},
                      status="EXPORTING", notes=json.dumps(prior))
        runner = CampaignRunner(
            store=store,
            adapter=adapter,
            spec=self.spec,
            run_id="r1",
            artifact_dir=self.root / "runs" / "r1" / "artifacts",
            frequency_ghz=28.0,
        )
        self.assertEqual(runner.resume("EXPORTING"), "COMPLETED")
        log = json.loads(store.get_run("r1")["notes"])
        stages = [entry["stage"] for entry in log]
        self.assertEqual(stages[:4], ["PREPARED", "SOLVING", "SOLVED", "EXPORTING"])
        self.assertEqual(stages[-1], "COMPLETED")
        self.assertEqual(stages.count("SOLVING"), 1)
        self.assertEqual(stages.count("VALIDATING"), 1)
        store.close()

    def test_last_stage_helper(self):
        self.assertIsNone(last_stage([]))
        self.assertIsNone(last_stage(None))
        self.assertEqual(last_stage([{"stage": "SOLVING"}, {"stage": "SOLVED"}]), "SOLVED")


class ResumeToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = str(Path(self.tmp.name) / "camp")
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_tool_finishes_without_analyze(self):
        with Store(Path(self.campaign) / "campaign.sqlite") as store:
            store.add_run(
                "r1", "lens", 1,
                {"project_name": "p", "design_name": "d", "setup_name": "Setup1"},
                status="EXPORTING",
                artifact_dir=str(Path(self.campaign) / "runs" / "r1" / "artifacts"),
                notes=json.dumps([{"stage": "PREPARED"}, {"stage": "SOLVING"},
                                  {"stage": "SOLVED"}, {"stage": "EXPORTING"}]),
            )
        commands: list[str] = []

        def fake_execute(target, command, arguments, timeout=None):
            commands.append(command)
            if command == "finalize_solve":
                return {"saved": True, "solution_data_complete": True}
            if command == "export_results":
                path = Path(arguments["output_path"])
                if arguments.get("export_type") == "convergence":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("convergence", encoding="utf-8")
                    return {"path": str(path), "evidence": {"assessment": "converged",
                                                             "native_converged": True}}
                _write_s1p(path)
                return {"path": str(path)}
            if command == "export_farfield_report":
                theta = Path(arguments["output_dir"]) / "t.csv"
                phi = Path(arguments["output_dir"]) / "p.csv"
                _write_theta(theta)
                _write_phi(phi)
                return {"cuts": {"theta_phi0": {"path": str(theta)},
                                 "phi_theta90": {"path": str(phi)}}}
            raise AssertionError(f"unexpected command during resume: {command}")

        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=fake_execute):
            submitted = mcp_server.resume_guarded_run(self.campaign, "r1", pid=1)
            self.assertEqual(submitted["resumed_from"], "EXPORTING")
            deadline = time.monotonic() + 10
            status = None
            while time.monotonic() < deadline:
                status = mcp_server.get_run_status(self.campaign, "r1")
                if status["terminal"]:
                    break
                time.sleep(0.05)
        self.assertEqual(status["status"], "COMPLETED", status)
        self.assertNotIn("analyze_design", commands)
        self.assertNotIn("start_analysis", commands)

    def test_resume_tool_ignores_terminal_runs(self):
        with Store(Path(self.campaign) / "campaign.sqlite") as store:
            store.add_run("r1", "lens", 1, {}, status="COMPLETED")
        result = mcp_server.resume_guarded_run(self.campaign, "r1", pid=1)
        self.assertFalse(result["resumed"])
        self.assertEqual(result["status"], "COMPLETED")

    def test_resume_tool_rejects_unknown_run(self):
        with self.assertRaises(ValueError):
            mcp_server.resume_guarded_run(self.campaign, "nope", pid=1)


if __name__ == "__main__":
    unittest.main()



