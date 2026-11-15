"""Stale plots inherited from the reference project are cleared before each solve."""
from __future__ import annotations

import unittest

from aedt_target import AedtTarget
from pyaedt_backend import PyAedtBackend, _clear_design_reports


class _Reporter:
    def __init__(self, names):
        self.names = list(names)
        self.deleted: list[str] = []

    def GetAllReportNames(self):
        return list(self.names)

    def DeleteReports(self, names):
        self.deleted.extend(names)
        for name in names:
            if name in self.names:
                self.names.remove(name)


class _Design:
    def __init__(self, reporter):
        self._reporter = reporter

    def GetModule(self, name):
        return self._reporter if name == "ReportSetup" else None


class _App:
    def __init__(self, names):
        self.reporter = _Reporter(names)
        self.odesign = _Design(self.reporter)
        self.project_name = "P"
        self.design_name = "D"
        self.valid = True
        self.field_setup_names = ["Infinite Sphere1"]

    def validate_simple(self):
        return True

    def get_setups(self):
        return []


class _Desktop:
    def __init__(self, **_kwargs):
        self.port = 0
        self.aedt_process_id = 1


class ClearingTests(unittest.TestCase):
    def test_inherited_reports_are_deleted_and_counted(self):
        app = _App(["UserReport1", "UserReport2"])
        cleared = _clear_design_reports(app)
        self.assertEqual(cleared, 2)
        self.assertEqual(app.reporter.names, [])

    def test_a_design_without_reports_is_a_no_op(self):
        self.assertEqual(_clear_design_reports(_App([])), 0)

    def test_a_broken_reporter_never_fails_the_cleanup(self):
        class Broken:
            @property
            def odesign(self):
                raise RuntimeError("vendor object unavailable")

        self.assertEqual(_clear_design_reports(Broken()), 0)

    def test_preflight_reports_how_many_plots_were_removed(self):
        app = _App(["Old1", "Old2", "Old3"])
        backend = PyAedtBackend(desktop_factory=lambda **kwargs: _Desktop(**kwargs),
                                hfss_factory=lambda **kwargs: app, version="2026.1")
        result = backend.execute(AedtTarget("port", 50051), "preflight_design",
                                 {"project_name": "P", "design_name": "D",
                                  "setup_name": "Setup1"})
        self.assertEqual(result["reports_cleared"], 3)
        self.assertEqual(app.reporter.names, [])


if __name__ == "__main__":
    unittest.main()
