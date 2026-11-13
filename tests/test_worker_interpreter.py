"""The worker must run in the interpreter that has PyAEDT, not in the frozen application."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


class InterpreterChoiceTests(unittest.TestCase):
    def _client(self, **kwargs):
        from worker_client import WorkerClient

        return WorkerClient(**kwargs)

    def test_the_default_is_this_interpreter(self):
        os.environ.pop("AEDT_MCP_PYTHON", None)
        client = self._client()
        self.assertEqual(client.python_executable, Path(sys.executable))

    def test_the_environment_variable_overrides_it(self):
        os.environ["AEDT_MCP_PYTHON"] = r"C:\some\venv\Scripts\python.exe"
        try:
            client = self._client()
        finally:
            os.environ.pop("AEDT_MCP_PYTHON", None)
        self.assertEqual(client.python_executable, Path(r"C:\some\venv\Scripts\python.exe"))

    def test_an_explicit_argument_wins_over_the_environment(self):
        os.environ["AEDT_MCP_PYTHON"] = r"C:\env\python.exe"
        try:
            client = self._client(python_executable=r"C:\explicit\python.exe")
        finally:
            os.environ.pop("AEDT_MCP_PYTHON", None)
        self.assertEqual(client.python_executable, Path(r"C:\explicit\python.exe"))

    def test_an_empty_environment_variable_falls_back(self):
        os.environ["AEDT_MCP_PYTHON"] = ""
        try:
            client = self._client()
        finally:
            os.environ.pop("AEDT_MCP_PYTHON", None)
        self.assertEqual(client.python_executable, Path(sys.executable))


if __name__ == "__main__":
    unittest.main()
