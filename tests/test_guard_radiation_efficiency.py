"""The far-field export may carry directivity, which yields radiation efficiency."""
from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from aedt_guard.evaluator import evaluate, radiation_efficiency


def write_curve(path: Path, rows: list[tuple[float, float, float | None]]) -> None:
    lines = []
    if rows and rows[0][2] is not None:
        lines.append('"Theta [deg]","dB(GainTotal) []","dB(DirectivityTotal) []"')
        for theta, gain, directivity in rows:
            lines.append("%s,%s,%s" % (theta, gain, directivity))
    else:
        lines.append('"Theta [deg]","dB(GainTotal) []"')
        for theta, gain, _ in rows:
            lines.append("%s,%s" % (theta, gain))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_touchstone(path: Path) -> None:
    lines = ["# GHz S dB R 50"]
    for frequency in range(24, 31):
        lines.append("%d -20.0 0.0" % frequency)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def spec(with_efficiency: bool = True) -> dict:
    metrics = [
        {"id": "gain", "kind": "peak_gain", "operator": ">=", "target": 5.0, "unit": "dBi"},
    ]
    if with_efficiency:
        metrics.append({"id": "eff", "kind": "radiation_efficiency", "operator": ">=",
                        "target": 0.5, "unit": "1"})
    return {
        "case_id": "synthetic",
        "frequency_ghz": [24.0, 30.0],
        "metrics": metrics,
        "evaluation": {"horiz_theta_deg": 90, "main_lobe_exclusion_deg": 10},
    }


class RadiationEfficiencyMetricTests(unittest.TestCase):
    def test_formula_and_clamping(self):
        self.assertAlmostEqual(radiation_efficiency(9.0, 10.0), 10 ** -0.1)
        self.assertEqual(radiation_efficiency(9.0, 9.0), 1.0)
        self.assertEqual(radiation_efficiency(9.0, 8.0), 1.0)
        self.assertIsNone(radiation_efficiency(None, 9.0))

    def _artifacts(self, directory: Path, with_directivity: bool) -> dict:
        theta = directory / "theta.csv"
        rows = [(0.0, -5.0, -5.0), (10.0, 2.0, 3.0), (90.0, 9.0, 9.8), (180.0, -5.0, -5.0)]
        if with_directivity:
            write_curve(theta, [(t, g, d) for t, g, d in rows])
        else:
            write_curve(theta, [(t, g, None) for t, g, d in rows])
        s11 = directory / "result.s1p"
        write_touchstone(s11)
        return {"s11": str(s11), "farfield": {"28": {"theta": str(theta)}}}

    def test_efficiency_is_reported_when_directivity_is_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = self._artifacts(Path(tmp), with_directivity=True)
            result = evaluate(artifacts, spec())
            self.assertIn("radiation_efficiency", result["metrics"])
            value = result["metrics"]["radiation_efficiency"]["value"]
            self.assertAlmostEqual(value, 10 ** ((9.0 - 9.8) / 10.0), places=6)
            self.assertIn("directivity_db", result["metrics"])

    def test_efficiency_is_absent_when_only_gain_is_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = self._artifacts(Path(tmp), with_directivity=False)
            result = evaluate(artifacts, spec(with_efficiency=False))
            self.assertNotIn("radiation_efficiency", result["metrics"])
            self.assertIn("peak_gain", result["metrics"])

    def test_missing_efficiency_does_not_invalidate_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = self._artifacts(Path(tmp), with_directivity=False)
            result = evaluate(artifacts, spec(with_efficiency=True))
            self.assertIsNone(result["invalid"])
            self.assertIn("peak_gain", result["metrics"])


if __name__ == "__main__":
    unittest.main()
