"""The directivity export is a second report file next to the gain cut."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from aedt_guard.evaluator import evaluate, radiation_efficiency


def write_pair(directory: Path, with_directivity: bool) -> tuple[Path, Path]:
    gain = directory / "theta.csv"
    lines = ['"Theta [deg]","dB(GainTotal) []"']
    rows = [(0.0, -5.0), (10.0, 2.0), (90.0, 9.0), (180.0, -5.0)]
    lines += ["%s,%s" % row for row in rows]
    gain.write_text("\n".join(lines) + "\n", encoding="utf-8")
    direct = directory / "theta_directivity.csv"
    if with_directivity:
        dlines = ['"Theta [deg]","dB(DirectivityTotal) []"']
        drows = [(0.0, -4.0), (10.0, 3.0), (90.0, 9.8), (180.0, -4.0)]
        dlines += ["%s,%s" % row for row in drows]
        direct.write_text("\n".join(dlines) + "\n", encoding="utf-8")
    return gain, direct


def spec(with_efficiency: bool) -> dict:
    metrics = [{"id": "gain", "kind": "peak_gain", "operator": ">=", "target": 5.0,
                "unit": "dBi"}]
    if with_efficiency:
        metrics.append({"id": "eff", "kind": "radiation_efficiency", "operator": ">=",
                        "target": 0.5, "unit": "1"})
    return {"case_id": "synthetic", "frequency_ghz": [24.0, 30.0], "metrics": metrics,
            "evaluation": {"horiz_theta_deg": 90, "main_lobe_exclusion_deg": 10,
                           "include_directivity": with_efficiency}}


def touchstone(path: Path) -> None:
    lines = ["# GHz S dB R 50"] + ["%d -20.0 0.0" % f for f in range(24, 31)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class SiblingDirectivityTests(unittest.TestCase):
    def _artifacts(self, directory: Path, with_directivity: bool) -> dict:
        gain, direct = write_pair(directory, with_directivity)
        s11 = directory / "result.s1p"
        touchstone(s11)
        entry = {"theta": str(gain)}
        if with_directivity:
            entry["theta_directivity"] = str(direct)
        return {"s11": str(s11), "farfield": {"28": entry}}

    def test_efficiency_reads_the_sibling_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate(self._artifacts(Path(tmp), True), spec(True))
            self.assertIn("radiation_efficiency", result["metrics"])
            value = result["metrics"]["radiation_efficiency"]["value"]
            self.assertAlmostEqual(value, 10 ** ((9.0 - 9.8) / 10.0), places=6)
            self.assertAlmostEqual(result["metrics"]["directivity_db"]["value"], 9.8)

    def test_without_the_sibling_file_the_metric_stays_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate(self._artifacts(Path(tmp), False), spec(False))
            self.assertNotIn("radiation_efficiency", result["metrics"])
            self.assertIsNone(result["invalid"])

    def test_relative_efficiency_formula(self):
        self.assertAlmostEqual(radiation_efficiency(9.0, 9.8), 10 ** -0.08)


if __name__ == "__main__":
    unittest.main()
