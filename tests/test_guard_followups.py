# -*- coding: utf-8 -*-
"""Regression tests for the guard fixes found in the second agent's session and the audit."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aedt_target import AedtTarget
from pyaedt_backend import PyAedtBackend


def _backend(desktop=None) -> PyAedtBackend:
    backend = PyAedtBackend(desktop_factory=lambda **_: None, version="2026.1")
    backend._desktop = desktop
    return backend


class _FakeDesktop:
    """Shape of PyAEDT 1.10 Desktop project APIs, not the old test-only shortcut."""

    def __init__(self, active: str = "probe", path: str = r"C:\work\probe.aedt") -> None:
        self.aedt_process_id = 4242
        self.port = 5566
        self._active = active
        self._path = path

    @property
    def active_project_name(self) -> str:
        return self._active

    def project_path(self, project_name: str | None = None) -> str | None:
        if project_name is None or project_name == self._active:
            return self._path
        return None


def test_pid_target_uses_verified_port_and_keeps_version():
    desktop = SimpleNamespace(aedt_process_id=4242, port=5566)
    backend = _backend(desktop)
    kwargs = backend._connection_kwargs(AedtTarget(kind="pid", value=4242))
    assert kwargs["port"] == 5566
    assert "aedt_process_id" not in kwargs
    assert kwargs["version"] == "2026.1"


def test_port_target_keeps_port_and_version():
    backend = _backend(SimpleNamespace(aedt_process_id=4242, port=5566))
    kwargs = backend._connection_kwargs(AedtTarget(kind="port", value=5566))
    assert kwargs["port"] == 5566
    assert kwargs["version"] == "2026.1"


def test_realistic_open_project_name_resolves_to_path_for_factory_and_name_for_app_resolver():
    desktop = _FakeDesktop()
    backend = _backend(desktop)
    assert backend._desktop_project_file(desktop) == r"C:\work\probe.aedt"
    assert backend._resolve_project_argument(desktop, "probe") == r"C:\work\probe.aedt"
    assert backend._resolve_project_argument(desktop, "probe", prefer_name=True) == "probe"
    normalized = backend._normalize_arguments(desktop, {"project_name": "probe"}, prefer_name=True)
    assert normalized["project_name"] == "probe"


def test_absolute_project_path_prefers_stem_for_app_resolver(tmp_path: Path):
    project = tmp_path / "demo.aedt"
    project.write_text("{}", encoding="utf-8")
    desktop = _FakeDesktop()
    backend = _backend(desktop)
    # The guard/path factory keeps the path (see create_design); the app resolver gets the name.
    assert backend._resolve_project_argument(desktop, str(project)) == str(project)
    assert backend._resolve_project_argument(desktop, str(project), prefer_name=True) == "demo"


def test_unopened_bare_name_is_left_for_the_project_guard():
    desktop = SimpleNamespace(aedt_process_id=4242, port=5566)
    backend = _backend(desktop)
    assert backend._resolve_project_argument(desktop, "unknown_project") == "unknown_project"
