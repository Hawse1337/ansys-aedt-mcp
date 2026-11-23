from __future__ import annotations

import os
import sys
import math
import re
import socket
import subprocess
import time
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aedt_target import AedtTarget
from worker_client import WorkerClient


class AedtLaunchError(RuntimeError):
    pass


_VERSION_PATTERN = re.compile(r"^20(?P<year>\d{2})\.(?P<release>\d+)$")


def _version_root_variable(version: str) -> str:
    match = _VERSION_PATTERN.fullmatch(version)
    if not match:
        raise AedtLaunchError(
            f"invalid AEDT version {version!r}; expected a value such as 2026.1"
        )
    return f"ANSYSEM_ROOT{match.group('year')}{match.group('release')}"


def _trusted_aedt_roots(version: str) -> list[Path]:
    """Roots the launcher is allowed to start AEDT from.

    An explicit install_dir is still checked against these roots; a caller cannot
    point the guard at a renamed executable outside the configured product tree.
    """
    roots: list[Path] = []
    for name in ("AEDT_INSTALL_DIR", _version_root_variable(version)):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value).expanduser().resolve())
    for key, value in os.environ.items():
        if key.upper().startswith(("ANSYSEM_ROOT", "AWP_ROOT")) and value:
            roots.append(Path(value).expanduser().resolve())
    if not roots:
        # No explicit configuration: accept only the standard product layout.
        for base in (Path(r"C:\Program Files\ANSYS Inc"), Path(r"D:\ANSYS Inc")):
            if base.is_dir():
                roots.append(base.resolve())
    return roots


def resolve_aedt_executable(
    install_dir: str | Path | None,
    version: str = "2026.1",
) -> Path:
    candidates: list[Path] = []
    if install_dir:
        candidates.append(Path(install_dir))
    else:
        configured = os.environ.get("AEDT_INSTALL_DIR")
        if configured:
            candidates.append(Path(configured))
        root = os.environ.get(_version_root_variable(version))
        if root:
            candidates.append(Path(root))

    trusted_roots = _trusted_aedt_roots(version)
    for candidate in candidates:
        executable = (
            candidate
            if candidate.name.lower() == "ansysedt.exe"
            else candidate / "ansysedt.exe"
        )
        if executable.is_file():
            resolved = executable.resolve()
            if trusted_roots and not any(
                root == resolved.parent or root in resolved.parents for root in trusted_roots
            ):
                raise AedtLaunchError(
                    "refusing to launch %s: it is outside the configured AEDT roots %s. "
                    "Set AEDT_INSTALL_DIR/%s to the product installation."
                    % (resolved, [str(root) for root in trusted_roots],
                       _version_root_variable(version))
                )
            return resolved
    raise AedtLaunchError(
        "Cannot locate ansysedt.exe. Set AEDT_INSTALL_DIR or "
        f"{_version_root_variable(version)} to the AEDT {version} installation directory."
    )


def _choose_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


