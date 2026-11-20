from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aedt_target import AedtTarget
from maxwell_capabilities import MAXWELL_COMMANDS, MaxwellCapabilities
from pyaedt_capabilities import (
    OFFICIAL_BACKEND_COMMANDS,
    CapabilityError,
    OfficialCapabilities,
)


class BackendCommandError(ValueError):
    pass


_DESKTOP_COMMANDS = {
    "ping",
    "project_info",
    "close_projects",
} | OFFICIAL_BACKEND_COMMANDS | MAXWELL_COMMANDS
_HFSS_COMMANDS = {
    "create_hfss_design",
    "start_analysis",
    "analysis_status",
    "build_wr90_waveguide",
    "abort_analysis",
    # v1.5 guarded-workflow extensions (additive)
    "preflight_design",
    "finalize_solve",
    "export_farfield_report",
}
_SOLUTION_TYPES = {
    "DrivenModal": "Modal",
    "DrivenTerminal": "Terminal",
    "Modal": "Modal",
    "Terminal": "Terminal",
    "SBR+": "SBR+",
    "Transient": "Transient",
    "Eigenmode": "Eigenmode",
}


_ATTACH_LOCK = threading.RLock()
_AUDITED_PYAEDT_VERSION = "1.10.0"


def _clear_design_reports(app: Any) -> int:
    """Delete every report plot in the *campaign copy* of the design; returns how many were removed.

    Inherited plots from the reference project are what makes PyAEDT's PostProcessor enumerate traces it
    cannot resolve, printing 'Solution is not a property name' into AEDT's message window on every
    measurement. The campaign project copy is disposable, so cleaning it is the honest place to stop it.
    """
    try:
        design = getattr(app, "odesign", None)
        if design is None:
            return 0
        reporter = design.GetModule("ReportSetup")
        names = [str(name) for name in (reporter.GetAllReportNames() or [])]
        if not names:
            return 0
        reporter.DeleteReports(names)
        return len(names)
    except Exception:  # noqa: BLE001 - cleanup must never fail a run
        return 0


def _drop_report(post: Any, name: str) -> None:
    """Best-effort deletion of a report the guard itself created.

    PyAEDT's PostProcessor walks every plot it finds and probes a 'Solution' property on each trace;
    a plot the guard leaves behind makes AEDT print a script-macro error on every later job. Deleting
    our own reports after exporting the CSV keeps that noise away and leaves user reports untouched.
    """
    delete = getattr(post, "delete_report", None)
    if not callable(delete):
        return
    try:
        delete(name)
    except Exception:  # noqa: BLE001 - cleanup must never fail a measurement
        pass


def _resolve_report_name(report: Any) -> str:
    """Return the identifier ``export_report_to_csv`` expects.

    PyAEDT returns different report classes per category: a ``FarField`` report
    (2026 R1) exposes ``plot_name`` and has no ``name`` attribute, so falling
    back to ``str(report)`` produces a label that makes ``ExportToFile`` fail.
    """
    if isinstance(report, str) and report.strip():
        # some PyAEDT versions return the report name directly
        return report.strip()
    if isinstance(report, (list, tuple)):
        for item in report:
            try:
                return _resolve_report_name(item)
            except BackendCommandError:
                continue
        raise BackendCommandError(
            "report list contained no resolvable report name"
        )
    for attribute in ("name", "plot_name", "internal_plot_name", "_saved_root_name"):
        try:
            value = getattr(report, attribute, None)
        except Exception:  # noqa: BLE001 - vendor properties can raise
            continue
        if isinstance(value, str) and value.strip():
            return value
    raise BackendCommandError(
        "could not resolve the report name (tried name/plot_name/"
        "internal_plot_name/_saved_root_name); got "
        f"{type(report).__name__}: {repr(report)[:160]}"
    )


def _existing_target_owner(kwargs: Mapping[str, Any]) -> tuple[int, float]:
    """Prove the explicit local target exists without invoking a vendor client."""
    import psutil

    pid, port = kwargs.get("aedt_process_id"), kwargs.get("port")
    if kwargs.get("new_desktop") is not False or bool(pid) == bool(port):
        raise BackendCommandError("attach requires new_desktop=False and exactly one explicit PID or port")
    if kwargs.get("machine") not in (None, "", "localhost", "127.0.0.1", "::1"):
        raise BackendCommandError("this attach-only broker supports local AEDT targets only")
    if pid:
        if type(pid) is not int or pid <= 0:
            raise BackendCommandError("invalid explicit AEDT PID")
    else:
        if type(port) is not int or not 0 < port <= 65535:
            raise BackendCommandError("invalid explicit AEDT port")
        try:
            owners = set()
            for connection in psutil.net_connections(kind="tcp"):
                address = connection.laddr
                address_port = getattr(address, "port", None)
                if address_port is None and isinstance(address, tuple) and len(address) >= 2:
                    address_port = address[1]
                if str(connection.status).upper() == "LISTEN" and address_port == port:
                    if type(connection.pid) is not int or connection.pid <= 0:
                        raise BackendCommandError("listener owner is unknown; use a verified explicit AEDT PID")
                    owners.add(connection.pid)
            if len(owners) != 1:
                raise BackendCommandError(
                    f"cannot prove a unique AEDT listener on port {port}; inspect sessions and use an explicit PID"
                )
            pid = owners.pop()
        except BackendCommandError:
            raise
        except Exception as exc:
            raise BackendCommandError("cannot inspect listener ownership; use a verified explicit AEDT PID") from exc
    try:
        process = psutil.Process(pid)
        if process.name().lower() not in {"ansysedt.exe", "ansysedt"}:
            raise BackendCommandError(f"PID {pid} does not belong to AEDT")
        return pid, process.create_time()
    except BackendCommandError:
        raise
    except Exception as exc:
        raise BackendCommandError(f"cannot verify existing AEDT PID {pid}; no attach or launch was attempted") from exc


