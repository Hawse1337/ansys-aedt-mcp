# -*- coding: utf-8 -*-
"""Tests for the touchstone export hardening (v1.15.11).

Motivation: a swept run could export a one-row touchstone and every bandwidth
metric would silently read 0 GHz, which looks like a design conclusion instead of
an export failure. The export now retries the vendor call (ExportNetworkData fails
intermittently right after a solve) and reports the row count so the guard can
fail closed.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import tempfile

import mcp_server
from aedt_target import AedtTarget
from pyaedt_capabilities import _count_touchstone_rows


class RowCounterTests(unittest.TestCase):
    def test_counts_data_rows_and_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.s1p"
            path.write_text(
                "# GHz S MA R 50\n"
                "! comment\n"
                "24 0.9 -170\n"
                "27 0.2 30\n"
                "28 0.1 10\n",
                encoding="utf-8",
            )
            self.assertEqual(_count_touchstone_rows(path), 3)

    def test_missing_file_counts_zero(self):
        self.assertEqual(_count_touchstone_rows(Path("does-not-exist.s1p")), 0)


class _FakeApp:
    def __init__(self, rows_per_call):
        self.rows_per_call = list(rows_per_call)
        self.calls = 0
        self.design_type = "HFSS"

    def export_touchstone(self, output_file=None, setup=None, sweep=None):
        self.calls += 1
        rows = self.rows_per_call.pop(0) if self.rows_per_call else 0
        if rows == "raise":
            raise RuntimeError("grpc ExportNetworkData failed")
        path = Path(output_file)
        lines = ["# GHz S MA R 50"]
        for index in range(rows):
            lines.append("%g 0.5 0" % (24 + index))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)


class ExportRetryTests(unittest.TestCase):
    def _export(self, app):
        from pyaedt_capabilities import OfficialCapabilities

        capabilities = OfficialCapabilities(app_resolver=lambda *a, **k: app)
        return capabilities._export_results(
            desktop=None, target=AedtTarget.from_values(pid=1),
            arguments={"output_path": str(self.tmp / "out.s1p"),
                       "export_type": "touchstone",
                       "setup_name": "P4",
                       "sweep_name": "ANT_Band"},
            connection_kwargs={},
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_retries_after_vendor_error_and_reports_attempts(self):
        app = _FakeApp(["raise", 5])
        result = self._export(app)
        self.assertEqual(app.calls, 2)
        self.assertEqual(result["details"]["attempts"], 2)
        self.assertEqual(result["details"]["row_count"], 5)

    def test_retries_when_a_swept_export_returns_one_row(self):
        app = _FakeApp([1, 5])
        result = self._export(app)
        self.assertEqual(app.calls, 2)
        self.assertEqual(result["details"]["row_count"], 5)

    def test_only_one_attempt_without_a_sweep(self):
        from pyaedt_capabilities import OfficialCapabilities

        app = _FakeApp([1, 5])
        capabilities = OfficialCapabilities(app_resolver=lambda *a, **k: app)
        result = capabilities._export_results(
            desktop=None, target=AedtTarget.from_values(pid=1),
            arguments={"output_path": str(self.tmp / "single.s1p"),
                       "export_type": "touchstone", "setup_name": "P4"},
            connection_kwargs={},
        )
        self.assertEqual(app.calls, 1)
        self.assertEqual(result["details"]["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
