# -*- coding: utf-8 -*-
"""Offline tests for configurable solver parallelism (cores / tasks / GPUs)."""
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
from aedt_guard import SpecError, resolve_solver_settings
from aedt_guard.store import Store


def _write_s1p(path: Path) -> None:
    magnitude = 10 ** (-15.0 / 20.0)
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


class SolverSettingsTests(unittest.TestCase):
    def _spec(self, policy=None):
        return mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics(),
                                   execution_policy=policy)

    def test_nothing_requested_means_no_arguments(self):
        settings = resolve_solver_settings(self._spec(), cpu_count=16)
        self.assertEqual(settings["effective"], {})
        self.assertEqual(settings["requested"], {})

    def test_override_and_spec_defaults(self):
        spec = self._spec({"num_cores": 4})
        self.assertEqual(resolve_solver_settings(spec)["effective"],
                         {"num_cores": 4, "num_tasks": 1, "num_gpus": 0})
        overridden = resolve_solver_settings(spec, num_cores=8, num_tasks=2)
        self.assertEqual(overridden["effective"],
                         {"num_cores": 8, "num_tasks": 2, "num_gpus": 0})
        self.assertEqual(overridden["total_cores"], 16)

    def test_max_total_cores_clamps_with_warning(self):
        spec = self._spec({"num_cores": 32, "num_tasks": 4, "max_total_cores": 16})
        settings = resolve_solver_settings(spec, cpu_count=64)
        self.assertEqual(settings["total_cores"], 16)
        self.assertTrue(settings["warnings"])
        self.assertLessEqual(settings["effective"]["num_cores"] * settings["effective"]["num_tasks"], 16)

    def test_cpu_count_bounds_when_no_explicit_cap(self):
        spec = self._spec({"num_cores": 64})
        settings = resolve_solver_settings(spec, cpu_count=8)
        self.assertEqual(settings["total_cores"], 8)
        self.assertTrue(settings["warnings"])

    def test_strict_mode_raises_core_budget(self):
        spec = self._spec({"num_cores": 64, "enforce_cpu_budget": True})
        with self.assertRaises(SpecError) as ctx:
            resolve_solver_settings(spec, cpu_count=8)
        self.assertIn("E_CORE_BUDGET", str(ctx.exception))

    def test_invalid_values_rejected(self):
        spec = self._spec()
        with self.assertRaises(SpecError):
            resolve_solver_settings(spec, num_cores=0)
        with self.assertRaises(SpecError):
            resolve_solver_settings(spec, num_tasks=-2)


class SolverSettingsWiringTests(unittest.TestCase):
    def setUp(self):
        os.environ["AEDT_GUARD_NO_DAEMON"] = "1"
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = str(Path(self.tmp.name) / "camp")
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())
        self.calls = []

    def tearDown(self):
        for _ in range(10):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.2)

    def _fake_execute(self, target, command, arguments, timeout=None):
        self.calls.append((command, dict(arguments)))
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
            theta = out / "t.csv"
            phi = out / "p.csv"
            _write_theta(theta)
            _write_phi(phi)
            return {"cuts": {"theta_phi0": {"path": str(theta)},
                             "phi_theta90": {"path": str(phi)}}}
        raise AssertionError(f"unexpected command: {command}")

    def _wait(self, run_id):
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = mcp_server.get_run_status(self.campaign, run_id)
            if status["terminal"]:
                break
            time.sleep(0.05)
        time.sleep(1.0)
        return status

    def test_cores_and_tasks_reach_the_worker_and_the_record(self):
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            submitted = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0, num_cores=8, num_tasks=2,
            )
            status = self._wait(submitted["run_id"])
        self.assertEqual(status["status"], "COMPLETED", status)
        analyze = next(args for cmd, args in self.calls if cmd == "analyze_design")
        self.assertEqual(analyze["num_cores"], 8)
        self.assertEqual(analyze["num_tasks"], 2)
        self.assertNotIn("num_gpus", analyze)  # 0 must not be sent
        self.assertEqual(submitted["solver_settings"]["total_cores"], 16)
        self.assertEqual(status["solver_settings"]["requested"]["num_cores"], 8)

    def test_spec_policy_defaults_apply_when_no_override(self):
        from aedt_guard import load_spec, save_spec

        spec = load_spec(Path(self.campaign) / "design_spec.json")
        spec["execution_policy"]["num_cores"] = 4
        save_spec(spec, Path(self.campaign) / "design_spec.json")
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            submitted = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0,
            )
            self._wait(submitted["run_id"])
        analyze = next(args for cmd, args in self.calls if cmd == "analyze_design")
        self.assertEqual(analyze["num_cores"], 4)

    def test_solver_settings_do_not_change_cache_key(self):
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            first = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0, num_cores=2,
            )
            self._wait(first["run_id"])
            second = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0, num_cores=8,
            )
        self.assertTrue(second.get("cached"))
        self.assertEqual(second["run_id"], first["run_id"])


if __name__ == "__main__":
    unittest.main()

