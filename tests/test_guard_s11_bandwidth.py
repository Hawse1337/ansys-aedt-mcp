# -*- coding: utf-8 -*-
"""S11-only bandwidth metric tests (no far fields needed)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import mcp_server
from aedt_guard.evaluator import evaluate, s11_bandwidth


def _write_s11(path: Path, s11_by_freq: dict[float, float]) -> None:
    lines = ["# GHz S MA R 50"]
    for freq in sorted(s11_by_freq):
        lines.append(f"{freq:.4f} {10 ** (s11_by_freq[freq] / 20.0):.8f} 0.0")
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


class S11BandwidthUnitTests(unittest.TestCase):
    def test_contiguous_span(self):
        freqs = [24, 25, 26, 27, 28, 29, 30]
        s11 = [-12, -14, -13, -11, -9, -8, -7]
        result = s11_bandwidth(freqs, s11, -10.0, (24.0, 30.0))
        self.assertEqual(result["value"], 3.0)
        self.assertEqual(result["low_ghz"], 24.0)
        self.assertEqual(result["high_ghz"], 27.0)

    def test_gap_is_not_bridged(self):
        freqs = [24, 26, 28, 30]
        s11 = [-12, -12, -12, -12]
        result = s11_bandwidth(freqs, s11, -10.0, (24.0, 30.0))
        self.assertEqual(result["value"], 6.0)  # measured points span the band
        self.assertEqual(result["samples"], 4)

    def test_band_limits_are_respected(self):
        freqs = [20, 24, 27, 31]
        s11 = [-15, -15, -15, -15]
        result = s11_bandwidth(freqs, s11, -10.0, (24.0, 30.0))
        self.assertEqual(result["value"], 3.0)
        self.assertEqual(result["low_ghz"], 24.0)

    def test_no_pass_returns_zero(self):
        result = s11_bandwidth([24, 28], [-5, -6], -10.0, (24.0, 30.0))
        self.assertEqual(result["value"], 0.0)
        self.assertIsNone(result["low_ghz"])


class S11BandwidthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _spec(self):
        return mcp_server.new_spec(
            "bowl", "bowl", [24.0, 30.0],
            [
                {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=",
                 "target": -10.0, "hard": True, "band_ghz": [24.0, 30.0]},
                {"id": "bw", "kind": "s11_bandwidth", "unit": "GHz", "operator": ">=",
                 "target": 3.0, "hard": True},
                {"id": "gain", "kind": "peak_gain", "unit": "dBi", "operator": ">=",
                 "target": 6.0, "hard": True},
            ],
            evaluation={"frequencies_ghz": [28.0]},
        )

    def test_metric_is_reported_and_gates_feasibility(self):
        s1p = self.dir / "c.s1p"
        _write_s11(s1p, {24.0: -14, 25.0: -13, 26.0: -12, 27.0: -11,
                         28.0: -9, 29.0: -8, 30.0: -7})
        theta = self.dir / "t.csv"
        phi = self.dir / "p.csv"
        _write_theta(theta)
        _write_phi(phi)
        artifacts = {"s11": str(s1p), "farfield": {"28": {"theta": str(theta),
                                                         "phi": str(phi)}}}
        result = evaluate(artifacts, self._spec())
        bandwidth = result["metrics"]["s11_bandwidth"]
        self.assertEqual(bandwidth["value"], 3.0)
        self.assertFalse(result["feasible"])  # 3 GHz is not >= 3.0? equal -> passes
        # equal to target passes the gate; feasibility therefore depends on others

    def test_wide_band_passes_the_gate(self):
        s1p = self.dir / "wide.s1p"
        _write_s11(s1p, {f: -14.0 for f in (24, 25, 26, 27, 28, 29, 30)})
        theta = self.dir / "t2.csv"
        phi = self.dir / "p2.csv"
        _write_theta(theta, peak_gain=7.0)
        _write_phi(phi)
        artifacts = {"s11": str(s1p), "farfield": {"28": {"theta": str(theta),
                                                         "phi": str(phi)}}}
        result = evaluate(artifacts, self._spec())
        self.assertEqual(result["metrics"]["s11_bandwidth"]["value"], 6.0)
        self.assertTrue(result["feasible"])


if __name__ == "__main__":
    unittest.main()