def _audited_desktop_module() -> Any:
    from importlib.metadata import version
    if version("pyaedt") != _AUDITED_PYAEDT_VERSION:
        raise BackendCommandError(
            f"attach-only protection was audited for PyAEDT {_AUDITED_PYAEDT_VERSION}; "
            "use that version or review the changed Desktop launch paths before enabling another version"
        )
    import ansys.aedt.core.desktop as desktop_module
    return desktop_module


def _pid_launch_hints(pid: int) -> tuple[int | None, bool]:
    """Read the verified process's command line, without treating it as readiness.

    Windows socket enumeration may omit an AEDT listener. The explicit PID and
    its -grpcsrv argument let us make one attach-only handshake; the native
    result must still report the same PID and port before this factory returns.
    """
    import psutil
    try:
        command_line = psutil.Process(pid).cmdline()
    except Exception:
        return None, False
    if not isinstance(command_line, (tuple, list)):
        return None, False
    non_graphical = any(isinstance(value,str) and value.lower() == "-ng" for value in command_line)
    for index, argument in enumerate(command_line[:-1]):
        if isinstance(argument,str) and argument.lower() == "-grpcsrv":
            value = command_line[index+1]
            if isinstance(value,str) and value.isascii() and value.isdecimal() and 0 < int(value) <= 65535:
                return int(value), non_graphical
    return None, non_graphical


@contextmanager
def _attach_only_desktop(module: Any, kwargs: Mapping[str, Any], *,
                         verified_owner: tuple[int, float] | None = None,
                         grpc_port_hint: int | None = None):
    """Block the audited vendor fallback paths for this one Desktop constructor.

    PyAEDT 1.10 can set new_desktop=True when its own session discovery misses a
    port. Do not let a health check spawn AEDT. Restore methods on every exit;
    the broker serializes calls, and this process-local lock covers this patch.
    Arbitrary run_python_code calls are outside this narrow protection.
    """
    cls = module.Desktop
    names = ("_validate_port", "_Desktop__initialize", "_Desktop__dispatch_win32", "_Desktop__run_student", "check_starting_mode")
    if not callable(getattr(module, "launch_aedt", None)) or any(not callable(getattr(cls, name, None)) for name in names):
        raise BackendCommandError("PyAEDT Desktop layout differs from the audited attach-only implementation")
    old = {name: getattr(cls, name) for name in names}
    old_launch = module.launch_aedt

    def refuse_launch(*args: Any, **options: Any):
        raise BackendCommandError("PyAEDT attempted a fallback launch; attach-only protection refused it")

    def check_starting_mode(desktop: Any):
        if grpc_port_hint is not None:
            if desktop.new_desktop:
                refuse_launch()
            # Avoid a second vendor process/socket discovery. Its false
            # negatives can incorrectly select COM for an existing -ng server.
            desktop.port = grpc_port_hint
            desktop.is_grpc_api = True
            desktop._Desktop__starting_mode = "grpc"
            return
        return old["check_starting_mode"](desktop)

    def validate_port(desktop: Any):
        if desktop.new_desktop or not desktop.port:
            raise BackendCommandError("PyAEDT attempted to allocate a desktop or change the explicit attach target")
        if kwargs.get("port") and desktop.port != kwargs["port"]:
            raise BackendCommandError("PyAEDT changed the requested gRPC port")
        if grpc_port_hint is not None and desktop.port != grpc_port_hint:
            raise BackendCommandError("PyAEDT changed the verified PID's gRPC port hint")
        # Bypass vendor all_active_sessions(), whose false negatives otherwise
        # switch the constructor into launch mode. Socket ownership was proved.
        if verified_owner is not None:
            actual_owner = _existing_target_owner({"new_desktop": False, "aedt_process_id": verified_owner[0]})
            if actual_owner != verified_owner:
                raise BackendCommandError("AEDT process identity changed before the attach handshake")
        else:
            _existing_target_owner({"new_desktop": False, "port": desktop.port})
        return desktop.port

    def initialize(desktop: Any, new_desktop_required: bool | None = None):
        if desktop.new_desktop or new_desktop_required:
            refuse_launch()
        if not desktop.is_grpc_api:
            # __init_dotnet subsequently binds the ROT moniker for the exact PID.
            # Load COM utilities but skip CreateObject(version), which can create
            # an application if no registered active object remains.
            if not kwargs.get("aedt_process_id"):
                raise BackendCommandError("COM attach requires an explicit verified PID")
            from ansys.aedt.core.internal.clr_module import _clr
            if _clr is None:
                raise BackendCommandError("COM attach requires ansys-pythonnet; install it or use a verified gRPC port")
            _clr.AddReference("Ansys.Ansoft.CoreCOMScripting")
            com = __import__("Ansys.Ansoft.CoreCOMScripting")
            desktop.COMUtil = com.Ansoft.CoreCOMScripting.Util.COMUtil
            return None
        return old["_Desktop__initialize"](desktop, new_desktop_required=False)

    try:
        module.launch_aedt = refuse_launch
        cls._validate_port = validate_port
        cls.check_starting_mode = check_starting_mode
        cls._Desktop__initialize = initialize
        cls._Desktop__dispatch_win32 = refuse_launch
        cls._Desktop__run_student = refuse_launch
        yield
    finally:
        module.launch_aedt = old_launch
        for name, method in old.items():
            setattr(cls, name, method)


