# -*- coding: utf-8 -*-
"""Effective-bandwidth metric and guarded-executor integration (offline)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import mcp_server
from aedt_guard.evaluator import evaluate
from antopt.core import Constraint, Objective
from antopt.executor import GuardConfig, GuardExecutor
from antopt.synthetic import build_case

os.environ.setdefault("AEDT_GUARD_NO_DAEMON", "1")


def _write_s1p(path: Path, s11_by_freq: dict[float, float]) -> None:
    lines = ["# GHz S MA R 50"]
    for freq in sorted(s11_by_freq):
        magnitude = 10 ** (s11_by_freq[freq] / 20.0)
        lines.append(f"{freq:.4f} {magnitude:.8f} 0.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_theta(path: Path, peak_gain: float = 6.5) -> None:
    lines = ['"Theta [deg]","Phi [deg]","dB(GainTotal) []"']
    for theta in range(0, 181, 2):
        delta = abs(theta - 90.0)
        gain = peak_gain - 1.5 * delta if delta <= 12 else peak_gain - 18.0
        lines.append(f'{theta},"0.0",{gain:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_phi(path: Path, level: float = 6.0, wobble: float = 1.0) -> None:
    lines = ['"Phi [deg]","Theta [deg]","dB(GainTotal) []"']
    for phi in range(0, 361, 5):
        gain = level + (wobble if phi % 20 else 0.0)
        lines.append(f'{phi},"90.0",{gain:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _spec(frequencies, with_gain=True):
    metrics = [
        {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=", "target": -10.0,
         "hard": True, "band_ghz": [24.0, 30.0]},
        {"id": "ripple", "kind": "ripple", "unit": "dB", "operator": "<=", "target": 3.0,
         "hard": True},
        {"id": "ebw", "kind": "effective_bandwidth", "unit": "GHz", "operator": ">=",
         "target": 2.0, "hard": True},
    ]
    if with_gain:
        metrics.insert(1, {"id": "gain_h", "kind": "realized_gain_h", "unit": "dBi",
                           "operator": ">=", "target": 4.0, "hard": True})
    return mcp_server.new_spec(
        "lens", "lens", [24.0, 30.0], metrics,
        evaluation={"frequencies_ghz": list(frequencies)},
    )


class EffectiveBandwidthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _artifacts(self, s11_by_freq, gains=None):
        s1p = self.dir / "cand.s1p"
        _write_s1p(s1p, s11_by_freq)
        farfield = {}
        gains = gains or {}
        for freq in sorted(s11_by_freq):
            theta = self.dir / f"theta_{freq}.csv"
            phi = self.dir / f"phi_{freq}.csv"
            _write_theta(theta, peak_gain=gains.get(freq, 6.5))
            _write_phi(phi)
            farfield[f"{freq:g}"] = {"theta": str(theta), "phi": str(phi)}
        return {"s11": str(s1p), "farfield": farfield}

    def test_full_band_span(self):
        freqs = [24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0]
        result = evaluate(self._artifacts({f: -15.0 for f in freqs}), _spec(freqs))
        ebw = result["metrics"]["effective_bandwidth"]
        self.assertEqual(ebw["value"], 6.0)
        self.assertEqual(ebw["low_ghz"], 24.0)
        self.assertEqual(ebw["high_ghz"], 30.0)

    def test_midband_s11_failure_splits_band(self):
        freqs = [24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0]
        s11 = {f: -15.0 for f in freqs}
        s11[25.0] = -8.0                              # 24 alone, then 26-30
        result = evaluate(self._artifacts(s11), _spec(freqs))
        ebw = result["metrics"]["effective_bandwidth"]
        self.assertEqual(ebw["value"], 4.0)
        self.assertEqual(ebw["low_ghz"], 26.0)
        self.assertEqual(ebw["high_ghz"], 30.0)

    def test_gain_failure_shortens_band(self):
        freqs = [24.0, 26.0, 28.0, 30.0]
        s11 = {f: -15.0 for f in freqs}
        result = evaluate(self._artifacts(s11, gains={28.0: 1.0}), _spec(freqs))
        ebw = result["metrics"]["effective_bandwidth"]
        self.assertEqual(ebw["value"], 2.0)          # 24-26 only

    def test_effective_bandwidth_constraint_drives_feasibility(self):
        freqs = [24.0, 27.0, 30.0]
        s11 = {24.0: -15.0, 27.0: -8.0, 30.0: -15.0}
        result = evaluate(self._artifacts(s11), _spec(freqs))
        self.assertEqual(result["metrics"]["effective_bandwidth"]["value"], 0.0)
        self.assertFalse(result["feasible"])


class GuardExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = str(Path(self.tmp.name) / "camp")
        self.case = build_case()
        mcp_server.spec_create(
            self.campaign, "lens", "lens", [24.0, 30.0],
            [
                {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=",
                 "target": -10.0, "hard": True, "band_ghz": [24.0, 30.0]},
                {"id": "gain_h", "kind": "realized_gain_h", "unit": "dBi",
                 "operator": ">=", "target": 4.0, "hard": True},
            ],
            evaluation={"frequencies_ghz": [24.0, 27.0, 30.0]},
        )
        self.calls: list[tuple[str, dict]] = []

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
                path.write_text("conv", encoding="utf-8")
                return {"path": str(path), "evidence": {"assessment": "converged"}}
            _write_s1p(path, {24.0: -14.0, 25.0: -14.0, 26.0: -14.0, 27.0: -14.0,
                              28.0: -14.0, 29.0: -14.0, 30.0: -14.0})
            return {"path": str(path)}
        if command == "export_farfield_report":
            out = Path(arguments["output_dir"])
            out.mkdir(parents=True, exist_ok=True)
            cuts = {}
            for freq in arguments.get("frequencies_ghz") or [28.0]:
                label = "@%gGHz" % freq
                theta = out / f"t{label}.csv"
                phi = out / f"p{label}.csv"
                _write_theta(theta)
                _write_phi(phi)
                cuts[f"theta_phi0{label}"] = {"path": str(theta)}
                cuts[f"phi_theta90{label}"] = {"path": str(phi)}
            return {"cuts": cuts}
        raise AssertionError(f"unexpected command {command}")

    def test_guard_executor_returns_measured_evaluation(self):
        config = GuardConfig(
            campaign_dir=self.campaign,
            project_name="lens",
            design_name="Lens",
            setup_name="Setup1",
            frequencies_ghz=[24.0, 27.0, 30.0],
            poll_interval_s=0.01,
            timeout_s=5.0,
            pid=1,
        )
        executor = GuardExecutor(self.case, config)
        candidate = self.case.make_candidate({"x1": 0.5, "x2": 0.5})
        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=self._fake_execute):
            entry = executor.evaluate(candidate)
        self.assertEqual(entry.status, "COMPLETED")
        self.assertTrue(entry.feasible, entry.violations)
        self.assertIsNotNone(entry.run_id)
        self.assertAlmostEqual(entry.metrics["s11"], -14.0, places=3)
        self.assertGreater(entry.metrics["realized_gain_h"], 4.0)
        analyze = next(args for cmd, args in self.calls if cmd == "analyze_design")
        self.assertEqual(analyze["project_name"], "lens")


if __name__ == "__main__":
    unittest.main()

