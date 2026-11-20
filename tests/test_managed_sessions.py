"""Managed-session enforcement, audit log and guard_status contract tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aedt_guard import managed
from pyaedt_backend import AedtTarget, BackendCommandError, PyAedtBackend


class ManagedRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"AEDT_MCP_STATE_DIR": str(self.state)}, clear=False)
        self._env.start()
        os.environ.pop("AEDT_MCP_ALLOW_UNMANAGED", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_record_launch_is_visible_in_registry(self) -> None:
        with mock.patch.object(managed, "_process_identity", return_value=("ansysedt.exe", 1234.5)):
            record = managed.record_launch(pid=4242, port=5001, version="2026.1", source="test")
        self.assertEqual(record["pid"], 4242)
        self.assertEqual(record["port"], 5001)
        raw = json.loads((self.state / managed.REGISTRY_NAME).read_text(encoding="utf-8"))
        self.assertEqual(raw["sessions"][0]["source"], "test")

    def test_enforce_target_refuses_unmanaged_pid(self) -> None:
        target = AedtTarget("pid", 999999)
        with self.assertRaisesRegex(BackendCommandError, "not launched by this guard"):
            managed.enforce_target(target)

    def test_enforce_target_accepts_managed_pid(self) -> None:
        target = AedtTarget("pid", 4242)
        with mock.patch.object(managed, "is_managed", return_value=True):
            managed.enforce_target(target)

    def test_escape_hatch_allows_unmanaged_attach(self) -> None:
        with mock.patch.dict(os.environ, {"AEDT_MCP_ALLOW_UNMANAGED": "1"}):
            managed.enforce_target(AedtTarget("port", 5001))

    def test_scan_shape(self) -> None:
        scan = managed.scan_aedt_processes()
        self.assertIn("managed", scan)
        self.assertIn("unmanaged", scan)
        self.assertIn("enforcement", scan)


class BackendEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"AEDT_MCP_STATE_DIR": str(self.state)}, clear=False)
        self._env.start()
        os.environ.pop("AEDT_MCP_ALLOW_UNMANAGED", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_real_backend_refuses_unmanaged_attach_before_creating_desktop(self) -> None:
        backend = PyAedtBackend()  # no fake factory: real client path
        with mock.patch.object(managed, "is_managed", return_value=False):
            with self.assertRaises(BackendCommandError):
                backend._desktop_for(AedtTarget("pid", 123456))
        self.assertIsNone(backend._desktop)

    def test_fake_factory_backends_are_not_blocked(self) -> None:
        created: list[dict] = []

        def factory(**kwargs):
            created.append(kwargs)
            return object()

        backend = PyAedtBackend(desktop_factory=factory)
        backend._desktop_for(AedtTarget("pid", 123456))
        self.assertTrue(created)


class ArbitraryCodeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"AEDT_MCP_STATE_DIR": str(self.state)}, clear=False)
        self._env.start()
        os.environ.pop("AEDT_MCP_ALLOW_ARBITRARY_CODE", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_run_python_tools_are_refused_by_default(self) -> None:
        import asyncio

        import mcp_server

        code = asyncio.run(mcp_server.run_python_code(code="result = 1", pid=1))
        script = asyncio.run(mcp_server.run_python_script(script_path="C:/demo.py", pid=1))
        self.assertTrue(code["refused"])
        self.assertTrue(script["refused"])
        self.assertIn("operator", code["note"])
        self.assertNotIn("AEDT_MCP_ALLOW_ARBITRARY_CODE", code["note"])
        lines = (self.state / mcp_server.GUARD_AUDIT_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

    def test_opt_in_allows_the_tool(self) -> None:
        import asyncio
        from unittest import mock

        import mcp_server

        fake = mock.Mock()
        fake.execute_async = mock.AsyncMock(return_value={"executed": True})
        with mock.patch.object(mcp_server, "worker_client", fake), \
                mock.patch.dict(os.environ, {"AEDT_MCP_ALLOW_ARBITRARY_CODE": "1"}):
            result = asyncio.run(mcp_server.run_python_code(code="result = 1", pid=1))
        self.assertEqual(result, {"executed": True})


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_audit_line_is_written(self) -> None:
        import mcp_server

        with mock.patch.object(managed, "state_dir", return_value=self.state):
            mcp_server._audit_worker_call(
                "save_project", pid=1, port=None, arguments={"name": "secret-project"},
                outcome="refused", detail="unmanaged session",
            )
        lines = (self.state / mcp_server.GUARD_AUDIT_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["command"], "save_project")
        self.assertFalse(entry["allowed"])
        self.assertIn("unmanaged session", entry["detail"])
        self.assertNotIn("secret-project", lines[0])


if __name__ == "__main__":
    unittest.main()