class AedtLauncher:
    def __init__(
        self,
        *,
        worker_client: WorkerClient | None = None,
        process_factory: Callable[..., Any] = subprocess.Popen,
        choose_free_port: Callable[[], int] = _choose_free_port,
        port_is_free: Callable[[int], bool] = _port_is_free,
        port_is_open: Callable[[int], bool] = _port_is_open,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._worker_client = worker_client or WorkerClient()
        self._process_factory = process_factory
        self._choose_free_port = choose_free_port
        self._port_is_free = port_is_free
        self._port_is_open = port_is_open
        self._monotonic = monotonic
        self._sleep = sleep
        self._launch_lock = threading.Lock()
        self._unfinished_launch: tuple[Any, int, str, bool] | None = None

    def launch(
        self,
        *,
        version: str = "2026.1",
        port: int = 0,
        install_dir: str | Path | None = None,
        non_graphical: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # A client retry must not fan out into concurrent desktop launches.
        if not self._launch_lock.acquire(blocking=False):
            raise AedtLaunchError("an AEDT launch is already in progress; do not retry concurrently")
        try:
            unfinished = self._unfinished_launch
            if unfinished is not None and unfinished[0].poll() is None:
                adopted = self._adopt_unfinished(timeout=timeout)
                if adopted is not None:
                    return adopted
                # A failed handshake left a guard-spawned AEDT that cannot be
                # adopted.  Release it automatically instead of asking the
                # operator to close the window by hand, then launch a clean one.
                self._release_unfinished("readiness handshake failed and adoption failed")
            return self._launch_once(version=version, port=port, install_dir=install_dir,
                                     non_graphical=non_graphical, timeout=timeout)
        except Exception:
            unfinished = self._unfinished_launch
            if unfinished is not None:
                process = unfinished[0]
                # Keep the spawn-time managed record while the process is alive.
                # This is what lets a subsequent PID attach pass managed-only
                # enforcement instead of deadlocking between "can't attach" and
                # "can't launch".
                if process.poll() is not None:
                    try:
                        from aedt_guard.managed import discard_launch

                        discard_launch(int(process.pid))
                    except Exception:  # noqa: BLE001 - cleanup must not hide the launch error
                        pass
                    self._unfinished_launch = None
            raise
        finally:
            self._launch_lock.release()

    def _adopt_unfinished(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        """Adopt a guard-spawned AEDT whose launch handshake timed out.

        The process was recorded as managed at spawn time.  If its gRPC port is
        now ready, verify identity and return it as a normal launch instead of
        leaving the caller with "can't attach, can't launch".
        """
        unfinished = self._unfinished_launch
        if unfinished is None:
            return None
        process, port, version, non_graphical = unfinished
        if process.poll() is not None:
            self._unfinished_launch = None
            return None
        if not self._port_is_open(port):
            return None
        remaining = 60.0 if timeout is None else min(max(5.0, float(timeout)), 120.0)
        try:
            info = self._worker_client.execute(
                AedtTarget("pid", int(process.pid)), "ping", {}, timeout=remaining
            )
            if not isinstance(info, dict) or info.get("connected") is not True:
                return None
            if str(info.get("aedt_version")) != version:
                return None
            if type(info.get("pid")) is not int or info["pid"] != process.pid:
                return None
            if type(info.get("port")) is not int or info["port"] != port:
                return None
        except Exception:  # noqa: BLE001 - adoption is best effort
            return None
        self._unfinished_launch = None
        try:
            from aedt_guard.managed import mark_verified, record_launch

            record_launch(pid=int(process.pid), port=port, version=version,
                          non_graphical=non_graphical, source="aedt_launcher.adopted")
            mark_verified(pid=int(process.pid))
        except Exception:  # noqa: BLE001 - bookkeeping must not hide a live session
            pass
        result: dict[str, Any] = {
            "pid": int(process.pid),
            "port": port,
            "version": version,
            "connection_mode": "grpc",
            "recovered": True,
            "recovery": "adopted after launch handshake timeout",
        }
        if non_graphical:
            result["non_graphical"] = True
        return result

    def _release_unfinished(self, reason: str) -> bool:
        """Terminate the guard-spawned process that could not be adopted."""
        unfinished = self._unfinished_launch
        if unfinished is None:
            return False
        process, _port, _version, _non_graphical = unfinished
        released = False
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    try:
                        process.kill()
                    except Exception:  # noqa: BLE001
                        pass
            released = True
        except Exception:  # noqa: BLE001 - cleanup is best effort
            released = False
        try:
            from aedt_guard.managed import discard_launch

            discard_launch(int(process.pid))
        except Exception:  # noqa: BLE001
            pass
        self._unfinished_launch = None
        return released

    def _launch_once(
        self, *, version: str, port: int, install_dir: str | Path | None,
        non_graphical: bool, timeout: float | None,
    ) -> dict[str, Any]:
        _version_root_variable(version)
        if type(port) is not int or port < 0 or port > 65535:
            raise AedtLaunchError(f"invalid gRPC port: {port!r}")
        if type(non_graphical) is not bool:
            raise AedtLaunchError("non_graphical must be a boolean")
        if self._unfinished_launch is not None:
            previous, previous_port, _previous_version, _previous_ng = self._unfinished_launch
            if previous.poll() is None:
                raise AedtLaunchError(
                    f"previous AEDT launch PID {previous.pid}, port {previous_port} is still running; "
                    "if this is a guard-spawned orphan, recover it with "
                    "disconnect_from_aedt(pid=%d, close_desktop=true) and retry launch_aedt; "
                    "do not ask the operator to close the AEDT window" % previous.pid
                )
            self._unfinished_launch = None
        selected_port = self._choose_free_port() if port == 0 else port
        if type(selected_port) is not int or not 0 < selected_port <= 65535:
            raise AedtLaunchError(f"invalid selected gRPC port: {selected_port!r}")
        if not self._port_is_free(selected_port):
            raise AedtLaunchError(f"gRPC port {selected_port} is already in use")

        raw_timeout = (
            timeout
            if timeout is not None
            else os.environ.get("AEDT_LAUNCH_TIMEOUT", "240")
        )
        if isinstance(raw_timeout, bool):
            raise AedtLaunchError("launch timeout must be positive")
        try:
            effective_timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise AedtLaunchError("launch timeout must be a positive number") from exc
        if not math.isfinite(effective_timeout) or effective_timeout <= 0:
            raise AedtLaunchError("launch timeout must be finite and positive")

        executable = resolve_aedt_executable(install_dir, version)
        command = [str(executable), "-grpcsrv", str(selected_port)]
        if non_graphical:
            command.append("-ng")
        deadline = self._monotonic() + effective_timeout
        process = self._process_factory(command, cwd=str(executable.parent))
        self._unfinished_launch = (process, selected_port, version, non_graphical)
        # Record ownership before the readiness handshake: the broker's managed-session
        # enforcement must see this PID as guard-launched, or its own probe is refused.
        try:
            from aedt_guard.managed import record_launch

            record_launch(pid=int(process.pid), port=selected_port, version=version,
                          non_graphical=non_graphical, source='aedt_launcher.spawn')
        except Exception:  # noqa: BLE001 - never fail the launch on bookkeeping
            pass
        # The launched PID is known even when psutil cannot enumerate the
        # listener owner. A port-only attach would fail on those hosts.
        target = AedtTarget("pid", int(process.pid))
        while self._monotonic() < deadline:
            returncode = process.poll()
            if returncode is not None:
                self._unfinished_launch = None
                raise AedtLaunchError(
                    f"AEDT exited with code {returncode} before gRPC was ready"
                )
            if self._port_is_open(selected_port):
                # Wait passively first, then make exactly one full-budget handshake.
                # Repeated Desktop()/broker probes can start new AEDT processes if
                # the vendor API falls back from an unsuccessful attach to a launch.
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                try:
                    info = self._worker_client.execute(
                        target,
                        "ping",
                        {},
                        timeout=remaining,
                    )
                    if not isinstance(info, dict) or info.get("connected") is not True:
                        raise ValueError("readiness probe did not confirm a connection")
                    if str(info.get("aedt_version")) != version:
                        raise ValueError(f"readiness probe returned a different AEDT version: {info.get('aedt_version')!r}")
                    if type(info.get("pid")) is not int or info["pid"] != process.pid:
                        raise ValueError(f"readiness probe PID {info.get('pid')!r} does not match launched PID {process.pid}")
                    if type(info.get("port")) is not int or info["port"] != selected_port:
                        raise ValueError(f"readiness probe port {info.get('port')!r} does not match requested gRPC port {selected_port}")
                    result = {
                        "pid": int(process.pid), "port": selected_port,
                        "version": version, "connection_mode": "grpc",
                    }
                    if non_graphical:
                        result["non_graphical"] = True
                    self._unfinished_launch = None
                    try:
                        from aedt_guard.managed import mark_verified, record_launch

                        record_launch(pid=result['pid'], port=result.get('port'),
                                      version=version, non_graphical=non_graphical,
                                      source='aedt_launcher')
                        mark_verified(pid=result['pid'])
                    except Exception:  # noqa: BLE001 - never fail a successful launch on bookkeeping
                        pass
                    return result
                except Exception as exc:  # noqa: BLE001 - vendor readiness errors vary
                    # In particular, do not kill a timed-out broker: its attach
                    # may still be pending. Preserve it for explicit recovery.
                    raise AedtLaunchError(
                        f"single readiness handshake failed for AEDT PID {process.pid}, port {selected_port}; "
                        f"no retry or termination was performed; inspect the existing session: {exc}"
                    ) from exc
            self._sleep(min(0.25, max(0.0, deadline - self._monotonic())))

        raise AedtLaunchError(
            f"timed out after {effective_timeout:g}s waiting for AEDT gRPC port {selected_port}; "
            f"AEDT PID {process.pid} was left running for inspection; no broker probe was started"
        )
