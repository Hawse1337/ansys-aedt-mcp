# -*- coding: utf-8 -*-
"""Effective-bandwidth metric tests (guard side, offline)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import mcp_server
from aedt_guard.evaluator import evaluate


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

    def test_unsimulated_frequency_never_passes(self):
        # only two frequencies measured; the gap between them must not count
        freqs = [24.0, 30.0]
        result = evaluate(self._artifacts({24.0: -15.0, 30.0: -15.0}), _spec(freqs))
        ebw = result["metrics"]["effective_bandwidth"]
        self.assertEqual(ebw["value"], 6.0)
        self.assertEqual(ebw["samples"], 2)


if __name__ == "__main__":
    unittest.main()
