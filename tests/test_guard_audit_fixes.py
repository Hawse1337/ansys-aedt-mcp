# -*- coding: utf-8 -*-
"""Audit-driven regression tests: lock audit, .aedb resolution, release failures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

import mcp_server
from aedt_target import AedtTarget
from pyaedt_backend import PyAedtBackend
from pyaedt_capabilities import OfficialCapabilities


def _backend(desktop=None) -> PyAedtBackend:
    backend = PyAedtBackend(desktop_factory=lambda **_: None, version="2026.1")
    backend._desktop = desktop
    return backend


def test_clear_stale_lock_is_audited(tmp_path: Path):
    project = tmp_path / "audited.aedt"
    project.write_text("{}", encoding="utf-8")
    (tmp_path / "audited.aedt.lock").write_text(
        "$begin 'ConnectionParameters'\nDesktopProcessID=999999\n$end 'ConnectionParameters'\n",
        encoding="utf-8",
    )
    result = mcp_server.clear_stale_lock(str(project))
    assert result["cleared"] is True
    audit = Path(mcp_server.managed_sessions.state_dir()) / "guard_audit.jsonl"
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    lock_records = [r for r in records if r.get("command") == "clear_stale_lock"]
    assert lock_records
    assert lock_records[-1]["allowed"] is True
    detail = json.loads(lock_records[-1]["detail"])
    assert detail["owner_pid"] == 999999


def test_aedb_directory_path_resolves(tmp_path: Path):
    aedb = tmp_path / "layout.aedb"
    aedb.mkdir()
    backend = _backend(SimpleNamespace(aedt_process_id=4242, port=5566))
    desktop = backend._desktop
    assert backend._resolve_project_argument(desktop, str(aedb)) == str(aedb.resolve())
    assert backend._resolve_project_argument(desktop, str(aedb), prefer_name=True) == "layout"


def test_create_design_path_is_preserved_by_normalizer(tmp_path: Path):
    project = tmp_path / "keep.aedt"
    project.write_text("{}", encoding="utf-8")
    desktop = SimpleNamespace(aedt_process_id=4242, port=5566)
    backend = _backend(desktop)
    normalized = backend._normalize_arguments(
        desktop, {"project_name": str(project)}, prefer_name=False
    )
    assert normalized["project_name"] == str(project.resolve())


def test_disconnect_release_error_is_reported(monkeypatch):
    class _Desktop:
        are_there_simulations_running = False
        aedt_process_id = 4321

        def release_desktop(self, close_projects: bool = False, close_on_exit: bool = False) -> bool:
            raise RuntimeError("vendor release failed")

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(
        create_time=lambda: 100.0, name=lambda: "fixture.exe"))
    result = OfficialCapabilities()._disconnect_from_aedt(
        _Desktop(),
        AedtTarget(kind="pid", value=4321),
        {"close_desktop": True},
        {},
    )
    assert result["release_error"] == "RuntimeError('vendor release failed')"
    assert result["closed_desktop"] is False
    assert result["forced_termination"] is False
    assert result["pid"] == 4321
