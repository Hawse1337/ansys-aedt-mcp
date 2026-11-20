"""Tests for the expected-row check on swept touchstone exports.

The same geometry exported 4 rows once and 7 rows on a repeat with identical
per-frequency S11 values; the sparse export silently halved the bandwidth. The
guard now compares the row count with the sweep's own frequency count.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from aedt_target import AedtTarget
from pyaedt_capabilities import OfficialCapabilities


class _Sweep:
    def __init__(self, count):
        self.frequency = ["%gGHz" % (24 + i) for i in range(count)]


class _Setup:
    def __init__(self, count):
        self._count = count

    def get_sweep(self, name):
        return _Sweep(self._count)


class _App:
    design_type = "HFSS"

    def __init__(self, rows, sweep_count):
        self.rows = rows
        self.sweep_count = sweep_count
        self.calls = 0

    def export_touchstone(self, output_file=None, setup=None, sweep=None):
        self.calls += 1
        path = Path(output_file)
        lines = ["# GHz S MA R 50"]
        for index in range(self.rows):
            lines.append("%g 0.5 0" % (24 + index))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def get_setup(self, name):
        return _Setup(self.sweep_count)


class ExpectedRowsTests(unittest.TestCase):
    def _export(self, app):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        capabilities = OfficialCapabilities(app_resolver=lambda *a, **k: app)
        return capabilities._export_results(
            desktop=None, target=AedtTarget.from_values(pid=1),
            arguments={"output_path": str(Path(tmp.name) / "out.s1p"),
                       "export_type": "touchstone", "setup_name": "P4",
                       "sweep_name": "ANT_Band"},
            connection_kwargs={},
        )

    def test_expected_rows_come_from_the_sweep(self):
        app = _App(rows=7, sweep_count=7)
        result = self._export(app)
        self.assertEqual(result["details"]["row_count"], 7)
        self.assertEqual(result["details"]["expected_rows"], 7)

    def test_missing_rows_are_reported(self):
        app = _App(rows=4, sweep_count=7)
        result = self._export(app)
        self.assertEqual(result["details"]["row_count"], 4)
        self.assertEqual(result["details"]["expected_rows"], 7)


if __name__ == "__main__":
    unittest.main()
