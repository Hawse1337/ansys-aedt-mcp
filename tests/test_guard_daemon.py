# -*- coding: utf-8 -*-
"""Offline tests for the resident guarded-run daemon (queue, recovery, singleton)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import mcp_server
from aedt_guard.daemon import DaemonPaths, RunDaemon
from aedt_guard.store import Store


def _metrics():
    return [
        {"id": "s11", "kind": "s11", "unit": "dB", "operator": "<=", "target": -10.0,
         "hard": True, "band_ghz": [24.0, 30.0]},
    ]


class FakeRunner:
    def __init__(self, *, outcome="COMPLETED", record=None):
        self.outcome = outcome
        self.record = record if record is not None else []
        self.resumed_from = None

    def run(self):
        self.record.append(("run",))
        return self.outcome

    def resume(self, last_stage):
        self.resumed_from = last_stage
        self.record.append(("resume", last_stage))
        return self.outcome


class FakeStore:
    def __init__(self, path):
        self.path = path
        self.status = {}

    def list_runs(self, project=None, limit=50):
        return [{"run_id": "stuck", "status": "EXPORTING"}]

    def get_run(self, run_id):
        return {
            "run_id": run_id,
            "status": "EXPORTING" if run_id == "stuck" else "CREATED",
            "params": {"project_name": "p", "design_name": "d", "setup_name": "Setup1"},
            "artifact_dir": None,
            "notes": json.dumps([{"stage": "SOLVED"}, {"stage": "EXPORTING"}]),
        }

    def close(self):
        pass


class DaemonHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = Path(self.tmp.name) / "camp"
        self.paths = DaemonPaths(self.campaign).ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_roundtrip(self):
        self.paths.write_request({"run_id": "r1", "project_name": "p"})
        pending = self.paths.list_requests()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], "r1")
        self.paths.drop_request("r1")
        self.assertEqual(self.paths.list_requests(), [])

    def test_claim_is_exclusive_and_releases(self):
        self.assertTrue(self.paths.claim("r1"))
        self.assertFalse(self.paths.claim("r1"))
        self.paths.release("r1")
        self.assertTrue(self.paths.claim("r1"))

    def test_heartbeat_liveness(self):
        self.assertFalse(self.paths.alive()["alive"])
        self.paths.write_heartbeat()
        alive = self.paths.alive()
        self.assertTrue(alive["alive"])
        self.assertEqual(alive["pid"], os.getpid())

    def test_stale_heartbeat_is_dead(self):
        self.paths.write_heartbeat()
        info = json.loads(self.paths.heartbeat.read_text(encoding="utf-8"))
        info["updated_ts"] = info["updated_ts"] - 600
        self.paths.heartbeat.write_text(json.dumps(info), encoding="utf-8")
        state = self.paths.alive()
        self.assertFalse(state["alive"])
        self.assertEqual(state["reason"], "heartbeat_stale")

    def test_singleton_acquisition(self):
        self.assertTrue(self.paths.acquire_singleton())
        self.assertFalse(self.paths.acquire_singleton())
        self.paths.release_singleton()
        self.assertFalse(self.paths.lock.exists())


class RunDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = Path(self.tmp.name) / "camp"
        self.campaign.mkdir(parents=True, exist_ok=True)
        (self.campaign / "design_spec.json").write_text(
            json.dumps(mcp_server.new_spec("lens", "lens", [24.0, 30.0], _metrics())),
            encoding="utf-8",
        )
        self.records = []
        self.daemon = RunDaemon(
            self.campaign,
            store_factory=lambda path: FakeStore(path),
            spec_loader=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
            adapter_factory=lambda payload: {"payload": payload},
            runner_factory=lambda **kwargs: FakeRunner(record=self.records),
            project="lens",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_poll_once_runs_and_clears_request(self):
        self.daemon.paths.write_request({
            "run_id": "r1", "project_name": "p", "design_name": "d",
            "setup_name": "Setup1", "params": {}, "cuts": [],
        })
        result = self.daemon.poll_once()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.records, [("run",)])
        self.assertEqual(self.daemon.paths.list_requests(), [])
        self.assertFalse((self.daemon.paths.claims / "r1.json").exists())

    def test_poll_once_returns_none_when_idle(self):
        self.assertIsNone(self.daemon.poll_once())

    def test_recover_resumes_non_terminal_run_without_solving(self):
        recovered = self.daemon.recover()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["resumed_from"], "EXPORTING")
        self.assertEqual(self.records, [("resume", "EXPORTING")])

    def test_claim_blocks_second_daemon(self):
        self.daemon.paths.write_request({"run_id": "r1", "project_name": "p"})
        self.daemon.paths.claim("r1")
        result = self.daemon.poll_once()
        self.assertEqual(result["status"], "skipped_claimed")
        self.assertEqual(self.records, [])


class EnqueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.campaign = str(Path(self.tmp.name) / "camp")
        mcp_server.spec_create(self.campaign, "lens", "metal_lens", [24.0, 30.0], _metrics())

    def tearDown(self):
        os.environ.pop("AEDT_GUARD_NO_DAEMON", None)
        for _ in range(10):
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                time.sleep(0.2)

    def test_submit_enqueues_for_the_daemon(self):
        os.environ.pop("AEDT_GUARD_NO_DAEMON", None)
        with mock.patch.object(mcp_server, "_ensure_daemon", return_value=True):
            response = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0, num_cores=2,
            )
        self.assertEqual(response["status"], "QUEUED")
        self.assertEqual(response["executor"], "daemon")
        request_path = Path(self.campaign) / "daemon" / "requests" / f"{response['run_id']}.json"
        self.assertTrue(request_path.is_file())
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], response["run_id"])
        self.assertEqual(payload["pid"], 1)
        self.assertEqual(payload["solver_settings"]["effective"]["num_cores"], 2)
        self.assertTrue(Path(payload["run_token"]).is_file())

    def test_submit_falls_back_to_thread_without_daemon(self):
        os.environ["AEDT_GUARD_NO_DAEMON"] = "1"

        def fake_execute(target, command, arguments, timeout=None):
            if command == "preflight_design":
                return {"validation_passed": True, "ff_sphere_present": True}
            if command == "analyze_design":
                return {"started": True}
            if command == "analysis_status":
                return {"running": False}
            if command == "finalize_solve":
                return {"saved": True, "solution_data_complete": True}
            if command == "export_results":
                path = Path(arguments["output_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                if arguments.get("export_type") == "convergence":
                    path.write_text("conv", encoding="utf-8")
                    return {"path": str(path), "evidence": {"assessment": "converged"}}
                path.write_text("# GHz S MA R 50\n24 0.1 0\n25 0.1 0\n26 0.1 0\n", encoding="utf-8")
                return {"path": str(path)}
            if command == "export_farfield_report":
                out = Path(arguments["output_dir"])
                theta = out / "t.csv"
                phi = out / "p.csv"
                for target_path in (theta, phi):
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    header = ('"Theta [deg]","Phi [deg]","dB(GainTotal) []"' if target_path is theta
                              else '"Phi [deg]","Theta [deg]","dB(GainTotal) []"')
                    rows = [header] + [f'{i},"0.0",5.0' for i in range(0, 40, 2)]
                    target_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {"cuts": {"theta_phi0": {"path": str(theta)},
                                 "phi_theta90": {"path": str(phi)}}}
            raise AssertionError(command)

        with mock.patch.object(mcp_server.worker_client, "execute", side_effect=fake_execute):
            response = mcp_server.submit_guarded_run(
                self.campaign, "lens", "Lens", "Setup1", pid=1,
                poll_interval_s=0.01, timeout_s=5.0,
            )
            self.assertEqual(response["status"], "RUNNING")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = mcp_server.get_run_status(self.campaign, response["run_id"])
                if status["terminal"]:
                    break
                time.sleep(0.05)
        os.environ.pop("AEDT_GUARD_NO_DAEMON", None)
        self.assertIn(status["status"], {"COMPLETED", "FAILED"})


if __name__ == "__main__":
    unittest.main()


class SingletonLockTests(unittest.TestCase):
    def test_singleton_is_exclusive(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            first = DaemonPaths(tmp)
            second = DaemonPaths(tmp)
            self.assertTrue(first.acquire_singleton())
            self.assertFalse(second.acquire_singleton())
            first.release_singleton()

    def test_dead_owner_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            paths = DaemonPaths(tmp).ensure()
            paths.lock.write_text(json.dumps({"pid": 999999, "at": 1.0}), encoding="utf-8")
            self.assertTrue(paths.acquire_singleton())
            paths.release_singleton()

    def test_live_owner_lock_is_respected(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            paths = DaemonPaths(tmp).ensure()
            paths.lock.write_text(json.dumps({"pid": os.getpid(), "at": __import__("time").time()}),
                                  encoding="utf-8")
            self.assertFalse(paths.acquire_singleton())


class RecoveryWaitTests(unittest.TestCase):
    def test_wait_is_capped_for_sticky_solver_state(self):
        from aedt_guard.daemon import RECOVERY_WAIT_S, RunDaemon

        seen = {}

        class StickyAdapter:
            def wait(self, timeout_s, poll_interval_s):
                seen["timeout"] = timeout_s
                return {"running": True, "timed_out": True}

        RunDaemon._wait_if_solving(StickyAdapter(), 1800.0, 5.0)
        self.assertLessEqual(seen["timeout"], RECOVERY_WAIT_S)

    def test_adapter_without_wait_is_ignored(self):
        from aedt_guard.daemon import RunDaemon

        RunDaemon._wait_if_solving(object(), 10.0, 1.0)  # must not raise

class AdapterFinalizeScopeTests(unittest.TestCase):
    def test_finalize_probes_only_the_run_setup(self):
        from aedt_guard.worker_adapter import GuardedAdapter

        calls = []

        class FakeClient:
            def execute(self, target, command, arguments, timeout=None):
                calls.append((command, dict(arguments)))
                return {"saved": True, "solution_data_complete": True}

        adapter = GuardedAdapter(
            pid=1, port=None, project_name="p", design_name="D",
            setup_name="B0_Swp", cuts=[], worker_timeout=5.0, client=FakeClient(),
        )
        result = adapter.finalize()
        self.assertTrue(result["saved"])
        self.assertEqual(calls[0][0], "finalize_solve")
        self.assertEqual(calls[0][1]["setups"], ["B0_Swp"])

class AdapterExtraSetupTests(unittest.TestCase):
    def _adapter(self, **extra):
        from aedt_guard.worker_adapter import GuardedAdapter

        calls = []

        class FakeClient:
            def execute(self, target, command, arguments, timeout=None):
                calls.append((command, dict(arguments)))
                return {"started": True, "running": False, "path": "x"}

        adapter = GuardedAdapter(
            pid=1, port=None, project_name="p", design_name="D",
            setup_name="B0_Swp", cuts=[], worker_timeout=5.0, client=FakeClient(),
            **extra,
        )
        return adapter, calls

    def test_farfield_uses_its_own_setup(self):
        adapter, calls = self._adapter(farfield_setup="FF_Swp")
        adapter.export_farfield(Path("out"), ["theta_phi0"])
        self.assertEqual(calls[0][0], "export_farfield_report")
        self.assertEqual(calls[0][1]["setup_name"], "FF_Swp")

    def test_extra_setup_is_solved_explicitly(self):
        adapter, calls = self._adapter(extra_setups=["FF_Swp"])
        adapter.analyze_setup("FF_Swp")
        adapter.wait_setup("FF_Swp", 1.0, 0.1)
        self.assertEqual(calls[0][0], "analyze_design")
        self.assertEqual(calls[0][1]["setup_name"], "FF_Swp")
        self.assertEqual(calls[1][0], "analysis_status")
        self.assertEqual(calls[1][1]["setup_name"], "FF_Swp")

class ExportRetryTests(unittest.TestCase):
    def test_transient_export_failure_is_retried(self):
        from aedt_guard.sim import _export_with_retry

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ExportToFile failed")
            return {"ok": True}

        self.assertEqual(_export_with_retry(flaky, attempts=2, delay_s=0.0), {"ok": True})
        self.assertEqual(calls["n"], 2)

    def test_persistent_failure_is_raised(self):
        from aedt_guard.sim import _export_with_retry

        def broken():
            raise RuntimeError("CreateReport failed")

        with self.assertRaises(RuntimeError):
            _export_with_retry(broken, attempts=2, delay_s=0.0)


class ExportReconnectTests(unittest.TestCase):
    def test_reconnect_happens_before_retry(self):
        from aedt_guard.sim import _export_with_retry

        events = []

        def flaky():
            events.append("export")
            if events.count("export") == 1:
                raise RuntimeError("GetSetups failed")
            return {"ok": True}

        def reconnect():
            events.append("reconnect")

        self.assertEqual(
            _export_with_retry(flaky, attempts=2, delay_s=0.0, on_retry=reconnect),
            {"ok": True},
        )
        self.assertEqual(events, ["export", "reconnect", "export"])

    def test_adapter_reset_calls_disconnect(self):
        from aedt_guard.worker_adapter import GuardedAdapter

        calls = []

        class FakeClient:
            def execute(self, target, command, arguments, timeout=None):
                calls.append(command)
                return {"disconnected": True}

        adapter = GuardedAdapter(pid=1, port=None, project_name="p", design_name="D",
                                 setup_name="S", cuts=[], worker_timeout=5.0,
                                 client=FakeClient())
        adapter.reset_connection()
        self.assertEqual(calls, ["disconnect_from_aedt"])

class SingletonLongRunTests(unittest.TestCase):
    def test_live_owner_is_not_taken_over_after_a_long_run(self):
        import time as _time

        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            paths = DaemonPaths(tmp).ensure()
            # owner alive, lock written 300 s ago: a 30-minute solve must not
            # hand the campaign to a second daemon
            paths.lock.write_text(
                json.dumps({"pid": os.getpid(), "at": _time.time() - 300.0}),
                encoding="utf-8",
            )
            self.assertFalse(paths.acquire_singleton())

class FarfieldSweepScopeTests(unittest.TestCase):
    def _adapter(self, **extra):
        from aedt_guard.worker_adapter import GuardedAdapter

        calls = []

        class FakeClient:
            def execute(self, target, command, arguments, timeout=None):
                calls.append((command, dict(arguments)))
                return {"cuts": {}}

        adapter = GuardedAdapter(pid=1, port=None, project_name="p", design_name="D",
                                 setup_name="B0_Swp", cuts=[], worker_timeout=5.0,
                                 sweep_name="Swp_fast", client=FakeClient(), **extra)
        return adapter, calls

    def test_separate_farfield_setup_does_not_inherit_the_s11_sweep(self):
        adapter, calls = self._adapter(farfield_setup="FF_Swp")
        adapter.export_farfield(Path("out"), ["theta_phi0"], frequencies=[28.0])
        self.assertNotIn("sweep_name", calls[0][1])
        self.assertEqual(calls[0][1]["setup_name"], "FF_Swp")

    def test_same_setup_keeps_the_sweep(self):
        adapter, calls = self._adapter()
        adapter.export_farfield(Path("out"), ["theta_phi0"], frequencies=[28.0])
        self.assertEqual(calls[0][1]["sweep_name"], "Swp_fast")
