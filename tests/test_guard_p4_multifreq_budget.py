# -*- coding: utf-8 -*-
"""Offline tests for multi-frequency far fields and run budgets (P4-5/6)."""
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
from aedt_guard import SpecError, resolve_frequencies
from aedt_guard.evaluator import evaluate
from aedt_guard.sim import FAILED, CampaignRunner
from aedt_guard.store import Store


def _write_s1p(path: Path, level_db=-15.0, freqs=None) -> None:
    magnitude = 10 ** (level_db / 20.0)
    lines = ["# GHz S MA R 50"]
    for freq in (freqs or np.arange(24.0, 30.01, 0.25)):
        lines.append(f"{freq:.4f} {magnitude:.6f} 0.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_theta(path: Path, peak_gain=6.5, peak_theta=90.0) -> None:
    lines = ['"Theta [deg]","Phi [deg]","dB(GainTotal) []"']
    for theta in range(0, 181, 2):
        delta = abs(theta - peak_theta)
        gain = peak_gain - 1.5 * delta if delta <= 12 else peak_gain - 18.0
        lines.append(f'{theta},"0.0",{gain:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_phi(path: Path, level=6.0) -> None:
    lines = ['"Phi [deg]","Theta [deg]","dB(GainTotal) []"']
    for phi in range(0, 361, 5):
        lines.append(f'{phi},"90.0",{level + (1.0 if phi % 20 else 0.0):.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metrics():
    return [
        {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=", "target": -10.0,
         "hard": True, "band_ghz": [24.0, 30.0]},
        {"id": "gain_h", "kind": "realized_gain_h", "unit": "dBi", "operator": ">=",
         "target": 4.0, "hard": True},
        {"id": "ripple", "kind": "ripple", "unit": "dB", "operator": "<=", "target": 3.0,
         "hard": False},
        {"id": "sll", "kind": "sll", "unit": "dBc", "operator": "<=", "target": -8.0,
         "hard": False},
    ]


class FrequencyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.spec = mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics())

    def test_default_three_points(self):
        self.assertEqual(resolve_frequencies(self.spec), [24.0, 27.0, 30.0])

    def test_explicit_list_and_override(self):
        self.spec["evaluation"]["frequencies_ghz"] = [28.0, 24.0, 26.0]
        self.assertEqual(resolve_frequencies(self.spec), [24.0, 26.0, 28.0])
        self.assertEqual(resolve_frequencies(self.spec, [27.0]), [27.0])

    def test_linear_sampling(self):
        self.spec["evaluation"]["frequency_sampling"] = {"mode": "linear", "points": 5}
        self.assertEqual(resolve_frequencies(self.spec), [24.0, 25.5, 27.0, 28.5, 30.0])

    def test_cap_enforced(self):
        self.spec["evaluation"]["max_frequency_points"] = 3
        self.spec["evaluation"]["frequencies_ghz"] = [24.0, 25.0, 26.0, 27.0]
        with self.assertRaises(SpecError) as ctx:
            resolve_frequencies(self.spec)
        self.assertIn("E_FREQ_POINT_BUDGET", str(ctx.exception))

    def test_out_of_band_rejected(self):
        self.spec["evaluation"]["frequencies_ghz"] = [31.0]
        with self.assertRaises(SpecError):
            resolve_frequencies(self.spec)


class MultiFrequencyEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.dir = Path(self.tmp.name)
        self.spec = mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics())

    def tearDown(self):
        self.tmp.cleanup()

    def _artifacts(self, gains, s11_db=-15.0):
        s1p = self.dir / "cand.s1p"
        _write_s1p(s1p, level_db=s11_db)
        farfield = {}
        for freq, gain in gains.items():
            theta = self.dir / f"theta_{freq}.csv"
            phi = self.dir / f"phi_{freq}.csv"
            _write_theta(theta, peak_gain=gain)
            _write_phi(phi)
            farfield[f"{freq:g}"] = {"theta": str(theta), "phi": str(phi)}
        return {"s11": str(s1p), "farfield": farfield}

    def test_worst_case_aggregation(self):
        result = evaluate(self._artifacts({24.0: 6.5, 27.0: 5.0, 30.0: 7.5}), self.spec)
        self.assertIsNone(result["invalid"])
        gain = result["metrics"]["realized_gain_h"]
        self.assertEqual(gain["frequencies_ghz"], [24.0, 27.0, 30.0])
        self.assertLess(abs(gain["value"] - min(gain["per_frequency"].values())), 1e-9)
        self.assertTrue(result["feasible"], result)
        self.assertAlmostEqual(result["metrics"]["ripple"]["value"], 1.0, places=6)

    def test_low_gain_frequency_fails_hard_constraint(self):
        result = evaluate(self._artifacts({24.0: 6.5, 30.0: 1.0}), self.spec)
        self.assertIn("gain_h", result["unmet"])
        self.assertFalse(result["feasible"])

    def test_missing_frequency_artifact_is_invalid(self):
        artifacts = self._artifacts({24.0: 6.5})
        artifacts["farfield"]["30"] = {"theta": str(self.dir / "missing.csv")}
        result = evaluate(artifacts, self.spec)
        self.assertIsNotNone(result["invalid"])
        self.assertFalse(result["feasible"])


class BudgetAdapter:
    def __init__(self, delay=0.0):
        self.aborted = 0
        self.waited = 0
        self.wait_timeout = None
        self.delay = delay

    def preflight(self):
        return {"validation_passed": True}

    def submit(self):
        return {"started": True}

    def wait(self, timeout_s, poll_interval_s):
        self.waited += 1
        self.wait_timeout = timeout_s
        if self.delay:
            time.sleep(self.delay)
        return {"running": True, "timed_out": True, "poll_count": 3}

    def abort(self):
        self.aborted += 1
        return {"stopped": True}

    def finalize(self):
        raise AssertionError("must not finalize after a budget breach")

    def export_convergence(self, path):
        raise AssertionError("unreachable")

    def export_touchstone(self, path):
        raise AssertionError("unreachable")

    def export_farfield(self, output_dir, cuts, frequencies=None):
        raise AssertionError("unreachable")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_budget_caps_the_wait_and_then_aborts(self):
        spec = mcp_server.new_spec(
            "lens", "lens", [24.0, 30.0], _metrics(),
            execution_policy={"max_run_minutes": 0.05, "require_convergence": False},
        )
        store = Store(self.root / "campaign.sqlite")
        store.add_run("r1", "lens", 1, {})
        adapter = BudgetAdapter(delay=3.2)
        runner = CampaignRunner(store=store, adapter=adapter, spec=spec, run_id="r1",
                                artifact_dir=self.root / "runs" / "r1" / "artifacts",
                                timeout_s=900.0)
        status = runner.run()
        store.close()
        self.assertEqual(status, FAILED)
        # the wait must be capped by the budget (3 s), not by timeout_s (900 s)
        self.assertAlmostEqual(adapter.wait_timeout, 3.0, places=6)
        self.assertEqual(adapter.aborted, 1)

    def test_budget_breach_aborts_and_fails(self):
        spec = mcp_server.new_spec(
            "lens", "lens", [24.0, 30.0], _metrics(),
            execution_policy={"max_run_minutes": 1e-9, "require_convergence": False},
        )
        store = Store(self.root / "campaign.sqlite")
        store.add_run("r1", "lens", 1, {})
        adapter = BudgetAdapter()
        runner = CampaignRunner(store=store, adapter=adapter, spec=spec, run_id="r1",
                                artifact_dir=self.root / "runs" / "r1" / "artifacts")
        self.assertEqual(runner.run(), FAILED)
        self.assertEqual(adapter.aborted, 1)
        detail = store.get_run("r1")
        self.assertEqual(detail["status"], FAILED)
        log = json.loads(detail["notes"])
        self.assertIn("BUDGET_EXCEEDED", log[-1]["error"])
        store.close()


class MultiFrequencyToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = str(Path(self.tmp.name) / "camp")
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())
        from aedt_guard import load_spec, save_spec

        spec = load_spec(Path(self.campaign) / "design_spec.json")
        spec["evaluation"]["frequencies_ghz"] = [24.0, 27.0, 30.0]
        save_spec(spec, Path(self.campaign) / "design_spec.json")
        self.exe_args = []

    def tearDown(self):
        for _ in range(10):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.2)

    def _fake_execute(self, target, command, arguments, timeout=None):
        self.exe_args.append((command, dict(arguments)))
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
            if arguments.get("export_type") == "convergence":
                path.write_text("convergence", encoding="utf-8")
                return {"path": str(path), "evidence": {"assessment": "converged"}}
            _write_s1p(path)
            return {"path": str(path)}
        if command == "export_farfield_report":
            out = Path(arguments["output_dir"])
            cuts = {}
            for freq in arguments.get("frequencies_ghz") or [None]:
                label = "" if freq is None else "@%gGHz" % freq
                theta = out / f"t{label}.csv"
                phi = out / f"p{label}.csv"
                _write_theta(theta, peak_gain=6.0 if freq != 30.0 else 5.0)
                _write_phi(phi)
                cuts[f"theta_phi0{label}"] = {"path": str(theta)}
                cuts[f"phi_theta90{label}"] = {"path": str(phi)}
            return {"cuts": cuts}
        raise AssertionError(f"unexpected command: {command}")

    def test_multi_frequency_run_exports_each_frequency(self):
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            submitted = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0, sweep_name="Sweep1",
            )
            deadline = time.monotonic() + 10
            status = None
            while time.monotonic() < deadline:
                status = mcp_server.get_run_status(self.campaign, submitted["run_id"])
                if status["terminal"]:
                    break
                time.sleep(0.05)
            time.sleep(1.0)
        self.assertEqual(status["status"], "COMPLETED", status)
        farfield_calls = [args for cmd, args in self.exe_args if cmd == "export_farfield_report"]
        self.assertEqual(farfield_calls[-1]["frequencies_ghz"], [24.0, 27.0, 30.0])
        self.assertEqual(farfield_calls[-1]["sweep_name"], "Sweep1")
        gain = status["metrics"]["gain_h"]
        self.assertIsNotNone(gain["value"])

    def test_frequencies_override_changes_cache_key(self):
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            first = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if mcp_server.get_run_status(self.campaign, first["run_id"])["terminal"]:
                    break
                time.sleep(0.05)
            time.sleep(1.0)
            second = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0, frequencies_ghz=[27.0],
            )
        self.assertFalse(second.get("cached"))
        self.assertNotEqual(second["run_id"], first["run_id"])


if __name__ == "__main__":
    unittest.main()


