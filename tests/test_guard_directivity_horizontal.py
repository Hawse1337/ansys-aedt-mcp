"""The horizontal (theta=90) directivity is part of the brief: it must be measured, not assumed."""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from aedt_guard import pattern


def sphere_grid(path: Path, beam_theta: float = 90.0) -> Path:
    """A synthetic AEDT sphere-grid export: one column per phi, one row per theta."""
    phis = [0.0, 60.0, 120.0, 180.0, 240.0, 300.0, 360.0]
    header = ["Freq [GHz]", "Theta [deg]"] + ["dB(GainTotal) [dB]"] * len(phis)
    lines = [",".join('"%s"' % cell if index < 2 else cell
                      for index, cell in enumerate(header))]
    for theta in range(-180, 181, 2):
        if abs(theta - beam_theta) < 1e-6:
            gain = 10.0
        elif abs(theta) < 1e-6 or abs(abs(theta) - 180) < 1e-6:
            gain = -40.0
        else:
            gain = -10.0
        cells = ["28", str(theta)] + ["%.1f" % gain for _ in phis]
        lines.append(",".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class HorizontalDirectivityTests(unittest.TestCase):
    def test_a_beam_at_ninety_degrees_is_the_horizontal_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            grid = sphere_grid(Path(tmp) / "grid.csv", beam_theta=90.0)
            thetas, phis, values = pattern.read_sphere_grid(grid)
            result = pattern.directivity_horizontal_from_grid(thetas, values, 90.0)
        self.assertEqual(result["horizontal_theta"], 90.0)
        self.assertAlmostEqual(result["horizontal_db"], result["directivity_db"], places=6)
        self.assertGreater(result["horizontal_db"], 0.0)
        self.assertGreater(result["radiation_efficiency"], 0.0)
        self.assertLessEqual(result["radiation_efficiency"], 1.0)

    def test_a_beam_away_from_ninety_degrees_scores_lower(self):
        with tempfile.TemporaryDirectory() as tmp:
            grid = sphere_grid(Path(tmp) / "grid.csv", beam_theta=0.0)
            thetas, _phis, values = pattern.read_sphere_grid(grid)
            result = pattern.directivity_horizontal_from_grid(thetas, values, 90.0)
        self.assertGreater(result["directivity_db"] - result["horizontal_db"], 3.0,
                           "a beam at theta=0 must not look like a horizontal beam")

    def test_a_grid_without_a_horizontal_sample_is_refused(self):
        thetas = [0.0, 30.0, 60.0, 120.0, 150.0, 180.0]
        values = [[0.0] * 4 for _ in thetas]
        with self.assertRaises(ValueError):
            pattern.directivity_horizontal_from_grid(thetas, values, 90.0)

    def test_efficiency_and_peak_directivity_are_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            grid = sphere_grid(Path(tmp) / "grid.csv", beam_theta=90.0)
            thetas, _phis, values = pattern.read_sphere_grid(grid)
            result = pattern.directivity_horizontal_from_grid(thetas, values, 90.0)
            peak = pattern.directivity_from_grid(thetas, values)
        efficiency = 10.0 ** ((10.0 - result["directivity_db"]) / 10.0)
        self.assertAlmostEqual(result["radiation_efficiency"], efficiency, places=3)
        self.assertAlmostEqual(result["directivity_db"], peak, places=6)


class EvaluatorMetricTests(unittest.TestCase):
    def test_the_evaluator_reports_the_horizontal_metric(self):
        from aedt_guard.evaluator import evaluate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            grid = sphere_grid(root / "grid.csv", beam_theta=90.0)
            theta = root / "theta.csv"
            theta.write_text('"Theta [deg]","dB(GainTotal) []"\n'
                             + "\n".join("%d,%.1f" % (t, 10.0 if t == 90 else -10.0)
                                         for t in range(-180, 181, 2)) + "\n",
                             encoding="utf-8")
            phi = root / "phi.csv"
            phi.write_text('"Phi [deg]","dB(GainTotal) []"\n0,10\n60,10\n120,10\n180,10\n'
                           '240,10\n300,10\n360,10\n', encoding="utf-8")
            s11 = root / "s11.csv"
            s11.write_text("# GHz S dB R 50\n" + "\n".join("%d -20 0" % f for f in range(24, 31))
                           + "\n", encoding="utf-8")
            spec = {"case_id": "synthetic", "frequency_ghz": [24.0, 30.0],
                    "metrics": [{"id": "gain", "kind": "peak_gain", "operator": ">=",
                                 "target": 5.0, "hard": True},
                                {"id": "dir", "kind": "directivity_horiz_db",
                                 "operator": ">=", "target": 5.0, "hard": True}],
                    "evaluation": {"horiz_theta_deg": 90}}
            result = evaluate({"s11": str(s11),
                               "farfield": {"28.0": {"theta": str(theta), "phi": str(phi),
                                                     "sphere_grid": str(grid)}}}, spec)
        self.assertIn("directivity_horiz_db", result["metrics"])
        value = result["metrics"]["directivity_horiz_db"]["value"]
        self.assertAlmostEqual(value, result["metrics"]["directivity_db"]["value"], places=6)


if __name__ == "__main__":
    unittest.main()
