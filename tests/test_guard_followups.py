# -*- coding: utf-8 -*-
"""Regression tests for the guard fixes found in the second agent's session."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aedt_target import AedtTarget
from pyaedt_backend import PyAedtBackend


def _backend(desktop=None) -> PyAedtBackend:
    backend = PyAedtBackend(desktop_factory=lambda **_: None, version="2026.1")
    backend._desktop = desktop
    return backend


def test_pid_target_uses_verified_port_for_app_kwargs():
    desktop = SimpleNamespace(aedt_process_id=4242, port=5566)
    backend = _backend(desktop)
    kwargs = backend._connection_kwargs(AedtTarget(kind="pid", value=4242))
    assert kwargs["port"] == 5566
    assert "aedt_process_id" not in kwargs
    assert "version" not in kwargs


def test_port_target_keeps_port_only_kwargs():
    backend = _backend(SimpleNamespace(aedt_process_id=4242, port=5566))
    kwargs = backend._connection_kwargs(AedtTarget(kind="port", value=5566))
    assert kwargs["port"] == 5566
    assert "version" not in kwargs


def test_open_project_name_resolves_to_path_for_factory_and_name_for_app_resolver():
    desktop = SimpleNamespace(
        aedt_process_id=4242,
        port=5566,
        active_project="probe",
        project_file=r"C:\work\probe.aedt",
    )
    backend = _backend(desktop)
    assert backend._resolve_project_argument(desktop, "probe") == r"C:\work\probe.aedt"
    assert backend._resolve_project_argument(desktop, "probe", prefer_name=True) == "probe"
    normalized = backend._normalize_arguments(desktop, {"project_name": "probe"}, prefer_name=True)
    assert normalized["project_name"] == "probe"


def test_absolute_project_path_is_tracked(tmp_path: Path):
    project = tmp_path / "demo.aedt"
    project.write_text("{}", encoding="utf-8")
    desktop = SimpleNamespace(aedt_process_id=4242, port=5566)
    backend = _backend(desktop)
    assert backend._resolve_project_argument(desktop, str(project)) == str(project.resolve())
    assert backend._project_paths["demo"] == str(project.resolve())
    assert backend._resolve_project_argument(desktop, str(project), prefer_name=True) == "demo"


def test_unopened_bare_name_is_left_for_the_project_guard():
    desktop = SimpleNamespace(aedt_process_id=4242, port=5566)
    backend = _backend(desktop)
    assert backend._resolve_project_argument(desktop, "unknown_project") == "unknown_project"
