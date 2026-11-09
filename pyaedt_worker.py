from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Callable


def _bridge_configured_aedt_install() -> None:
    """Expose the configured AEDT install using PyAEDT's expected env name."""
    install_dir = os.environ.get("AEDT_INSTALL_DIR")
    version = os.environ.get("AEDT_VERSION")
    if not install_dir or not version:
        return
    try:
        year, release = version.split(".", 1)
        version_code = f"{year[-2:]}{int(release)}"
    except (TypeError, ValueError):
        return
    os.environ.setdefault(f"ANSYSEM_ROOT{version_code}", install_dir)


_bridge_configured_aedt_install()


def windows_app_control_warning() -> str | None:
    """Return a warning when Smart App Control is enforced on this machine.

    PyAEDT's gRPC plugin loads ``PyDesktopPlugin.dll`` (signed by ANSYS Inc.)
    from the AEDT install directory; that library depends on
    ``DesktopProxyLib.dll``, which is unsigned. Smart App Control (and some
    WDAC policies) can refuse that dependency with ``0xC0E90002`` /
    "application control policy blocked this file", which then surfaces as an
    unrelated gRPC failure. Detect the policy early and explain it once.
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\CI\Policy",
        ) as key:
            state, _ = winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")
    except Exception:
        return None
    try:
        enforced = int(state) == 1
    except (TypeError, ValueError):
        return None
    if not enforced:
        return None
    return (
        "[aedt-hardening] Smart App Control is ON (VerifiedAndReputablePolicyState=1). "
        "SAC may block the unsigned Ansys dependency DesktopProxyLib.dll "
        "(error 0xC0E90002 / 'application control policy has blocked this file'), "
        "after which AEDT calls fail with unrelated gRPC errors. "
        "See docs/WINDOWS_APP_CONTROL.md for diagnosis and options."
    )


_APP_CONTROL_WARNING = windows_app_control_warning()
if _APP_CONTROL_WARNING:
    print(_APP_CONTROL_WARNING, file=sys.stderr, flush=True)

# Install the project guard BEFORE anything can construct a PyAEDT application.
# It rewrites a bare project name to the absolute path of the existing .aedt so
# PyAEDT opens it instead of creating and overwriting it. See aedt_project_guard.py.
# The broker's stdout carries the JSON protocol, so the guard captures whatever
# PyAEDT prints on import and re-emits it on stderr.
try:
    import aedt_project_guard

    aedt_project_guard.install()
except Exception as _guard_exc:
    print("[aedt-guard] install failed: %s" % _guard_exc, file=sys.stderr, flush=True)
    raise RuntimeError('Project guard unavailable; refusing unguarded AEDT operations') from _guard_exc

from aedt_close_watcher import AedtCloseWatcher
from pyaedt_backend import BackendCommandError, PyAedtBackend
from worker_protocol import WorkerProtocolError, WorkerRequest, WorkerResponse


def run_request_line(
    payload: str,
    *,
    backend_factory: Callable[[], Any] = PyAedtBackend,
) -> tuple[WorkerResponse, int]:
    request_id = "unknown"
    try:
        request = WorkerRequest.from_json(payload.strip())
        request_id = request.request_id
    except WorkerProtocolError as exc:
        return WorkerResponse.failure(request_id, "invalid_request", str(exc)), 2

    try:
        backend = backend_factory()
        result = backend.execute(request.target, request.command, request.arguments)
        return WorkerResponse.success(request.request_id, result), 0
    except BackendCommandError as exc:
        return WorkerResponse.failure(request.request_id, "invalid_command", str(exc)), 1
    except Exception as exc:
        return WorkerResponse.failure(request.request_id, "backend_error", str(exc)), 1


def handle_close_intent(
    backend: Any,
    reason: str,
    *,
    exit_fn: Callable[[int], Any] = os._exit,
) -> None:
    sys.stderr.write(f"AEDT close intent detected: {reason}\n")
    try:
        backend.close_for_user_request()
    finally:
        sys.stderr.flush()
        exit_fn(0)


def run_stream(
    input_stream: Any,
    output_stream: Any,
    *,
    backend_factory: Callable[[], Any] = PyAedtBackend,
) -> int:
    backend = backend_factory()
    watcher = AedtCloseWatcher(
        session_pid=lambda: getattr(backend, "session_pid", None),
        on_close_intent=lambda reason: handle_close_intent(backend, reason),
    )
    watcher.start()
    released = False
    try:
        for payload in input_stream:
            if not payload.strip():
                continue
            try:
                request = WorkerRequest.from_json(payload.strip())
            except WorkerProtocolError as exc:
                response = WorkerResponse.failure("unknown", "invalid_request", str(exc))
            else:
                if request.command == "release_connection":
                    try:
                        with contextlib.redirect_stdout(sys.stderr):
                            released = bool(backend.release())
                        response = WorkerResponse.success(
                            request.request_id,
                            {"released": released},
                        )
                    except Exception as exc:
                        response = WorkerResponse.failure(
                            request.request_id,
                            "backend_error",
                            str(exc),
                        )
                    output_stream.write(response.to_json() + "\n")
                    output_stream.flush()
                    return 0

                with contextlib.redirect_stdout(sys.stderr):
                    response, _ = run_request_line(
                        payload,
                        backend_factory=lambda: backend,
                    )

            output_stream.write(response.to_json() + "\n")
            output_stream.flush()
        return 0
    finally:
        watcher.stop()
        if not released:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    backend.release()
            except Exception as exc:
                sys.stderr.write(f"AEDT broker release failed: {exc}\n")
        # v1.5: never let a hard exit lose buffered protocol/log output.
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass


def main() -> int:
    payload = sys.stdin.read()
    with contextlib.redirect_stdout(sys.stderr):
        response, exit_code = run_request_line(payload)
    sys.stdout.write(response.to_json() + "\n")
    sys.stdout.flush()
    return exit_code


def exit_without_pyaedt_cleanup(
    exit_code: int,
    *,
    exit_fn: Callable[[int], Any] = os._exit,
) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    exit_fn(exit_code)


if __name__ == "__main__":
    if "--serve" in sys.argv[1:]:
        raise SystemExit(run_stream(sys.stdin, sys.stdout))
    # Compatibility mode handles one request and intentionally skips PyAEDT's
    # process-exit callback. WorkerClient uses the persistent --serve mode.
    exit_without_pyaedt_cleanup(main())

