# -*- coding: utf-8 -*-
"""Close-verification tests (audit finding: the pid_reused/unknown-identity paths were untested)."""

from __future__ import annotations

import psutil
import pytest

from aedt_target import AedtTarget
from pyaedt_capabilities import OfficialCapabilities


class _FakeDesktop:
    are_there_simulations_running = False
    def __init__(self, pid: int = 4321) -> None:
        self.aedt_process_id = pid

    def release_desktop(self, close_projects: bool = False, close_on_exit: bool = False) -> bool:
        return True


class _Proc:
    def __init__(self, create_time: float, name: str = "ansysedt.exe") -> None:
        self._create_time = create_time
        self._name = name

    def create_time(self) -> float:
        return self._create_time

    def name(self) -> str:
        return self._name


def _disconnect(monkeypatch, processes, pid_exists, sleep=False):
    iterator = iter(processes)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid_exists)
    monkeypatch.setattr(psutil, "Process", lambda pid: next(iterator))
    if sleep:
        import time

        monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return OfficialCapabilities()._disconnect_from_aedt(
        _FakeDesktop(),
        AedtTarget(kind="pid", value=4321),
        {"close_desktop": True},
        {},
    )


def test_disconnect_reports_closed_when_process_exits(monkeypatch):
    result = _disconnect(monkeypatch, [_Proc(100.0)], pid_exists=False)
    assert result["closed_desktop"] is True
    assert result["process_alive"] is False
    assert result["forced_termination"] is False


def test_disconnect_does_not_kill_when_identity_snapshot_failed(monkeypatch):
    class _NoIdentity(_Proc):
        def create_time(self) -> float:
            raise RuntimeError("process vanished")

    result = _disconnect(monkeypatch, [_NoIdentity(0.0)], pid_exists=True, sleep=True)
    assert result["closed_desktop"] is False
    assert result["process_alive"] is True
    assert result["forced_termination"] is False


def test_disconnect_does_not_kill_a_recycled_pid(monkeypatch):
    result = _disconnect(
        monkeypatch,
        [_Proc(100.0), _Proc(500.0)],
        pid_exists=True,
        sleep=True,
    )
    assert result["pid_reused"] is True
    assert result["closed_desktop"] is True
    assert result["forced_termination"] is False