def _default_desktop_factory(**kwargs: Any) -> Any:
    # Environment variables silently override explicit targets in PyAEDT.
    for name in ("PYAEDT_PROCESS_ID", "PYAEDT_DESKTOP_PORT"):
        if os.environ.get(name) not in (None, "", "0"):
            raise BackendCommandError(f"unset {name}; this broker requires its explicit PID/port to remain authoritative")
    for name in ("PYAEDT_DOC_GENERATION", "PYAEDT_NON_GRAPHICAL"):
        if os.environ.get(name, "").lower() in ("true", "1", "t"):
            raise BackendCommandError(f"unset {name} before using the attach-only broker")
    configured_version = os.environ.get("PYAEDT_DESKTOP_VERSION")
    if configured_version and configured_version != kwargs.get("version"):
        raise BackendCommandError("PYAEDT_DESKTOP_VERSION conflicts with the explicit AEDT version")
    with _ATTACH_LOCK:
        expected_pid, expected_started = _existing_target_owner(kwargs)
        configured_port, actual_non_graphical = _pid_launch_hints(expected_pid)
        expected_port = kwargs.get("port") or configured_port
        if kwargs.get("port") and configured_port is not None and kwargs["port"] != configured_port:
            raise BackendCommandError("requested gRPC port differs from the verified process's launch argument")
        vendor_kwargs = dict(kwargs)
        if configured_port is not None:
            vendor_kwargs["non_graphical"] = actual_non_graphical
        module = _audited_desktop_module()
        settings = module.settings
        if settings.remote_rpc_session or settings.use_lsf_scheduler:
            raise BackendCommandError("remote/LSF settings are incompatible with this local attach-only broker")
        with _attach_only_desktop(module, kwargs, verified_owner=(expected_pid, expected_started), grpc_port_hint=expected_port):
            desktop = module.Desktop(**vendor_kwargs)
        if getattr(desktop, "aedt_process_id", None) != expected_pid:
            raise BackendCommandError("PyAEDT returned a different AEDT PID; no project operation was performed")
        if expected_port is not None and getattr(desktop, "port", None) != expected_port:
            raise BackendCommandError("PyAEDT returned a different AEDT port; no project operation was performed")
        if kwargs.get("version") and str(getattr(desktop, "aedt_version_id", "")) != kwargs["version"]:
            raise BackendCommandError("PyAEDT returned a different AEDT version; no project operation was performed")
        actual_pid, actual_started = _existing_target_owner({"new_desktop": False, "aedt_process_id": expected_pid})
        if (actual_pid, actual_started) != (expected_pid, expected_started):
            raise BackendCommandError("AEDT process identity changed during attach; no project operation was performed")
        return desktop


def _default_hfss_factory(**kwargs: Any) -> Any:
    from ansys.aedt.core import Hfss

    return Hfss(**kwargs)


def _default_wr90_builder(app: Any, output_dir: str, solve: bool) -> dict[str, Any]:
    from wr90_case import build_wr90_case

    return build_wr90_case(app, output_dir, solve)


def _target_dict(target: AedtTarget) -> dict[str, Any]:
    return {"kind": target.kind, "value": target.value}


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BackendCommandError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(arguments: Mapping[str, Any], name: str, default: str = "") -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise BackendCommandError(f"{name} must be a string")
    return value.strip()


def _read_value(obj: Any, name: str, *args: Any) -> Any:
    value = getattr(obj, name)
    return value(*args) if callable(value) else value


