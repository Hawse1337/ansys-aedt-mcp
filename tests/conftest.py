"""Redirect guard state into a temporary directory for every test run."""
from __future__ import annotations

import os
import tempfile


def pytest_configure(config):
    root = tempfile.mkdtemp(prefix="ansys-aedt-mcp-tests-")
    os.environ["AEDT_MCP_STATE_DIR"] = os.path.join(root, "state")
    os.environ["AEDT_CAMPAIGN_ROOT"] = os.path.join(root, "campaign")
    os.environ.setdefault("AEDT_GUARD_NO_DAEMON", "1")
    os.environ["AEDT_MCP_TEST_ALLOW_ANY_INSTALL_DIR"] = "1"