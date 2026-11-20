# -*- coding: utf-8 -*-
"""Offline tests for the guarded-workflow library (aedt_guard)."""
from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from aedt_guard import (
    ArtifactError,
    SpecError,
    Store,
    evaluate,
    load_spec,
    new_spec,
    passivity,
    realized_gain,
    save_spec,
    s11_band,
    sidelobe_level,
    task_context,
    update_spec,
    validate_spec,
)


def _metrics():
    return [
        {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=", "target": -10.0,
         "hard": True, "band_ghz": [24.0, 30.0]},
        {"id": "gain_h", "kind": "realized_gain_h", "unit": "dBi", "operator": ">=",
         "target": 5.0, "hard": True},
        {"id": "ripple", "kind": "ripple", "unit": "dB", "operator": "<=", "target": 3.0,
         "hard": True},
        {"id": "sll", "kind": "sll", "unit": "dBc", "operator": "<=", "target": -8.0,
         "hard": False},
    ]


def _write_s1p(path: Path, freqs, mags, phases=None) -> None:
    phases = phases or [0.0] * len(freqs)
    lines = ["# GHz S MA R 50"]
    for freq, mag, phase in zip(freqs, mags, phases):
        lines.append(f"{freq:.4f} {mag:.6f} {phase:.2f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_theta_cut(path: Path, peak_theta=82.0, peak_gain=6.5) -> None:
    lines = ['"Theta [deg]","Phi [deg]","dB(GainTotal) []"']
    for theta in range(0, 181, 2):
        gain = peak_gain - 0.02 * abs(theta - peak_theta) ** 1.2
        if abs(theta - peak_theta) > 40:
            gain = peak_gain - 14.0 + 0.05 * abs(theta - peak_theta)
        lines.append(f'{theta},"0.0",{gain:.4f}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_phi_cut(path: Path, level=6.0, wobble=1.0) -> None:
    lines = ['"Phi [deg]","Theta [deg]","dB(GainTotal) []"']
    for phi in range(0, 361, 5):
        gain = level + wobble * math.sin(math.radians(phi * 2))
        lines.append(f'{phi},"90.0",{gain:.4f}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class SpecTests(unittest.TestCase):
    def test_new_spec_is_valid(self):
        spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
        self.assertEqual(spec["version"], 1)
        self.assertEqual(validate_spec(spec), [])
        self.assertEqual(spec["evaluation"]["horiz_theta_deg"], 90)

    def test_validation_catches_errors(self):
        spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
        spec["metrics"][0]["operator"] = "~="
        spec["metrics"].append(dict(spec["metrics"][0]))
        spec["frequency_ghz"] = [30.0, 24.0]
        problems = validate_spec(spec)
        self.assertTrue(any("operator" in p for p in problems))
        self.assertTrue(any("duplicate metric id" in p for p in problems))
        self.assertTrue(any("frequency_ghz" in p for p in problems))

    def test_new_spec_rejects_bad_metric(self):
        bad = [{"id": "x", "kind": "nope", "unit": "dB", "operator": "<=", "target": 1.0}]
        with self.assertRaises(SpecError):
            new_spec("lens", "metal_lens", [24.0, 30.0], bad)

    def test_update_spec_versions_and_audits(self):
        spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
        updated = update_spec(spec, {"frequency_ghz": [24.0, 31.0]}, reason="band widened",
                              approver="user")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(spec["version"], 1)  # original untouched
        entry = updated["history"][-1]
        self.assertEqual(entry["reason"], "band widened")
        self.assertIn("frequency_ghz", {c["field"] for c in entry["diff"]})

    def test_update_spec_requires_reason_and_approver(self):
        spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
        with self.assertRaises(SpecError):
            update_spec(spec, {"frequency_ghz": [24.0, 31.0]}, reason="", approver="user")
        with self.assertRaises(SpecError):
            update_spec(spec, {"frequency_ghz": [24.0, 31.0]}, reason="x", approver="")

    def test_roundtrip_and_task_context(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            path = Path(tmp) / "spec.json"
            spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
            save_spec(spec, path)
            loaded = load_spec(path)
            self.assertEqual(loaded["project"], "lens")
            context = task_context(loaded, {"unmet_constraints": ["gain_h"]})
            self.assertEqual(len(context["hard_constraints"]), 3)
            self.assertIn("satisfy", context["next_step_hint"])

    def test_load_rejects_broken_json(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SpecError):
                load_spec(path)


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_s11_band_and_span(self):
        freqs = np.arange(24.0, 30.01, 0.25)
        s11 = np.full_like(freqs, -15.0)
        s11[(freqs > 27.5) & (freqs < 28.5)] = -8.0
        info = s11_band(freqs, s11, (24.0, 30.0))
        self.assertAlmostEqual(info["worst_db"], -8.0)
        self.assertAlmostEqual(info["worst_freq_ghz"], 27.75)
        self.assertGreater(info["span_ghz"], 2.0)

    def test_realized_gain_math(self):
        value = float(realized_gain(6.5, -10.0))
        expected = 6.5 + 10 * math.log10(1 - 0.1)
        self.assertAlmostEqual(value, expected, places=6)

    def test_sidelobe_relative_to_peak(self):
        theta = np.arange(0, 181, 2.0)
        delta = np.abs(theta - 82.0)
        gain = np.where(delta <= 12.0, -1.5 * delta, -18.0)
        sll = sidelobe_level(theta, gain, exclude_main_deg=10)
        self.assertLess(sll, -15.0)

    def test_evaluate_feasible(self):
        s1p = self.dir / "cand.s1p"
        _write_s1p(s1p, np.arange(24.0, 30.01, 0.25), np.full(25, 0.15))
        theta = self.dir / "theta0.csv"
        _write_theta_cut(theta)
        phi = self.dir / "phi90.csv"
        _write_phi_cut(phi)
        spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
        result = evaluate(
            {"s11": str(s1p), "theta_cuts": {"0": str(theta)}, "phi_cut": str(phi),
             "frequency_ghz": 28.0},
            spec,
        )
        self.assertIsNone(result["invalid"])
        self.assertTrue(result["feasible"], result)
        self.assertAlmostEqual(result["metrics"]["beam_theta"]["value"], 82.0, places=3)
        self.assertLessEqual(result["metrics"]["ripple"]["value"], 3.0)

    def test_evaluate_flags_unmet_hard_constraint(self):
        s1p = self.dir / "bad.s1p"
        _write_s1p(s1p, np.arange(24.0, 30.01, 0.25), np.full(25, 0.9))  # ~ -0.9 dB
        theta = self.dir / "theta0.csv"
        _write_theta_cut(theta, peak_gain=2.0)
        spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
        result = evaluate({"s11": str(s1p), "theta_cuts": {"0": str(theta)},
                           "frequency_ghz": 28.0}, spec)
        self.assertFalse(result["feasible"])
        self.assertIn("s11", result["unmet"])

    def test_evaluate_missing_artifact_is_invalid(self):
        spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
        result = evaluate({"s11": str(self.dir / "missing.s1p")}, spec)
        self.assertIsNotNone(result["invalid"])
        self.assertFalse(result["feasible"])

    def test_passivity_detects_active_result(self):
        good = self.dir / "good.s2p"
        good.write_text(
            "# GHz S MA R 50\n"
            "28 0.1 0 0.9 90\n"
            "29 0.2 0 0.8 90\n",
            encoding="utf-8",
        )
        self.assertTrue(passivity(good)["passive"])
        bad = self.dir / "bad.s2p"
        bad.write_text("# GHz S MA R 50\n28 0.9 0 0.9 90\n", encoding="utf-8")
        report = passivity(bad)
        self.assertFalse(report["passive"])
        self.assertEqual(report["violations"], 1)
        with self.assertRaises(ArtifactError):
            passivity(self.dir / "nope.s2p")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.db = Path(self.tmp.name) / "campaign.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_metrics_roundtrip_and_best(self):
        with Store(self.db) as store:
            spec = new_spec("lens", "metal_lens", [24.0, 30.0], _metrics())
            store.add_spec_version(spec, reason="initial", approver="user")
            store.add_run("r1", "lens", spec["version"], {"r_mm": 60}, status="done")
            store.add_run("r2", "lens", spec["version"], {"r_mm": 70}, status="done")
            store.add_metrics("r1", {"gain_h": {"value": 6.1, "unit": "dBi"}}, {"gain_h": True})
            store.add_metrics("r2", {"gain_h": {"value": 7.2, "unit": "dBi"}}, {"gain_h": True})
            self.assertEqual(store.counts()["runs"], 2)
            run = store.get_run("r1")
            self.assertEqual(run["params"]["r_mm"], 60)
            self.assertIn("gain_h", run["metrics"])
            best = store.best_feasible("lens", "gain_h", maximize=True)
            self.assertEqual(best["run_id"], "r2")
            worst = store.best_feasible("lens", "gain_h", maximize=False)
            self.assertEqual(worst["run_id"], "r1")

    def test_status_update_and_listing(self):
        with Store(self.db) as store:
            store.add_run("r1", "lens", 1, {})
            store.set_status("r1", "failed", notes="solver aborted")
            runs = store.list_runs("lens")
            self.assertEqual(runs[0]["status"], "failed")
            self.assertEqual(runs[0]["notes"], "solver aborted")


if __name__ == "__main__":
    unittest.main()
