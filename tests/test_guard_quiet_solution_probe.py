"""The pre-solve probe must not run a report query: that call floods AEDT's message window.

A real DoE run showed rows of 'Script macro error: Solution is not a property name' before every solve.
The cause was ``_probe_solution_data`` falling back to ``post.get_solution_data`` whenever the setup's
``is_solved`` flag was unavailable; PyAEDT's report plumbing emits those AEDT script-macro errors on
designs with app-created sweeps. 'Could not determine' (None) is the honest and quiet answer.
"""
from __future__ import annotations

import unittest

from aedt_target import AedtTarget
from pyaedt_backend import PyAedtBackend


class _Setup:
    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def is_solved(self):
        raise AttributeError("is_solved")


class _Post:
    def __init__(self) -> None:
        self.solution_calls: list[dict] = []
        self.report_calls: list[dict] = []

    def get_solution_data(self, **kwargs):
        self.solution_calls.append(kwargs)
        raise RuntimeError("vendor report plumbing failed")

    def create_report(self, **kwargs):
        self.report_calls.append(kwargs)
        return False


class _Desktop:
    def __init__(self, **_kwargs) -> None:
        self.port = 0
        self.aedt_process_id = 1


class _App:
    def __init__(self) -> None:
        self.project_name = "P"
        self.design_name = "D"
        self.post = _Post()
        self.setups = [_Setup("Setup1")]
        self.valid = True
        self.field_setup_names = ["Infinite Sphere1"]

    def validate_simple(self):
        return True

    def get_setups(self):
        return ["Setup1"]


def _backend(app) -> PyAedtBackend:
    return PyAedtBackend(desktop_factory=lambda **kwargs: _Desktop(**kwargs),
                         hfss_factory=lambda **kwargs: app, version="2026.1")


class QuietProbeTests(unittest.TestCase):
    def test_preflight_does_not_query_reports_when_the_flag_is_unavailable(self):
        app = _App()
        result = _backend(app).execute(
            AedtTarget("port", 50051), "preflight_design",
            {"project_name": "P", "design_name": "D", "setup_name": "Setup1"})
        self.assertIsNone(result["solution_data_present_before_submit"])
        self.assertEqual(app.post.solution_calls, [],
                         "the probe must not emit AEDT script-macro noise via report queries")
        self.assertEqual(app.post.report_calls, [])

    def test_finalize_does_not_query_reports_either(self):
        app = _App()
        app.save_project = lambda: True
        app.get_setup = lambda name: _Setup(name)
        result = _backend(app).execute(
            AedtTarget("port", 50051), "finalize_solve",
            {"project_name": "P", "design_name": "D", "setups": ["Setup1"]})
        self.assertIsNone(result["setup_solution_data"]["Setup1"])
        self.assertFalse(result["solution_data_complete"])
        self.assertEqual(result["unverified_setups"], ["Setup1"])
        self.assertEqual(app.post.solution_calls, [])

    def test_a_known_flag_is_still_reported(self):
        class _Solved(_Setup):
            @property
            def is_solved(self):
                return True

        app = _App()
        app.setups = [_Solved("Setup1")]
        result = _backend(app).execute(
            AedtTarget("port", 50051), "preflight_design",
            {"project_name": "P", "design_name": "D", "setup_name": "Setup1"})
        self.assertTrue(result["solution_data_present_before_submit"])


if __name__ == "__main__":
    unittest.main()