def _object_name(obj: Any) -> str | None:
    if obj is None:
        return None
    get_name = getattr(obj, "GetName", None)
    if callable(get_name):
        return str(get_name())
    name = getattr(obj, "name", None)
    return str(name) if name is not None else None


def _release(handle: Any) -> bool:
    desktop = getattr(handle, "desktop_class", handle)
    return bool(desktop.release_desktop(close_projects=False, close_on_exit=False))


def _active_design(desktop: Any, project: Any) -> Any:
    if project is None:
        return None
    project_name = _object_name(project)
    try:
        designs = list(_read_value(desktop, "design_list", project_name))
    except AttributeError:
        designs = None
    if designs == []:
        return None
    return desktop.active_design(project)


class PyAedtBackend:
    def __init__(
        self,
        *,
        desktop_factory: Callable[..., Any] | None = None,
        hfss_factory: Callable[..., Any] | None = None,
        wr90_builder: Callable[[Any, str, bool], dict[str, Any]] | None = None,
        official_capabilities: OfficialCapabilities | None = None,
        version: str | None = None,
    ) -> None:
        # Enforcement only applies to the real PyAEDT client; tests inject a fake factory.
        self._real_aedt = desktop_factory is None
        self._desktop_factory = desktop_factory or _default_desktop_factory
        self._hfss_factory = hfss_factory or _default_hfss_factory
        self._wr90_builder = wr90_builder or _default_wr90_builder
        self._official = official_capabilities or OfficialCapabilities()
        self._maxwell = MaxwellCapabilities(app_resolver=self._official._resolve_app)
        self._version = version or os.environ.get("AEDT_VERSION", "2026.1")
        self._desktop: Any = None
        self._bound_target: AedtTarget | None = None
        self._apps: dict[tuple[str, str, str | None], Any] = {}
        self._lock = threading.RLock()

    def execute(
        self,
        target: AedtTarget,
        command: str,
        arguments: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._execute(target, command, arguments)

    def _execute(
        self,
        target: AedtTarget,
        command: str,
        arguments: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if command not in _DESKTOP_COMMANDS | _HFSS_COMMANDS:
            raise BackendCommandError(f"unsupported command: {command}")
        if command in {"run_python_code", "run_python_script"} and self._real_aedt:
            allowed = os.environ.get("AEDT_MCP_ALLOW_ARBITRARY_CODE", "").strip().lower() in {
                "1", "true", "yes", "on"
            }
            if not allowed:
                raise BackendCommandError(
                    "E_ARBITRARY_CODE_DISABLED: arbitrary broker code is disabled by default; "
                    "use dedicated tools or set AEDT_MCP_ALLOW_ARBITRARY_CODE=1 for a supervised session"
                )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise BackendCommandError("arguments must be an object")

        if command in MAXWELL_COMMANDS:
            desktop = self._desktop_for(target)
            try:
                return self._maxwell.execute(
                    command, desktop=desktop, target=target, arguments=arguments,
                )
            except CapabilityError as exc:
                raise BackendCommandError(str(exc)) from exc
        if command in OFFICIAL_BACKEND_COMMANDS:
            desktop = self._desktop_for(target)
            try:
                result = self._official.execute(
                    command,
                    desktop=desktop,
                    target=target,
                    arguments=arguments,
                    connection_kwargs=self._connection_kwargs(
                        self._bound_target or target
                    ),
                )
            except CapabilityError as exc:
                raise BackendCommandError(str(exc)) from exc
            if command == "disconnect_from_aedt":
                self._apps.clear()
                self._desktop = None
                self._bound_target = None
            return result
        if command in _DESKTOP_COMMANDS:
            result = self._execute_desktop(target, command, arguments)
            if command == "close_projects":
                self._official.clear_local_state()
            return result
        return self._execute_hfss(target, command, arguments)

    def release(self) -> bool:
        with self._lock:
            if self._desktop is None:
                return False
            desktop = self._desktop
            self._apps.clear()
            self._official.clear_local_state()
            self._desktop = None
            self._bound_target = None
            return _release(desktop)

    @property
    def session_pid(self) -> int | None:
        with self._lock:
            if self._desktop is None:
                return None
            pid = getattr(self._desktop, "aedt_process_id", None)
            return pid if type(pid) is int and pid > 0 else None

    def close_for_user_request(self) -> bool:
        with self._lock:
            if self._desktop is None:
                return False
            desktop = self._desktop
            self._apps.clear()
            self._official.clear_local_state()
            self._desktop = None
            self._bound_target = None
            desktop.odesktop.QuitApplication()
            return True

    def _desktop_for(self, target: AedtTarget) -> Any:
        if self._desktop is None:
            if self._real_aedt:
                from aedt_guard import managed

                managed.enforce_target(target)
            self._desktop = self._desktop_factory(**self._connection_kwargs(target))
            self._bound_target = target
            return self._desktop

        if not self._target_matches(target):
            raise BackendCommandError(
                f"broker is bound to {self._bound_target.key}; requested {target.key}"
            )
        return self._desktop

    def _target_matches(self, target: AedtTarget) -> bool:
        if target == self._bound_target:
            return True
        if target.kind == "pid":
            return getattr(self._desktop, "aedt_process_id", None) == target.value
        return getattr(self._desktop, "port", None) == target.value

    def _connection_kwargs(self, target: AedtTarget) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "version": self._version,
            "non_graphical": False,
            "new_desktop": False,
            "close_on_exit": False,
        }
        if target.kind == "pid":
            kwargs["aedt_process_id"] = target.value
        else:
            kwargs.update({"machine": "localhost", "port": target.value})
        return kwargs

    def _execute_desktop(
        self,
        target: AedtTarget,
        command: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        desktop = self._desktop_for(target)
        if command == "ping":
            return self._ping(desktop, target)
        if command == "project_info":
            return self._project_info(desktop, target)
        if command == "close_projects":
            return self._close_projects(desktop, target, arguments)
        return self._save_project(desktop, target, arguments)

    def _execute_hfss(
        self,
        target: AedtTarget,
        command: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if command == "build_wr90_waveguide":
            project_name = _optional_text(
                arguments, "project_name", "Classic_WR90_Waveguide"
            )
            design_name = _optional_text(arguments, "design_name", "WR90_TE10")
            output_dir = _required_text(arguments, "output_dir")
            solve = arguments.get("solve", True)
            if not isinstance(solve, bool):
                raise BackendCommandError("solve must be a boolean")
            if solve:
                from aedt_guard.audit import enforce_analyze as _enforce_analyze

                _verdict = _enforce_analyze(
                    "build_wr90_waveguide", arguments,
                    project=project_name, design=design_name, setup=None,
                )
                if not _verdict["allowed"]:
                    raise BackendCommandError("E_UNGUARDED_ANALYZE: " + _verdict["reason"])
        else:
            project_name = _required_text(arguments, "project_name")
            design_name = _required_text(arguments, "design_name")
        self._desktop_for(target)
        kwargs = self._connection_kwargs(self._bound_target or target)
        kwargs.update({"project": project_name, "design": design_name})

        if command in {"create_hfss_design", "build_wr90_waveguide"}:
            requested = _optional_text(arguments, "solution_type", "DrivenModal")
            try:
                kwargs["solution_type"] = _SOLUTION_TYPES[requested]
            except KeyError as exc:
                raise BackendCommandError(
                    f"unsupported HFSS solution_type: {requested}"
                ) from exc
        elif command == "start_analysis":
            kwargs["setup"] = _required_text(arguments, "setup_name")
            from aedt_guard.audit import enforce_analyze as _enforce_analyze

            _verdict = _enforce_analyze(
                "start_analysis",
                arguments,
                project=project_name,
                design=design_name,
                setup=kwargs["setup"],
            )
            if not _verdict["allowed"]:
                raise BackendCommandError("E_UNGUARDED_ANALYZE: " + _verdict["reason"])

        app_key = (project_name, design_name, kwargs.get("solution_type"))
        app = self._apps.get(app_key)
        if app is None:
            app = self._hfss_factory(**kwargs)
            self._apps[app_key] = app
        if command == "build_wr90_waveguide":
            result = self._wr90_builder(app, output_dir, solve)
            return {"target": _target_dict(target), **result}
        if command == "create_hfss_design":
            return {
                "target": _target_dict(target),
                "project_name": str(getattr(app, "project_name", project_name)),
                "design_name": str(getattr(app, "design_name", design_name)),
                "solution_type": str(
                    getattr(app, "solution_type", kwargs["solution_type"])
                ),
            }
        if command == "start_analysis":
            blocking = arguments.get("blocking", False)
            if not isinstance(blocking, bool):
                raise BackendCommandError("blocking must be a boolean")
            setup_name = kwargs["setup"]
            started = bool(app.analyze_setup(name=setup_name, blocking=blocking))
            return {
                "target": _target_dict(target),
                "project_name": project_name,
                "design_name": design_name,
                "setup_name": setup_name,
                "blocking": blocking,
                "started": started,
                "solver_completion_confirmed": False,
                "convergence_confirmed": False,
                "completion_evidence_required": "Inspect the native profile and convergence for this exact submission; API success is not completion.",
            }

        # --- v1.5 guarded-workflow extensions (additive; no change to old commands) ---
        if command == "abort_analysis":
            stop = getattr(app, "stop_simulations", None)
            if not callable(stop):
                raise BackendCommandError(
                    "abort_analysis is unsupported by this PyAEDT build (no stop_simulations)"
                )
            detail = stop(clean_stop=True)
            return {
                "target": _target_dict(target),
                "project_name": project_name,
                "design_name": design_name,
                "stopped": True,
                "detail": str(detail)[:200],
            }
        if command == "preflight_design":
            return self._preflight_design(app, target, arguments)
        if command == "finalize_solve":
            return self._finalize_solve(app, target, arguments)
        if command == "export_farfield_report":
            return self._export_farfield_report(app, target, arguments)

        running = bool(_read_value(app, "are_there_simulations_running"))
        setups = list(app.get_setups())
        return {
            "target": _target_dict(target),
            "project_name": project_name,
            "design_name": design_name,
            "running": running,
            "setups": [str(item) for item in setups],
            "solver_completion_confirmed": False,
            "convergence_confirmed": False,
            "activity_scope": "desktop",
            "completion_evidence_required": "Idle state and existing results do not establish completion of the latest solve.",
        }

    # ------------------------------------------------------------------
    # v1.5 guarded-workflow helpers (new commands only; additive)
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_solution_data(app: Any, setup_name: str) -> bool | None:
        """True/False/None: does the named setup have solution data?

        None means the probe itself was inconclusive (attribute/API missing or
        an unexpected vendor error such as KeyError('HFSS Terminal Network')).
        """
        try:
            known: list[bool] = []
            for setup in getattr(app, "setups", None) or []:
                if str(getattr(setup, "name", "")) != str(setup_name):
                    continue
                solved = getattr(setup, "is_solved", None)
                if solved is not None:
                    known.append(bool(solved))
            if known:
                return any(known)
        except Exception:
            # vendor internals can raise for Terminal/mixed solution types
            return None
        # Do NOT fall back to a report/solution-data query here: on designs whose sweeps the app
        # created, PyAEDT's report plumbing prints repeated AEDT script-macro errors ("Solution is
        # not a property name") into the user's message window, once per job, before the solve, and
        # the answer only restates what the setup already exposes. "Could not determine" is honest.
        return None

    def _preflight_design(
        self,
        app: Any,
        target: AedtTarget,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        reports_cleared = _clear_design_reports(app)
        attempts = arguments.get("validate_attempts", 3)
        try:
            attempts = max(1, int(attempts))
        except (TypeError, ValueError):
            raise BackendCommandError("validate_attempts must be a positive integer")
        setup_name = _optional_text(arguments, "setup_name") or None
        require_ff_sphere = bool(arguments.get("require_ff_sphere", True))

        validation_passed = False
        last_error: str | None = None
        validate = getattr(app, "validate_simple", None)
        for _ in range(attempts):
            try:
                if callable(validate) and bool(validate()):
                    validation_passed = True
                    break
            except Exception as exc:  # noqa: BLE001 - report, then retry
                last_error = repr(exc)[:200]
            time.sleep(2.0)

        ff_sphere_present: bool | None = None
        try:
            field_setups = getattr(app, "field_setup_names", None)
            if isinstance(field_setups, (list, tuple)):
                ff_sphere_present = bool(len(field_setups))
        except Exception:
            ff_sphere_present = None

        solution_data_present: bool | None = None
        if setup_name:
            solution_data_present = self._probe_solution_data(app, setup_name)

        if require_ff_sphere and ff_sphere_present is False:
            raise BackendCommandError(
                "preflight_design refused submission: no infinite-sphere/radiation "
                "setup found (E_FF_SPHERE_MISSING). Insert an infinite sphere before "
                "solving if far fields are required, or set require_ff_sphere=false. "
                "Note: an infinite sphere also requires an existing radiation "
                "boundary (open region); AEDT refuses the sphere insertion without one."
            )
        if not validation_passed:
            raise BackendCommandError(
                "preflight_design refused submission: geometry validation failed "
                "after %d attempt(s) (E_VALIDATE_FALSE): %s"
                % (attempts, last_error or "unknown reason")
            )
        return {
            "target": _target_dict(target),
            "validation_passed": validation_passed,
            "validation_attempts": attempts,
            "ff_sphere_present": ff_sphere_present,
            "ff_sphere_required": require_ff_sphere,
            "solution_data_present_before_submit": solution_data_present,
            "reports_cleared": reports_cleared,
            "last_error": last_error,
            "note": "preflight passed; evidence fields None mean 'could not determine'.",
        }

    def _finalize_solve(
        self,
        app: Any,
        target: AedtTarget,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        setups_arg = arguments.get("setups")
        if setups_arg is None:
            try:
                setups = [str(getattr(s, "name", "")) for s in (getattr(app, "setups", None) or [])]
            except Exception as exc:  # vendor internals can raise for mixed solution types
                raise BackendCommandError(
                    "finalize_solve could not enumerate setups (%s); pass setups=[...] explicitly"
                    % repr(exc)[:120]
                ) from exc
        elif isinstance(setups_arg, str):
            setups = [setups_arg]
        elif isinstance(setups_arg, (list, tuple)):
            setups = [str(item) for item in setups_arg]
        else:
            raise BackendCommandError("setups must be a name, a list of names, or omitted")
        if not setups:
            raise BackendCommandError("finalize_solve requires at least one setup")

        try:
            saved = bool(app.save_project())
        except Exception as exc:  # noqa: BLE001
            raise BackendCommandError("finalize failed: save_project raised (E_SAVE_MISSING): %s" % exc) from exc
        if not saved:
            raise BackendCommandError("finalize failed: save_project returned False (E_SAVE_MISSING)")

        probe = {name: self._probe_solution_data(app, name) for name in setups}
        missing = [name for name, ok in probe.items() if ok is False]
        return {
            "target": _target_dict(target),
            "saved": True,
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "setup_solution_data": probe,
            "incomplete_setups": missing,
            "solution_data_complete": not missing and all(v is not False for v in probe.values()),
            "note": "True = solution data present for that setup; None = could not determine. "
                    "Incomplete setups mean the run must not be reported as COMPLETED.",
        }

    def _export_farfield_report(
        self,
        app: Any,
        target: AedtTarget,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        setup_name = _required_text(arguments, "setup_name")
        out_dir = _required_text(arguments, "output_dir")
        post = getattr(app, "post", None)
        if post is None:
            raise BackendCommandError("far-field export unavailable for this design (E_API_MISUSE)")
        presets = {
            "theta_phi0": ({"Phi": ["0deg"]}, "Theta"),
            "theta_phi90": ({"Phi": ["90deg"]}, "Theta"),
            "phi_theta90": ({"Phi": ["All"], "Theta": ["90deg"]}, "Phi"),
            # a full theta/phi grid: every phi is a family member, so the export holds one
            # column per phi and the directivity integral has the whole sphere to work with
            "sphere_grid": ({"Phi": ["All"]}, "Theta"),
        }
        cuts = arguments.get("cuts") or ["theta_phi0", "phi_theta90"]
        if isinstance(cuts, str):
            cuts = [cuts]
        unknown = [str(c) for c in cuts if str(c) not in presets]
        if unknown:
            raise BackendCommandError("unknown cuts %s; choose from %s" % (unknown, sorted(presets)))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        sweep_name = _optional_text(arguments, "sweep_name") or None
        raw_frequencies = arguments.get("frequencies_ghz")
        if raw_frequencies is None:
            frequencies: list[float | None] = [None]
        elif isinstance(raw_frequencies, (int, float)):
            frequencies = [float(raw_frequencies)]
        else:
            frequencies = [float(f) for f in raw_frequencies]
        results: dict[str, Any] = {}
        unavailable: list[float] = []
        for frequency in frequencies:
            at_frequency: list[bool] = []
            for cut in cuts:
                variations, sweep = presets[str(cut)]
                if frequency is None:
                    setup_sweep = "%s : LastAdaptive" % setup_name
                    variant = variations
                    suffix = ""
                else:
                    setup_sweep = (
                        "%s : %s" % (setup_name, sweep_name)
                        if sweep_name
                        else "%s : LastAdaptive" % setup_name
                    )
                    variant = {**variations, "Freq": ["%gGHz" % frequency]}
                    suffix = "@%gGHz" % frequency
                rep = False
                for _attempt in range(3):
                    # gain only by default: the directivity expression made real solves stall
                    # in the sweep phase (eight idle hf3d workers for thirty minutes), so it is
                    # opt-in per task rather than always on
                    expressions = ["dB(GainTotal)"]
                    if arguments.get("include_directivity") is True:
                        expressions.append("dB(DirectivityTotal)")
                    rep = post.create_report(
                        expressions=expressions,
                        setup_sweep_name=setup_sweep,
                        primary_sweep_variable=sweep,
                        report_category="Far Fields",
                        variations=variant,
                        plot_type="Rectangular Plot",
                    )
                    if rep:
                        break
                    # create_report returns False while the design's solution
                    # context is still settling right after a solve
                    time.sleep(2.0)
                if not rep:
                    raise BackendCommandError(
                        "create_report returned False for setup=%r sweep=%r freq=%r "
                        "after 3 attempts (far-field data may be unavailable for that "
                        "solution)" % (setup_name, setup_sweep, frequency)
                    )
                report_name = _resolve_report_name(rep)
                exported = Path(post.export_report_to_csv(out_dir, report_name))
                _drop_report(post, report_name)
                rows = 0
                if exported.is_file():
                    try:
                        with exported.open(encoding="utf-8") as handle:
                            rows = sum(1 for _ in handle)
                    except Exception:
                        rows = 0
                entry = {
                    "report_name": report_name,
                    "path": str(exported),
                    "exists": exported.is_file(),
                    "rows": rows,
                    "non_empty": rows > 1,
                    "frequency_ghz": frequency,
                }
                if "dB(DirectivityTotal)" in expressions:
                    # export_report_to_csv writes only the primary trace, so the directivity
                    # needs its own report and its own file next to the gain one
                    direct = False
                    direct_error = None
                    try:
                        for _attempt in range(3):
                            direct = post.create_report(
                                expressions=["dB(DirectivityTotal)"],
                                setup_sweep_name=setup_sweep,
                                primary_sweep_variable=sweep,
                                report_category="Far Fields",
                                variations=variant,
                                plot_type="Rectangular Plot",
                            )
                            if direct:
                                break
                            time.sleep(2.0)
                    except Exception as exc:  # noqa: BLE001 - the metric is optional
                        # AEDT rejects dB(DirectivityTotal) for some designs, and the failed
                        # command poisons the worker session, so record it and move on instead
                        # of failing the whole run for an optional column
                        direct = False
                        direct_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
                    if direct_error:
                        entry["directivity_error"] = direct_error
                    if direct:
                        direct_name = _resolve_report_name(direct)
                        direct_path = Path(post.export_report_to_csv(out_dir, direct_name))
                        _drop_report(post, direct_name)
                        entry["directivity_path"] = str(direct_path)
                        entry["directivity_rows"] = sum(
                            1 for _ in direct_path.open(encoding="utf-8")) \
                            if direct_path.is_file() else 0
                results["%s%s" % (cut, suffix)] = entry
                at_frequency.append(rows > 1)
            if frequency is not None and not any(at_frequency):
                unavailable.append(float(frequency))
        empty = [k for k, v in results.items() if not v["non_empty"]]
        if unavailable:
            raise BackendCommandError(
                "E_FF_FREQ_UNAVAILABLE: no radiated fields exported at %s GHz. "
                "An HFSS sweep only stores far fields at frequencies where fields "
                "were saved (enable Save Fields on the sweep) or at the adaptive "
                "solution frequency; otherwise limit frequencies_ghz to the "
                "adaptive frequency." % ", ".join("%g" % f for f in unavailable)
            )
        if empty:
            raise BackendCommandError(
                "far-field export produced empty report(s) %s (E_REPORT_EMPTY); "
                "setup %s may lack radiated-field data (infinite sphere must exist at solve time)"
                % (empty, setup_name)
            )
        return {
            "target": _target_dict(target),
            "setup_name": setup_name,
            "output_dir": out_dir,
            "cuts": results,
            "frequencies_ghz": [f for f in frequencies if f is not None] or None,
            "non_empty": True,
            "note": "Non-empty CSV proves the report has rows, not that the far field "
                    "belongs to the latest solve; pair with finalize_solve evidence.",
        }


    def _ping(self, desktop: Any, target: AedtTarget) -> dict[str, Any]:
        project = desktop.active_project()
        design = _active_design(desktop, project)
        return {
            "connected": True,
            "target": _target_dict(target),
            "aedt_version": str(getattr(desktop, "aedt_version_id", self._version)),
            "pid": getattr(desktop, "aedt_process_id", None),
            "port": getattr(desktop, "port", None),
            "active_project": _object_name(project),
            "active_design": _object_name(design),
        }

    def _project_info(self, desktop: Any, target: AedtTarget) -> dict[str, Any]:
        projects = list(_read_value(desktop, "project_list"))
        project = desktop.active_project()
        design = _active_design(desktop, project)
        project_name = _object_name(project)
        design_name = _object_name(design)
        design_type = None
        if project_name and design_name:
            design_type = desktop.design_type(project_name, design_name)
        return {
            "target": _target_dict(target),
            "projects": [str(item) for item in projects],
            "active_project": project_name,
            "active_design": design_name,
            "design_type": str(design_type) if design_type is not None else None,
        }

    def _save_project(
        self,
        desktop: Any,
        target: AedtTarget,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        project = desktop.active_project()
        project_name = _object_name(project)
        if not project_name:
            raise BackendCommandError("AEDT has no active project to save")
        path = _optional_text(arguments, "path")
        saved = bool(desktop.save_project(project_name, path or None))
        return {
            "target": _target_dict(target),
            "project_name": project_name,
            "path": path or None,
            "saved": saved,
        }

    def _close_projects(
        self,
        desktop: Any,
        target: AedtTarget,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        project_names = arguments.get("project_names")
        if not isinstance(project_names, list) or not project_names:
            raise BackendCommandError("project_names must be a non-empty list")
        if any(not isinstance(name, str) or not name.strip() for name in project_names):
            raise BackendCommandError(
                "project_names must contain only non-empty strings"
            )
        save = arguments.get("save", False)
        if not isinstance(save, bool):
            raise BackendCommandError("save must be a boolean")

        available = [str(item) for item in _read_value(desktop, "project_list")]
        closed = []
        not_found = []
        for raw_name in project_names:
            name = raw_name.strip()
            if name not in available:
                not_found.append(name)
                continue
            if save:
                desktop.save_project(name)
            desktop.odesktop.CloseProject(name)
            available.remove(name)
            closed.append(name)
        self._apps = {
            key: app for key, app in self._apps.items() if key[0] not in closed
        }
        return {
            "target": _target_dict(target),
            "closed": closed,
            "not_found": not_found,
            "remaining": [str(item) for item in _read_value(desktop, "project_list")],
            "saved_before_close": save,
        }


