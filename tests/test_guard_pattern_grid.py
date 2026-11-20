"""The sphere-grid reader and its directivity integral."""
from __future__ import annotations

import math
from pathlib import Path
import tempfile
import unittest

from aedt_guard.pattern import directivity_from_grid, read_sphere_grid


def write_grid(directory: Path, thetas, phis, values) -> Path:
    path = directory / "grid.csv"
    header = ['"Freq [GHz]"', '"Theta [deg]"'] + ['"dB(GainTotal) []"' for _ in phis]
    lines = [",".join(header)]
    for row_index, theta in enumerate(thetas):
        cells = ["28", str(theta)] + [("%.4f" % value) for value in values[row_index]]
        lines.append(",".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class GridReaderTests(unittest.TestCase):
    def test_reads_one_column_per_phi(self):
        thetas = [0.0, 90.0, 180.0]
        phis = [0.0, 90.0, 180.0, 270.0, 360.0]
        values = [[-10.0 for _ in phis] for _ in thetas]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_grid(Path(tmp), thetas, phis, values)
            got_thetas, got_phis, got_values = read_sphere_grid(path)
        self.assertEqual(got_thetas, thetas)
        self.assertEqual(len(got_phis), len(phis))
        self.assertEqual(len(got_values[0]), len(phis))

    def test_missing_file_is_rejected(self):
        with self.assertRaises(ValueError):
            read_sphere_grid(Path("nope.csv"))


class DirectivityTests(unittest.TestCase):
    def _grid(self, n_theta=181, n_phi=73):
        thetas = [180.0 * i / (n_theta - 1) for i in range(n_theta)]
        phis = [360.0 * j / (n_phi - 1) for j in range(n_phi)]
        return thetas, phis

    def test_isotropic_is_zero_db(self):
        thetas, phis = self._grid()
        values = [[0.0 for _ in phis] for _ in thetas]
        self.assertAlmostEqual(directivity_from_grid(thetas, values), 0.0, places=2)

    def test_sin_pattern_is_four_over_pi(self):
        thetas = [180.0 * i / 180.0 for i in range(181)]
        phis = [360.0 * j / 4.0 for j in range(5)]
        values = []
        for theta in thetas:
            lobe = max(math.sin(math.radians(theta)), 1e-12)
            values.append([10.0 * math.log10(lobe) for _ in phis])
        got = directivity_from_grid(thetas, values)
        self.assertAlmostEqual(got, 10.0 * math.log10(4.0 / math.pi), places=1)

    def test_partial_sphere_is_rejected(self):
        with self.assertRaises(ValueError):
            directivity_from_grid([0.0, 10.0, 20.0],
                                  [[0.0] * 5, [0.0] * 5, [0.0] * 5])

    def test_degenerate_pattern_is_rejected(self):
        thetas = [180.0 * i / 36.0 for i in range(37)]
        values = [[-200.0] * 5 for _ in thetas]
        with self.assertRaises(ValueError):
            directivity_from_grid(thetas, values)


if __name__ == "__main__":
    unittest.main()
