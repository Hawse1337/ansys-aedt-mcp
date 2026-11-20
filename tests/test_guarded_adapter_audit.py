"""Guarded-run adapter commands must land in the guard-wide audit log."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aedt_guard.worker_adapter import GuardedAdapter


class AdapterAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"AEDT_MCP_STATE_DIR": str(self.state)}, clear=False)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_adapter_command_is_audited(self) -> None:
        class FakeClient:
            def execute(self, target, command, arguments, timeout=None):
                return {"ok": True}

        adapter = GuardedAdapter(
            pid=123, port=None, project_name="p", design_name="d",
            setup_name="Setup1", cuts=[], worker_timeout=1, client=FakeClient(),
        )
        adapter.preflight()
        lines = (self.state / "guard_audit.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["command"], "preflight_design")
        self.assertEqual(entry["source"], "guarded_adapter")
        self.assertTrue(entry["allowed"])


if __name__ == "__main__":
    unittest.main()
