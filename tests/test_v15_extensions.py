# -*- coding: utf-8 -*-
"""Offline tests for the v1.5 guarded-workflow extensions (no AEDT session)."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aedt_target import AedtTarget
from pyaedt_backend import BackendCommandError, PyAedtBackend
from pyaedt_capabilities import OfficialCapabilities
import pyaedt_worker

import mcp_server


def _run(coro):
    return asyncio.run(coro)


class Named:
    def __init__(self, name):
        self.name = name


class FakeSetup:
    def __init__(self, name, is_solved=None):
        self.name = name
        self._is_solved = is_solved

    @property
    def is_solved(self):
        if self._is_solved is None:
            raise AttributeError("is_solved")
        return self._is_solved


class FakePost:
    def __init__(self, empty=False):
        self.empty = empty
        self.report_kwargs = None

    def create_report(self, **kwargs):
        self.report_kwargs = kwargs
        return Named("Rep1")

    def export_report_to_csv(self, out_dir, report_name):
        path = Path(out_dir) / (report_name + ".csv")
        path.write_text(
            "" if self.empty else "Theta [deg]\n0\n45\n90\n",
            encoding="utf-8",
        )
        return str(path)

    def get_solution_data(self, **kwargs):
        class _Data:
            full_matrix_real_imag = {"dB(S(1,1))": [[0.0, 0.0]]}
        return _Data()


class FakeApp:
    def __init__(self, **kwargs):
        self.project_name = kwargs.get("project", "P")
        self.design_name = kwargs.get("design", "D")
        self.valid = kwargs.get("valid", True)
        self.sphere = kwargs.get("sphere", ["Infinite Sphere1"])
        self.solved = kwargs.get("solved", None)
        self.save_ok = kwargs.get("save_ok", True)
        self.post = kwargs.get("post") or FakePost()
        self.setup_objs = kwargs.get("setup_objs")
        if self.setup_objs is None:
            self.setup_objs = (
                [FakeSetup("Setup1", is_solved=self.solved)]
                if self.solved is not None
                else [FakeSetup("Setup1")]
            )
        self.analyze_setup_calls = []
        self.save_calls = []

    def validate_simple(self, *args, **kwargs):
        return self.valid

    def save_project(self, *args, **kwargs):
        self.save_calls.append(args)
        return self.save_ok

    def analyze_setup(self, **kwargs):
        self.analyze_setup_calls.append(kwargs)
        return True

    def get_setups(self):
        return [s.name for s in self.setup_objs]

    @property
    def setups(self):
        return self.setup_objs

    @property
    def field_setup_names(self):
        return self.sphere

    @property
    def are_there_simulations_running(self):
        return False


class FakeDesktop:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.aedt_process_id = 4321
        self.port = kwargs.get("port", 0)


class BackendV15Tests(unittest.TestCase):
    def _make_backend(self, app):
        return PyAedtBackend(
            desktop_factory=lambda **kwargs: FakeDesktop(**kwargs),
            hfss_factory=lambda **kwargs: app,
            version="2026.1",
        )

    def test_preflight_passes_with_evidence(self):
        app = FakeApp(solved=True)
        backend = self._make_backend(app)
        result = backend.execute(
            AedtTarget("port", 50051),
            "preflight_design",
            {"project_name": "P", "design_name": "D", "setup_name": "Setup1"},
        )
        self.assertTrue(result["validation_passed"])
        self.assertTrue(result["ff_sphere_present"])
        self.assertTrue(result["solution_data_present_before_submit"])

    def test_preflight_validate_false_raises(self):
        app = FakeApp(valid=False)
        backend = self._make_backend(app)
        with self.assertRaises(BackendCommandError) as ctx:
            backend.execute(
                AedtTarget("port", 50051),
                "preflight_design",
                {"project_name": "P", "design_name": "D", "validate_attempts": 2},
            )
        self.assertIn("E_VALIDATE_FALSE", str(ctx.exception))

    def test_preflight_missing_sphere_raises(self):
        app = FakeApp(sphere=[])
        backend = self._make_backend(app)
        with self.assertRaises(BackendCommandError) as ctx:
            backend.execute(
                AedtTarget("port", 50051),
                "preflight_design",
                {"project_name": "P", "design_name": "D"},
            )
        self.assertIn("E_FF_SPHERE_MISSING", str(ctx.exception))

    def test_preflight_unknown_sphere_does_not_raise(self):
        app = FakeApp(sphere=None)
        backend = self._make_backend(app)
        result = backend.execute(
            AedtTarget("port", 50051),
            "preflight_design",
            {"project_name": "P", "design_name": "D"},
        )
        self.assertIsNone(result["ff_sphere_present"])

    def test_finalize_save_failure_raises(self):
        app = FakeApp(save_ok=False)
        backend = self._make_backend(app)
        with self.assertRaises(BackendCommandError) as ctx:
            backend.execute(
                AedtTarget("port", 50051),
                "finalize_solve",
                {"project_name": "P", "design_name": "D"},
            )
        self.assertIn("E_SAVE_MISSING", str(ctx.exception))

    def test_finalize_reports_incomplete_solution_data(self):
        app = FakeApp(solved=False)
        backend = self._make_backend(app)
        result = backend.execute(
            AedtTarget("port", 50051),
            "finalize_solve",
            {"project_name": "P", "design_name": "D"},
        )
        self.assertTrue(result["saved"])
        self.assertFalse(result["solution_data_complete"])
        self.assertEqual(result["incomplete_setups"], ["Setup1"])

    def test_finalize_complete(self):
        app = FakeApp(solved=True)
        backend = self._make_backend(app)
        result = backend.execute(
            AedtTarget("port", 50051),
            "finalize_solve",
            {"project_name": "P", "design_name": "D"},
        )
        self.assertTrue(result["solution_data_complete"])

    def test_farfield_export_nonempty(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            app = FakeApp()
            backend = self._make_backend(app)
            result = backend.execute(
                AedtTarget("port", 50051),
                "export_farfield_report",
                {
                    "project_name": "P",
                    "design_name": "D",
                    "setup_name": "FF_28",
                    "output_dir": tmp,
                    "cuts": ["phi_theta90"],
                },
            )
            self.assertTrue(result["cuts"]["phi_theta90"]["non_empty"])
            self.assertEqual(
                app.post.report_kwargs["setup_sweep_name"], "FF_28 : LastAdaptive"
            )

    def test_farfield_export_empty_raises(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            app = FakeApp(post=FakePost(empty=True))
            backend = self._make_backend(app)
            with self.assertRaises(BackendCommandError) as ctx:
                backend.execute(
                    AedtTarget("port", 50051),
                    "export_farfield_report",
                    {
                        "project_name": "P",
                        "design_name": "D",
                        "setup_name": "FF_28",
                        "output_dir": tmp,
                    },
                )
            self.assertIn("E_REPORT_EMPTY", str(ctx.exception))

    def test_farfield_export_unknown_cut_raises(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            app = FakeApp()
            backend = self._make_backend(app)
            with self.assertRaises(BackendCommandError):
                backend.execute(
                    AedtTarget("port", 50051),
                    "export_farfield_report",
                    {
                        "project_name": "P",
                        "design_name": "D",
                        "setup_name": "FF_28",
                        "output_dir": tmp,
                        "cuts": ["bogus_cut"],
                    },
                )


class AnalyzeEvidenceFieldsTests(unittest.TestCase):
    """The analyze payload keeps its old keys and adds exactly two evidence keys."""

    def test_analyze_adds_only_evidence_fields(self):
        class SolvedSetup:
            name = "Setup1"
            is_solved = True

        app = FakeApp(solved=True)
        app.setup_objs = [SolvedSetup()]

        def app_resolver(**kwargs):
            return app

        capabilities = OfficialCapabilities(
            app_resolver=app_resolver,
            app_class_resolver=lambda app_type: None,
            log_file_resolver=lambda: "",
        )
        backend = PyAedtBackend(
            desktop_factory=lambda **kwargs: FakeDesktop(**kwargs),
            official_capabilities=capabilities,
        )
        result = backend.execute(
            AedtTarget("port", 50051),
            "analyze_design",
            {"project_name": "P", "design_name": "D", "setup_name": "Setup1"},
        )
        new_keys = {"ff_sphere_present", "solution_data_present_before_submit"}
        self.assertTrue(new_keys.issubset(set(result.keys())))
        self.assertTrue(result["ff_sphere_present"])
        self.assertTrue(result["solution_data_present_before_submit"])
        self.assertFalse(result["solver_completion_confirmed"])
        self.assertFalse(result["convergence_confirmed"])


class WorkerFlushTests(unittest.TestCase):
    def test_run_stream_flushes_stdout_and_stderr_on_exit(self):
        class FakeBackend:
            def __init__(self):
                self.release_calls = 0

            def execute(self, target, command, arguments):
                return {"ok": True}

            def release(self):
                self.release_calls += 1
                return True

            @property
            def session_pid(self):
                return None

        backend = FakeBackend()
        out = io.StringIO()
        with mock.patch.object(sys, "stdout") as out_mock, mock.patch.object(sys, "stderr") as err_mock:
            code = pyaedt_worker.run_stream(
                io.StringIO(""),
                out,
                backend_factory=lambda: backend,
            )
        self.assertEqual(code, 0)
        self.assertTrue(out_mock.flush.called)
        self.assertTrue(err_mock.flush.called)


class McpBlockingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            mcp_server.worker_client, "execute_async", new=mock.AsyncMock()
        )
        self.exec = patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_blocking_payload_unchanged(self):
        submission = {
            "started": True,
            "solver_completion_confirmed": False,
            "convergence_confirmed": False,
        }
        self.exec.return_value = dict(submission)
        result = _run(
            mcp_server.analyze_design(
                project_name="P", design_name="D", setup_name="S", pid=1
            )
        )
        self.assertEqual(result, submission)
        self.assertNotIn("blocking_requested", result)
        arguments = self.exec.call_args[0][2]
        self.assertNotIn("blocking", arguments)

    def test_blocking_completed(self):
        self.exec.side_effect = [
            {"started": True, "solver_completion_confirmed": False},
            {"running": True},
            {"running": False},
        ]
        result = _run(
            mcp_server.analyze_design(
                project_name="P",
                design_name="D",
                setup_name="S",
                pid=1,
                blocking=True,
                blocking_poll_interval_s=1.0,
            )
        )
        self.assertEqual(result["blocking_status"], "completed")
        self.assertEqual(result["blocking_poll_count"], 2)
        self.assertTrue(result["started"])

    def test_blocking_timeout(self):
        self.exec.side_effect = [{"started": True}] + [{"running": True}] * 50
        result = _run(
            mcp_server.analyze_design(
                project_name="P",
                design_name="D",
                setup_name="S",
                pid=1,
                blocking=True,
                blocking_timeout_s=1.5,
                blocking_poll_interval_s=1.0,
            )
        )
        self.assertEqual(result["blocking_status"], "timed_out")
        self.assertGreaterEqual(result["blocking_poll_count"], 2)

    def test_blocking_error(self):
        self.exec.side_effect = [{"started": True}, RuntimeError("boom")]
        result = _run(
            mcp_server.analyze_design(
                project_name="P",
                design_name="D",
                setup_name="S",
                pid=1,
                blocking=True,
                blocking_poll_interval_s=1.0,
            )
        )
        self.assertEqual(result["blocking_status"], "error")
        self.assertEqual(result["blocking_poll_count"], 1)

    def test_blocking_requires_names(self):
        with self.assertRaises(ValueError):
            _run(mcp_server.analyze_design(pid=1, blocking=True))

    def test_wait_for_solve_idle(self):
        self.exec.return_value = {"running": False}
        result = _run(
            mcp_server.wait_for_solve(project_name="P", design_name="D", pid=1)
        )
        self.assertEqual(result["status"], "idle")

    def test_wait_for_solve_error(self):
        self.exec.side_effect = RuntimeError("worker down")
        result = _run(
            mcp_server.wait_for_solve(project_name="P", design_name="D", pid=1)
        )
        self.assertEqual(result["status"], "error")


class FilesystemToolTests(unittest.TestCase):
    def test_verify_artifacts_all_error_codes(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            root = Path(tmp)
            missing = root / "nope.s1p"
            empty = root / "empty.s1p"
            empty.write_text("", encoding="utf-8")
            short = root / "short.s1p"
            short.write_text("!header\n", encoding="utf-8")
            narrow = root / "narrow.csv"
            narrow.write_text("freq,s11\n24,-10\n26,-11\n28,-12\n30,-13\n", encoding="utf-8")
            result = mcp_server.verify_artifacts(
                [str(missing), str(empty), str(short), str(narrow)],
                min_rows=1,
                freq_lo_ghz=24.0,
                freq_hi_ghz=32.0,
                freq_points_min=5,
            )
            self.assertIn("E_EXPORT_MISSING", result["files"][str(missing.resolve())]["errors"])
            self.assertIn("E_EXPORT_EMPTY", result["files"][str(empty.resolve())]["errors"])
            self.assertIn("E_EXPORT_EMPTY", result["files"][str(short.resolve())]["errors"])
            self.assertIn("E_FREQ_RANGE_HIGH", result["files"][str(narrow.resolve())]["errors"])
            self.assertIn("E_FREQ_POINTS", result["files"][str(narrow.resolve())]["errors"])
            self.assertFalse(result["all_ok"])

    def test_verify_artifacts_ok(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            good = Path(tmp) / "good.s1p"
            good.write_text(
                "# GHz S MA R 50\n24 -10 0\n26 -11 0\n28 -12 0\n30 -13 0\n32 -14 0\n",
                encoding="utf-8",
            )
            result = mcp_server.verify_artifacts(
                [str(good)],
                freq_lo_ghz=24.0,
                freq_hi_ghz=32.0,
                freq_points_min=5,
            )
            self.assertTrue(result["all_ok"])

    def test_clear_stale_lock_owner_alive_refused(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            proj = Path(tmp) / "P.aedt"
            lock = Path(str(proj) + ".lock")
            lock.write_text("owner pid 123456", encoding="utf-8")
            with mock.patch("psutil.pid_exists", return_value=True):
                result = mcp_server.clear_stale_lock(str(proj))
            self.assertFalse(result["cleared"])
            self.assertEqual(result["reason"], "owner_alive")
            self.assertTrue(lock.exists())

    def test_clear_stale_lock_dead_owner_removed(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            proj = Path(tmp) / "P.aedt"
            lock = Path(str(proj) + ".lock")
            lock.write_text("owner pid 123456", encoding="utf-8")
            with mock.patch("psutil.pid_exists", return_value=False):
                result = mcp_server.clear_stale_lock(str(proj))
            self.assertTrue(result["cleared"])
            self.assertFalse(lock.exists())

    def test_clear_stale_lock_unknown_owner_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            proj = Path(tmp) / "P.aedt"
            lock = Path(str(proj) + ".lock")
            lock.write_text("no pid here", encoding="utf-8")
            result = mcp_server.clear_stale_lock(str(proj))
            self.assertFalse(result["cleared"])
            self.assertEqual(result["reason"], "unknown_owner")
            forced = mcp_server.clear_stale_lock(str(proj), force=True)
            self.assertTrue(forced["cleared"])

    def test_campaign_lock_acquire_release(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            first = mcp_server.acquire_campaign_lock(tmp, holder="agent-a")
            self.assertTrue(first["acquired"])
            second = mcp_server.acquire_campaign_lock(tmp, holder="agent-b")
            self.assertFalse(second["acquired"])
            self.assertEqual(second["reason"], "held_by_other")
            wrong = mcp_server.release_campaign_lock(tmp, holder="agent-b")
            self.assertFalse(wrong["released"])
            self.assertEqual(wrong["reason"], "holder_mismatch")
            right = mcp_server.release_campaign_lock(tmp, holder="agent-a")
            self.assertTrue(right["released"])

    def test_clear_stale_lock_parses_aedt_metadata(self):
        # Real AEDT lock format: "$begin 'ConnectionParameters' DesktopProcessID=740 ..."
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            proj = Path(tmp) / "P.aedt"
            lock = Path(str(proj) + ".lock")
            lock.write_text(
                "$begin 'ConnectionParameters'\r\n"
                "\tDesktopProcessID=740\r\n"
                "\tDesktopProcessPath='D:\\\\ANSYS Inc\\\\v261\\\\AnsysEM\\\\ansysedt.exe'\r\n"
                "\tGRPCPort=59141\r\n"
                "$end 'ConnectionParameters'\r\n",
                encoding="utf-8",
            )
            with mock.patch("psutil.pid_exists", return_value=True):
                held = mcp_server.clear_stale_lock(str(proj))
            self.assertFalse(held["cleared"])
            self.assertEqual(held["reason"], "owner_alive")
            self.assertEqual(held["owner_pid"], 740)
            self.assertEqual(held["pid_source"], "lock_metadata")
            with mock.patch("psutil.pid_exists", return_value=False):
                cleaned = mcp_server.clear_stale_lock(str(proj))
            self.assertTrue(cleaned["cleared"])


class ProbeRobustnessTests(unittest.TestCase):
    """Vendor internals may raise for Terminal/mixed designs; probes must stay
    inconclusive instead of failing the whole tool call."""

    class _ExplodingApp(FakeApp):
        @property
        def setups(self):
            raise KeyError("HFSS Terminal Network")

    def test_preflight_survives_setups_keyerror(self):
        app = self._ExplodingApp()
        backend = PyAedtBackend(
            desktop_factory=lambda **kwargs: FakeDesktop(**kwargs),
            hfss_factory=lambda **kwargs: app,
            version="2026.1",
        )
        result = backend.execute(
            AedtTarget("port", 50051),
            "preflight_design",
            {"project_name": "P", "design_name": "D", "setup_name": "Setup1"},
        )
        self.assertTrue(result["validation_passed"])
        self.assertIsNone(result["solution_data_present_before_submit"])

    def test_finalize_with_explicit_setups_survives_setups_keyerror(self):
        app = self._ExplodingApp()
        backend = PyAedtBackend(
            desktop_factory=lambda **kwargs: FakeDesktop(**kwargs),
            hfss_factory=lambda **kwargs: app,
            version="2026.1",
        )
        result = backend.execute(
            AedtTarget("port", 50051),
            "finalize_solve",
            {"project_name": "P", "design_name": "D", "setups": ["Setup1"]},
        )
        self.assertTrue(result["saved"])
        self.assertIn("Setup1", result["setup_solution_data"])

    def test_finalize_without_setups_reports_actionable_error(self):
        app = self._ExplodingApp()
        backend = PyAedtBackend(
            desktop_factory=lambda **kwargs: FakeDesktop(**kwargs),
            hfss_factory=lambda **kwargs: app,
            version="2026.1",
        )
        with self.assertRaises(BackendCommandError) as ctx:
            backend.execute(
                AedtTarget("port", 50051),
                "finalize_solve",
                {"project_name": "P", "design_name": "D"},
            )
        self.assertIn("pass setups=", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
