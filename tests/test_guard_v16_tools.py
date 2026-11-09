# -*- coding: utf-8 -*-
"""Offline tests for the v1.6 guarded-run tools and state machine."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import mcp_server
from aedt_guard.sim import FAILED, RECOVERY_REQUIRED, CampaignRunner
from aedt_guard.store import Store


def _write_s1p(path: Path, level_db=-15.0, freqs_ghz=None) -> None:
    freqs = freqs_ghz or np.arange(24.0, 30.01, 0.25)
    magnitude = 10 ** (level_db / 20.0)
    lines = ["# GHz S MA R 50"]
    for freq in freqs:
        lines.append(f"{freq:.4f} {magnitude:.6f} 0.0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_theta(path: Path, peak_theta=90.0, peak_gain=6.5) -> None:
    lines = ['"Theta [deg]","Phi [deg]","dB(GainTotal) []"']
    for theta in range(0, 181, 2):
        delta = abs(theta - peak_theta)
        gain = peak_gain - 1.5 * delta if delta <= 12 else peak_gain - 18.0
        lines.append(f'{theta},"0.0",{gain:.4f}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_phi(path: Path, level=6.0) -> None:
    lines = ['"Phi [deg]","Theta [deg]","dB(GainTotal) []"']
    for phi in range(0, 361, 5):
        gain = level + (1.0 if phi % 20 == 0 else 0.0)
        lines.append(f'{phi},"90.0",{gain:.4f}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metrics():
    return [
        {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=", "target": -10.0,
         "hard": True, "band_ghz": [24.0, 30.0]},
        {"id": "gain_h", "kind": "realized_gain_h", "unit": "dBi", "operator": ">=",
         "target": 4.0, "hard": True},
        {"id": "ripple", "kind": "ripple", "unit": "dB", "operator": "<=", "target": 3.0,
         "hard": False},
    ]


class FakeAdapter:
    def __init__(self, *, preflight_ok=True, timed_out=False, saved=True,
                 complete=True, export_fails=False, workdir=None):
        self.preflight_ok = preflight_ok
        self.timed_out = timed_out
        self.saved = saved
        self.complete = complete
        self.export_fails = export_fails
        self.workdir = Path(workdir) if workdir else None
        self.calls = []

    def preflight(self):
        self.calls.append("preflight")
        return {"validation_passed": self.preflight_ok, "ff_sphere_present": True}

    def submit(self):
        self.calls.append("submit")
        return {"started": True}

    def wait(self, timeout_s, poll_interval_s):
        self.calls.append("wait")
        return {"running": self.timed_out, "timed_out": self.timed_out, "poll_count": 2}

    def finalize(self):
        self.calls.append("finalize")
        return {"saved": self.saved, "solution_data_complete": self.complete,
                "incomplete_setups": [] if self.complete else ["Setup1"]}

    def export_touchstone(self, path):
        self.calls.append("touchstone")
        if self.export_fails:
            raise RuntimeError("touchstone export failed")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_s1p(path)
        return {"path": str(path)}

    def export_farfield(self, output_dir, cuts):
        self.calls.append("farfield")
        output_dir.mkdir(parents=True, exist_ok=True)
        theta = output_dir / "theta.csv"
        phi = output_dir / "phi.csv"
        _write_theta(theta)
        _write_phi(phi)
        return {"cuts": {"theta_phi0": {"path": str(theta)},
                         "phi_theta90": {"path": str(phi)}}}


class GuardedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = Path(self.tmp.name)
        self.db = self.root / "campaign.sqlite"
        self.spec = mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics())

    def tearDown(self):
        self.tmp.cleanup()

    def _runner(self, adapter, run_id="r1"):
        store = Store(self.db)
        store.add_run(run_id, "lens", 1, {"r": 60})
        return store, CampaignRunner(
            store=store,
            adapter=adapter,
            spec=self.spec,
            run_id=run_id,
            artifact_dir=self.root / "runs" / run_id / "artifacts",
            frequency_ghz=28.0,
        )

    def test_happy_path_completes_with_metrics(self):
        adapter = FakeAdapter(workdir=self.root)
        store, runner = self._runner(adapter)
        self.assertEqual(runner.run(), "COMPLETED")
        detail = store.get_run("r1")
        self.assertEqual(detail["status"], "COMPLETED")
        self.assertIn("s11", detail["metrics"])
        self.assertIn("gain_h", detail["metrics"])
        stages = [entry["stage"] for entry in runner.stage_log]
        self.assertEqual(
            stages,
            ["PREPARED", "SOLVING", "SOLVED", "EXPORTING", "VALIDATING", "COMPLETED"],
        )
        store.close()

    def test_preflight_failure_stops_before_submit(self):
        adapter = FakeAdapter(preflight_ok=False)
        store, runner = self._runner(adapter)
        self.assertEqual(runner.run(), FAILED)
        self.assertNotIn("submit", adapter.calls)
        self.assertEqual(store.get_run("r1")["status"], FAILED)
        store.close()

    def test_timeout_requires_recovery(self):
        adapter = FakeAdapter(timed_out=True)
        store, runner = self._runner(adapter)
        self.assertEqual(runner.run(), RECOVERY_REQUIRED)
        self.assertEqual(store.get_run("r1")["status"], RECOVERY_REQUIRED)
        store.close()

    def test_unsaved_project_requires_recovery(self):
        adapter = FakeAdapter(saved=False)
        store, runner = self._runner(adapter)
        self.assertEqual(runner.run(), RECOVERY_REQUIRED)
        store.close()

    def test_missing_solution_data_fails(self):
        adapter = FakeAdapter(complete=False)
        store, runner = self._runner(adapter)
        self.assertEqual(runner.run(), FAILED)
        self.assertNotIn("touchstone", adapter.calls)
        store.close()

    def test_export_failure_fails(self):
        adapter = FakeAdapter(export_fails=True)
        store, runner = self._runner(adapter)
        self.assertEqual(runner.run(), FAILED)
        self.assertEqual(store.get_run("r1")["status"], FAILED)
        store.close()


class GuardedToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = str(Path(self.tmp.name) / "camp")

    def tearDown(self):
        self.tmp.cleanup()

    def test_spec_lifecycle(self):
        created = mcp_server.spec_create(self.campaign, "lens", "metal_lens",
                                         [24.0, 30.0], _metrics())
        self.assertEqual(created["spec"]["version"], 1)
        fetched = mcp_server.spec_get(self.campaign)
        self.assertEqual(fetched["spec"]["project"], "lens")
        self.assertEqual(len(fetched["versions"]), 1)
        updated = mcp_server.spec_update_with_approval(
            self.campaign, {"frequency_ghz": [24.0, 31.0]},
            reason="band widened", approver="user",
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["history_entry"]["reason"], "band widened")
        again = mcp_server.spec_get(self.campaign)
        self.assertEqual(len(again["versions"]), 2)

    def test_spec_requires_campaign(self):
        from aedt_guard import SpecError as GuardSpecError

        with self.assertRaises(GuardSpecError):
            mcp_server.spec_get(self.campaign)

    def test_evaluate_exported_results_and_store(self):
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())
        with Store(Path(self.campaign) / "campaign.sqlite") as store:
            store.add_run("r1", "lens", 1, {})
        s1p = Path(self.tmp.name) / "c.s1p"
        theta = Path(self.tmp.name) / "theta.csv"
        phi = Path(self.tmp.name) / "phi.csv"
        _write_s1p(s1p)
        _write_theta(theta)
        _write_phi(phi)
        result = mcp_server.evaluate_exported_results(
            self.campaign, str(s1p), {"0": str(theta)}, str(phi), 28.0, run_id="r1"
        )
        self.assertTrue(result["feasible"], result)
        self.assertEqual(result["stored_run_id"], "r1")
        with Store(Path(self.campaign) / "campaign.sqlite") as store:
            detail = store.get_run("r1")
        self.assertIn("gain_h", detail["metrics"])

    def test_campaign_context_and_status(self):
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())
        context = mcp_server.campaign_context(self.campaign)
        self.assertEqual(context["spec_version"], 1)
        self.assertEqual(context["counts"]["runs"], 0)
        empty = mcp_server.get_run_status(self.campaign)
        self.assertIsNone(empty["run_id"])
        unknown = mcp_server.get_run_status(self.campaign, "nope")
        self.assertIsNone(unknown["status"])

    def test_campaign_context_reports_infeasible_completed_run(self):
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())
        with Store(Path(self.campaign) / "campaign.sqlite") as store:
            store.add_run("r1", "lens", 1, {}, status="COMPLETED")
            store.add_metrics(
                "r1",
                {"s11": {"value": -6.8, "unit": "dB"},
                 "gain_h": {"value": 1.5, "unit": "dBi"}},
                {"s11": False, "gain_h": True},
            )
        context = mcp_server.campaign_context(self.campaign)
        self.assertEqual(context["latest_run"]["status"], "COMPLETED")
        self.assertEqual(context["unmet_constraints"], ["s11"])
        self.assertIn("hard constraints", context["next_step_hint"])

    def test_submit_guarded_run_completes_with_fake_worker(self):
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())

        def fake_execute(target, command, arguments, timeout=None):
            if command == "preflight_design":
                return {"validation_passed": True, "ff_sphere_present": True}
            if command == "analyze_design":
                return {"started": True}
            if command == "analysis_status":
                return {"running": False}
            if command == "finalize_solve":
                return {"saved": True, "solution_data_complete": True}
            if command == "export_results":
                path = Path(arguments["output_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_s1p(path)
                return {"path": str(path)}
            if command == "export_farfield_report":
                out = Path(arguments["output_dir"])
                out.mkdir(parents=True, exist_ok=True)
                theta = out / "t.csv"
                phi = out / "p.csv"
                _write_theta(theta)
                _write_phi(phi)
                return {"cuts": {"theta_phi0": {"path": str(theta)},
                                 "phi_theta90": {"path": str(phi)}}}
            raise AssertionError(f"unexpected worker command: {command}")

        submitted = None
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=fake_execute):
            submitted = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1",
                cuts=["theta_phi0", "phi_theta90"], pid=1,
                poll_interval_s=0.01, timeout_s=5.0, frequency_ghz=28.0,
            )
            self.assertEqual(submitted["status"], "RUNNING")
            deadline = time.monotonic() + 10.0
            status = None
            while time.monotonic() < deadline:
                status = mcp_server.get_run_status(self.campaign, submitted["run_id"])
                if status["terminal"]:
                    break
                time.sleep(0.05)
        self.assertEqual(status["status"], "COMPLETED", status)
        self.assertIn("s11", status["metrics"])


if __name__ == "__main__":
    unittest.main()

