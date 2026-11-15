"""The grid reader must accept both layouts AEDT writes, including the real run's flattened table."""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from aedt_guard import pattern


def flattened_grid(path: Path, beam_theta: float = 90.0) -> Path:
    """Exactly the shape a real export produced: Freq, Phi, Theta, one dB column, one row per pair."""
    phis = list(range(0, 361, 5))
    lines = ['"Freq [GHz]","Phi [deg]","Theta [deg]","dB(GainTotal) []"']
    for theta in range(0, 181, 2):
        for phi in phis:
            if abs(theta - beam_theta) < 1e-6:
                gain = 6.0
            elif abs(theta) < 1e-6 or abs(theta - 180) < 1e-6:
                gain = -40.0
            else:
                gain = -8.0
            lines.append("28,%d,%d,%.4f" % (phi, theta, gain))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FlattenedLayoutTests(unittest.TestCase):
    def test_the_real_export_shape_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            thetas, phis, values = pattern.read_sphere_grid(flattened_grid(Path(tmp) / "g.csv"))
        self.assertEqual(len(thetas), 91)
        self.assertEqual(len(phis), 73)
        self.assertEqual(len(values), 91)
        self.assertEqual(len(values[0]), 73)
        self.assertEqual(thetas[0], 0.0)
        self.assertEqual(thetas[-1], 180.0)

    def test_directivity_is_finite_from_the_flattened_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            thetas, _phis, values = pattern.read_sphere_grid(flattened_grid(Path(tmp) / "g.csv"))
            peak = pattern.directivity_from_grid(thetas, values)
            horizontal = pattern.directivity_horizontal_from_grid(thetas, values, 90.0)
        self.assertTrue(math.isfinite(peak))
        self.assertAlmostEqual(horizontal["horizontal_db"], peak, places=3)

    def test_the_matrix_layout_still_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matrix.csv"
            header = ["Freq [GHz]", "Theta [deg]"] + ["dB(GainTotal) [dB]"] * 7
            lines = [",".join('"%s"' % cell if index < 2 else cell
                              for index, cell in enumerate(header))]
            for theta in range(-180, 181, 2):
                lines.append(",".join(["28", str(theta)] + ["%.1f" % -5.0] * 7))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            thetas, phis, values = pattern.read_sphere_grid(path)
        self.assertEqual(len(thetas), 181)
        self.assertEqual(len(phis), 7)
        self.assertEqual(len(values[0]), 7)

    def test_theta_first_column_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thetafirst.csv"
            lines = ['"Freq [GHz]","Theta [deg]","Phi [deg]","dB(GainTotal) []"']
            for theta in range(0, 181, 2):
                for phi in (0, 120, 240):
                    lines.append("28,%d,%d,0.0" % (theta, phi))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            thetas, phis, values = pattern.read_sphere_grid(path)
        self.assertEqual(len(thetas), 91)
        self.assertEqual(len(phis), 3)


if __name__ == "__main__":
    unittest.main()
