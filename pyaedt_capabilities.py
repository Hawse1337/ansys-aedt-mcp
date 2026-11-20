from __future__ import annotations

import base64
import contextlib
import csv
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from aedt_target import AedtTarget
from solve_evidence import summarize_hfss_convergence, summarize_hfss_profile



def _count_touchstone_rows(path: Path) -> int:
    """Number of data rows in a touchstone file (header/comment lines skipped)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    rows = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("!") or stripped.startswith("#"):
            continue
        if len(stripped.split()) >= 3:
            rows += 1
    return rows


class CapabilityError(ValueError):
    pass


APPLICATION_TYPES = (
    "Hfss",
    "Maxwell2d",
    "Maxwell3d",
    "Q3d",
    "Q2d",
    "Icepak",
    "Circuit",
    "TwinBuilder",
    "Mechanical",
    "Emit",
    "RMXprt",
    "Hfss3dLayout",
)

OFFICIAL_BACKEND_COMMANDS = {
    "connect_to_aedt",
    "disconnect_from_aedt",
    "get_pyaedt_logs",
    "run_python_script",
    "run_python_code",
    "run_approved_builder",
    "list_designs",
    "list_projects",
    "open_project",
    "save_project",
    "create_design",
    "validate_design",
    "analyze_design",
    "export_results",
    "screenshot",
    "export_config",
    "clear_aedt",
    "get_model_info",
}


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(arguments: Mapping[str, Any], name: str, default: str = "") -> str:
    value = arguments.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise CapabilityError(f"{name} must be a string or null")
    return value.strip()


def _boolean(arguments: Mapping[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if type(value) is not bool:
        raise CapabilityError(f"{name} must be a boolean")
    return value


def _positive_int(arguments: Mapping[str, Any], name: str) -> int | None:
    value = arguments.get(name)
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise CapabilityError(f"{name} must be a positive integer or null")
    return value


def _name(value: Any) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "GetName", None)
    if callable(getter):
        return str(getter())
    raw_name = getattr(value, "name", None)
    return str(raw_name) if raw_name is not None else None


def json_safe(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def _is_icepak(app: Any) -> bool:
    return str(getattr(app, "design_type", "")).strip().lower() == "icepak"


def _hfss_setup_readback(app: Any, setup_name: str | None) -> dict[str, Any]:
    """Read native HFSS setup properties; never substitute the PyAEDT cache.

    Unsupported solution types, versions, or native interfaces are explicitly
    unconfirmed.  In particular a snapshot cannot prove the eventual settings
    used by batch analyze(), which can save internally after this readback.
    """
    applicable = str(getattr(app, "design_type", "")).strip().casefold() == "hfss"
    result: dict[str, Any] = {
        "applicable": applicable,
        "source": "native_HfssTab_GetPropertyValue" if applicable else None,
        "readback_confirmed": False,
        "setups": {},
        "errors": [],
    }
    if not applicable:
        return result
    try:
        names = [setup_name] if setup_name else list(app.get_setups())
        getter = app.odesign.GetPropertyValue
        for name in names:
            values = {}
            for key, prop in (("solver_type", "Solver Type"), ("solution_frequency", "Solution Freq")):
                try:
                    value = getter("HfssTab", f"AnalysisSetup:{name}", prop)
                    values[key] = str(value).strip() if value is not None and type(value) is not bool else None
                    if not values[key]:
                        result["errors"].append(f"{name}/{prop}: native readback unavailable")
                except Exception as exc:
                    values[key] = None
                    result["errors"].append(f"{name}/{prop}: {type(exc).__name__}: {exc}")
            result["setups"][str(name)] = values
        result["readback_confirmed"] = bool(names) and not result["errors"]
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def _check_saved_hfss_setup(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Refuse submission when an explicit save changes confirmed native settings."""
    for name, previous in before["setups"].items():
        current = after["setups"].get(name, {})
        for prop, value in previous.items():
            if value is not None and current.get(prop) != value:
                raise CapabilityError(
                    f"Project was saved, but native HFSS {name}/{prop} changed "
                    f"from {value!r} to {current.get(prop)!r}. No solve was submitted. "
                    "Inspect the saved setup and explicitly restore the intended settings before retrying."
                )


def _icepak_result_directories(app: Any) -> list[Path]:
    raw_results = getattr(app, "results_directory", None)
    if not raw_results:
        return []
    root = Path(str(raw_results)).expanduser()
    design_name = str(getattr(app, "design_name", "")).strip()
    if design_name:
        design_results = root / f"{design_name}.results"
        if design_results.is_dir():
            return [design_results]
    return [root] if root.is_dir() else []


def _parse_icepak_residual_history(path: Path) -> list[dict[str, float | int]]:
    history: list[dict[str, float | int]] = []
    residual_markers = {
        "Continuity",
        "XVelocity",
        "YVelocity",
        "ZVelocity",
        "Energy",
    }
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(rf"^\s*({number})\s+(.*)$", line)
        if not match:
            continue
        values = {
            name.strip().replace(" ", ""): float(value)
            for name, value in re.findall(rf"([A-Za-z][A-Za-z0-9 _-]*)\(({number})\)", match.group(2))
        }
        if not residual_markers.intersection(values):
            continue
        iteration_value = float(match.group(1))
        iteration: float | int = (
            int(iteration_value) if iteration_value.is_integer() else iteration_value
        )
        history.append({"Iteration": iteration, **values})
    return history


def _export_icepak_convergence(app: Any, output: Path) -> dict[str, Any]:
    candidates: list[tuple[int, float, Path, list[dict[str, float | int]]]] = []
    for root in _icepak_result_directories(app):
        for source in root.rglob("*.sd"):
            if not source.is_file():
                continue
            history = _parse_icepak_residual_history(source)
            if history:
                candidates.append((len(history), source.stat().st_mtime, source, history))
    if not candidates:
        raise CapabilityError(
            "Icepak residual monitor history was not found in the solved results directory"
        )
    _, _, source, history = max(candidates, key=lambda item: (item[0], item[1]))
    preferred = [
        "Continuity",
        "XVelocity",
        "YVelocity",
        "ZVelocity",
        "Energy",
    ]
    available = {key for row in history for key in row if key != "Iteration"}
    columns = [name for name in preferred if name in available]
    columns.extend(sorted(available - set(columns)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["Iteration", *columns, "SourceFile"],
        )
        writer.writeheader()
        for row in history:
            writer.writerow({**row, "SourceFile": str(source)})
    return {
        "path": str(output),
        "format": "csv",
        "source": "icepak_monitor_history",
        "source_file": str(source),
        "row_count": len(history),
        "columns": ["Iteration", *columns],
    }


def _profile_mesh_value(content: str, label: str) -> int | None:
    escaped_label = re.escape(label)
    for prefix in ("Total ", ""):
        patterns = (
            rf"\\?'(?:{prefix}){escaped_label}\\?',\s*(\d+)",
            rf"\b(?:{prefix}){escaped_label}\s*:\s*(\d+)",
        )
        for pattern in patterns:
            values = re.findall(pattern, content, flags=re.IGNORECASE)
            if values:
                return int(values[-1])
    return None


def _export_icepak_mesh_stats(app: Any, setup_name: str, output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    profile_path = output.with_name(f"{output.stem}_source_profile.prof")
    profile_result = app.export_profile(
        setup=setup_name,
        output_file=str(profile_path),
    )
    if isinstance(profile_result, (str, Path)) and Path(profile_result).is_file():
        profile_path = Path(profile_result)
    if not profile_path.is_file():
        raise CapabilityError("Icepak profile export did not create a readable file")
    content = profile_path.read_text(encoding="utf-8", errors="replace")
    statistics = {
        label.lower(): _profile_mesh_value(content, label)
        for label in ("Nodes", "Faces", "Cells")
    }
    if any(value is None for value in statistics.values()):
        raise CapabilityError(
            "Icepak profile did not contain total Nodes, Faces, and Cells"
        )
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["Setup", "Nodes", "Faces", "Cells", "NormalCompletion", "SourceProfile"]
        )
        writer.writerow(
            [
                setup_name,
                statistics["nodes"],
                statistics["faces"],
                statistics["cells"],
                "Normal Completion" in content,
                str(profile_path),
            ]
        )
    return {
        "path": str(output),
        "format": "csv",
        "source": "icepak_solution_profile",
        "source_file": str(profile_path),
        "statistics": statistics,
        "normal_completion": "Normal Completion" in content,
    }


def _default_app_resolver(
    *, desktop: Any, project_name: str | None, design_name: str | None
) -> Any:
    from ansys.aedt.core import get_pyaedt_app

    return get_pyaedt_app(
        project_name=project_name,
        design_name=design_name,
        desktop=desktop,
    )


def _default_app_class_resolver(app_type: str) -> Any | None:
    import ansys.aedt.core as aedt

    app_map = {
        "Hfss": aedt.Hfss,
        "Maxwell2d": aedt.Maxwell2d,
        "Maxwell3d": aedt.Maxwell3d,
        "Q3d": aedt.Q3d,
        "Q2d": aedt.Q2d,
        "Icepak": aedt.Icepak,
        "Circuit": aedt.Circuit,
        "TwinBuilder": aedt.TwinBuilder,
        "Mechanical": aedt.Mechanical,
        "Emit": aedt.Emit,
        "RMXprt": getattr(aedt, "Rmxprt", None),
        "Hfss3dLayout": aedt.Hfss3dLayout,
    }
    return app_map.get(app_type)


def _default_log_file_resolver() -> str | None:
    try:
        from ansys.aedt.core.aedt_logger import pyaedt_logger

        raw_logger = getattr(pyaedt_logger, "logger", None)
        for handler in getattr(raw_logger, "handlers", []):
            candidate = getattr(handler, "baseFilename", None)
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        candidate = getattr(pyaedt_logger, "filename", None)
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    except Exception:  # noqa: BLE001,S110 - PyAEDT logger internals vary by release
        pass
    try:
        from ansys.aedt.core import settings

        configured = getattr(settings, "logger_file_path", None)
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())
            if candidate.is_dir():
                logs = sorted(
                    candidate.glob("pyaedt*.log"),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
                if logs:
                    return str(logs[0].resolve())
    except Exception:  # noqa: BLE001,S110 - settings fallback is optional
        pass
    return None


def _open_viewer(path: Path) -> str | None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
            return None
    except Exception as exc:  # noqa: BLE001 - report platform viewer failures
        return f"Viewer launch failed: {exc}"
    return "Viewer launch is not supported on this platform."


class OfficialCapabilities:
    def __init__(
        self,
        *,
        app_resolver: Callable[..., Any] | None = None,
        app_class_resolver: Callable[[str], Any | None] | None = None,
        log_file_resolver: Callable[[], str | None] | None = None,
    ) -> None:
        self._app_resolver = app_resolver or _default_app_resolver
        self._app_class_resolver = app_class_resolver or _default_app_class_resolver
        self._log_file_resolver = log_file_resolver or _default_log_file_resolver
        self._exec_globals: dict[str, Any] = {}
        self._apps: list[Any] = []

    def clear_local_state(self) -> None:
        self._exec_globals.clear()
        self._apps.clear()

    def execute(
        self,
        command: str,
        *,
        desktop: Any,
        target: AedtTarget,
        arguments: Mapping[str, Any],
        connection_kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        if command not in OFFICIAL_BACKEND_COMMANDS:
            raise CapabilityError(f"unsupported official command: {command}")
        handler = getattr(self, f"_{command}")
        result = handler(desktop, target, arguments, connection_kwargs)
        safe = json_safe(result)
        if not isinstance(safe, dict):
            raise TypeError(f"AEDT command {command} returned a non-object result")
        return safe

    def _resolve_app(
        self, desktop: Any, project_name: str | None, design_name: str | None
    ) -> Any:
        app = self._app_resolver(
            desktop=desktop,
            project_name=project_name,
            design_name=design_name,
        )
        if app is None:
            raise CapabilityError("AEDT has no active design for this operation")
        return app

    @staticmethod
    def _target(target: AedtTarget) -> dict[str, Any]:
        return {"kind": target.kind, "value": target.value}

    def _connect_to_aedt(self, desktop, target, arguments, connection_kwargs):
        project_name = _optional_text(arguments, "project_name") or None
        design_name = _optional_text(arguments, "design_name") or None
        project = desktop.active_project()
        design = None
        try:
            design = desktop.active_design(project)
        except (IndexError, TypeError):
            pass
        result = {
            "target": self._target(target),
            "connected": True,
            "aedt_version": getattr(desktop, "aedt_version_id", None),
            "pid": getattr(desktop, "aedt_process_id", None),
            "port": getattr(desktop, "port", None),
            "grpc": bool(getattr(desktop, "is_grpc_api", target.kind == "port")),
            "active_project": _name(project),
            "active_design": _name(design),
        }
        if design_name:
            app = self._resolve_app(desktop, project_name, design_name)
            result.update(
                active_project=getattr(app, "project_name", project_name),
                active_design=getattr(app, "design_name", design_name),
                design_type=getattr(app, "design_type", None),
            )
        return result

    def _disconnect_from_aedt(self, desktop, target, arguments, connection_kwargs):
        close_projects = _boolean(arguments, "close_projects", False)
        close_desktop = _boolean(arguments, "close_desktop", False)
        pid = getattr(desktop, "aedt_process_id", None)
        pid_create_time: float | None = None
        if close_desktop and isinstance(pid, int) and pid > 0:
            try:
                import psutil

                pid_create_time = float(psutil.Process(pid).create_time())
            except Exception:  # noqa: BLE001 - process may already be gone
                pid_create_time = None
        release_error: str | None = None
        try:
            released = bool(
                desktop.release_desktop(
                    close_projects=close_projects,
                    close_on_exit=close_desktop,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report the failure, do not crash the tool
            released = False
            release_error = repr(exc)[:300]
        self.clear_local_state()
        exited: bool | None = None
        forced = False
        pid_reused = False
        if release_error is None and close_desktop and isinstance(pid, int) and pid > 0:
            import psutil
            import time

            exited = False
            for _ in range(40):
                if not psutil.pid_exists(pid):
                    exited = True
                    break
                time.sleep(0.25)
            if not exited:
                if pid_create_time is None:
                    # No pre-release identity snapshot: the PID may have been recycled. Do not
                    # terminate an unverified process; report it instead.
                    exited = False
                else:
                    try:
                        process = psutil.Process(pid)
                        name = (process.name() or "").lower()
                        if name not in {"ansysedt.exe", "ansysedt"}:
                            pid_reused = True
                            exited = True
                        elif abs(float(process.create_time()) - pid_create_time) > 2.0:
                            pid_reused = True
                            exited = True
                        else:
                            process.terminate()
                            for _ in range(20):
                                if not psutil.pid_exists(pid):
                                    exited = True
                                    break
                                time.sleep(0.25)
                            if not exited:
                                process.kill()
                                time.sleep(1.0)
                                exited = not psutil.pid_exists(pid)
                            forced = True
                    except Exception:  # noqa: BLE001 - report the truth below
                        exited = not psutil.pid_exists(pid)
        return {
            "target": self._target(target),
            "disconnected": True,
            "release_result": released,
            "closed_projects": close_projects,
            "closed_desktop": bool(exited) if close_desktop and release_error is None else False,
            "process_alive": (
                (not exited) if close_desktop and exited is not None else None
            ),
            "forced_termination": forced,
            "pid_reused": pid_reused,
            "release_error": release_error,
            "pid": pid,
        }

    def _list_projects(self, desktop, target, arguments, connection_kwargs):
        projects = [str(item) for item in desktop.project_list]
        return {
            "target": self._target(target),
            "open_projects": projects,
            "count": len(projects),
        }

    def _list_designs(self, desktop, target, arguments, connection_kwargs):
        requested = _optional_text(arguments, "project_name") or None
        projects = (
            [requested] if requested else [str(item) for item in desktop.project_list]
        )
        entries = []
        for project in projects:
            designs = [str(item) for item in desktop.design_list(project)]
            entries.append(
                {"project": project, "designs": designs, "count": len(designs)}
            )
        result = {
            "target": self._target(target),
            "projects": entries,
            "project_count": len(entries),
            "design_count": sum(item["count"] for item in entries),
        }
        if requested and entries:
            result.update(entries[0])
        return result

    def _open_project(self, desktop, target, arguments, connection_kwargs):
        path = Path(_required_text(arguments, "project_path")).expanduser().resolve()
        if not path.is_file():
            raise CapabilityError(f"project file not found: {path}")
        design_name = _optional_text(arguments, "design_name") or None
        loaded = desktop.load_project(str(path), design_name=design_name)
        return {
            "target": self._target(target),
            "opened": bool(loaded) if loaded is not None else True,
            "project_path": str(path),
            "active_design": design_name,
        }

    def _save_project(self, desktop, target, arguments, connection_kwargs):
        project_name = _optional_text(arguments, "project_name") or None
        if project_name is None:
            project_name = _name(desktop.active_project())
        if not project_name:
            raise CapabilityError("AEDT has no active project to save")
        save_as = (
            _optional_text(arguments, "save_as")
            or _optional_text(arguments, "path")
            or None
        )
        saved = bool(desktop.save_project(project_name, save_as))
        return {
            "target": self._target(target),
            "project_name": project_name,
            "path": save_as,
            "saved": saved,
        }

    def _clear_aedt(self, desktop, target, arguments, connection_kwargs):
        close_projects = _boolean(arguments, "close_projects", True)
        projects = [str(item) for item in desktop.project_list]
        closed = []
        if close_projects:
            for project in projects:
                desktop.odesktop.CloseProject(project)
                closed.append(project)
            self._apps.clear()
        clear_messages = getattr(desktop, "clear_messages", None)
        if callable(clear_messages):
            clear_messages()
        return {
            "target": self._target(target),
            "cleared": True,
            "closed_projects": closed,
            "project_count": len(closed),
        }

    def _get_model_info(self, desktop, target, arguments, connection_kwargs):
        requested_design = _optional_text(arguments, "design_name") or None
        project = desktop.active_project()
        design = None
        try:
            design = desktop.active_design(project)
        except (IndexError, TypeError):
            pass
        project_name = _name(project)
        design_name = requested_design or _name(design)
        design_type = None
        if design_name:
            try:
                design_type = desktop.design_type(design_name=design_name)
            except TypeError:
                design_type = desktop.design_type(project_name, design_name)
        project_path = None
        try:
            value = desktop.project_path
            project_path = value() if callable(value) else value
        except (AttributeError, TypeError):
            pass
        return {
            "target": self._target(target),
            "project_name": project_name,
            "design_name": design_name,
            "design_type": design_type,
            "project_path": project_path,
        }

    def _get_pyaedt_logs(self, desktop, target, arguments, connection_kwargs):
        tail_lines = arguments.get("tail_lines", 200)
        max_chars = arguments.get("max_chars", 40000)
        contains = _optional_text(arguments, "contains") or None
        if type(tail_lines) is not int or tail_lines <= 0:
            raise CapabilityError("tail_lines must be a positive integer")
        if type(max_chars) is not int or max_chars <= 0:
            raise CapabilityError("max_chars must be a positive integer")
        safe_tail = min(tail_lines, 5000)
        safe_chars = min(max_chars, 200000)
        log_file = self._log_file_resolver()
        payload = {
            "target": self._target(target),
            "log_file": log_file,
            "contains": contains,
            "total_lines": 0,
            "matched_lines": 0,
            "returned_lines": 0,
            "truncated": False,
            "logs": "",
        }
        if log_file and Path(log_file).is_file():
            all_lines = (
                Path(log_file)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines(keepends=True)
            )
            filtered = all_lines
            if contains:
                token = contains.lower()
                filtered = [line for line in all_lines if token in line.lower()]
            selected = filtered[-safe_tail:]
            log_text = "".join(selected)
            if len(log_text) > safe_chars:
                log_text = log_text[-safe_chars:]
                payload["truncated"] = True
            payload.update(
                total_lines=len(all_lines),
                matched_lines=len(filtered),
                returned_lines=len(selected),
                logs=log_text,
            )
        else:
            payload["file_log_error"] = "PyAEDT log file could not be resolved."

        try:
            project = desktop.active_project()
            design = desktop.active_design(project)
            project_name = _name(project) or ""
            design_name = _name(design) or ""
            payload["native_messages"] = {
                "project": project_name or None,
                "design": design_name or None,
                "info_messages": list(
                    desktop.odesktop.GetMessages(project_name, design_name, 0)
                ),
                "error_messages": list(
                    desktop.odesktop.GetMessages(project_name, design_name, 2)
                ),
            }
        except Exception as exc:  # noqa: BLE001 - AEDT native message APIs vary
            payload["native_messages_error"] = str(exc)
        return payload

    def _run_python_script(self, desktop, target, arguments, connection_kwargs):
        script = Path(_required_text(arguments, "script_path")).expanduser().resolve()
        if not script.is_file():
            raise CapabilityError(f"script file not found: {script}")
        from aedt_guard.audit import enforce_python_code as _enforce_python_code

        try:
            body = script.read_text(encoding="utf-8", errors="replace")[:2_000_000]
        except OSError as exc:
            raise CapabilityError(f"cannot read script for policy check: {exc}") from exc
        _verdict = _enforce_python_code(body, arguments)
        if not _verdict["allowed"]:
            raise CapabilityError("E_UNGUARDED_ANALYZE: " + _verdict["reason"])
        result = desktop.odesktop.RunScript(str(script))
        return {
            "target": self._target(target),
            "executed": True,
            "script_path": str(script),
            "result": result,
        }

    def _run_python_code(self, desktop, target, arguments, connection_kwargs):
        code = _required_text(arguments, "code")
        from aedt_guard.audit import enforce_python_code as _enforce_python_code

        _verdict = _enforce_python_code(
            code,
            arguments,
            project=_optional_text(arguments, "project_name") or None,
            design=_optional_text(arguments, "design_name") or None,
        )
        if not _verdict["allowed"]:
            raise CapabilityError("E_UNGUARDED_ANALYZE: " + _verdict["reason"])
        self._exec_globals.update(
            {
                "desktop": desktop,
                "odesktop": desktop.odesktop,
                "aedt_port": getattr(desktop, "port", None),
            }
        )
        self._exec_globals.pop("result", None)
        stdout = StringIO()
        stderr = StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(code, self._exec_globals)  # noqa: S102  # nosec B102 - explicit tool contract
        return {
            "target": self._target(target),
            "executed": True,
            "result": self._exec_globals.get("result"),
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
        }

    def _run_approved_builder(self, desktop, target, arguments, connection_kwargs):
        """Execute one operator-approved builder entrypoint with typed parameters.

        The worker loads the allowlist itself, reads the approved script bytes once, hashes those
        exact bytes and executes the compiled module; the caller cannot supply script path, hash or
        entrypoint. During execution the attach-only wrapper stays active, and the active project
        must remain inside the approved working/output roots.
        """
        import contextlib
        import types
        from io import StringIO

        from aedt_guard.builders import (
            get_builder,
            load_allowlist,
            sha256_bytes,
            validate_output_dir,
            validate_params,
            validate_project,
        )

        builder_id = _required_text(arguments, "builder_id")
        entry = get_builder(load_allowlist(), builder_id)
        params = validate_params(entry, arguments.get("params"))
        project = validate_project(_required_text(arguments, "project_path"))
        outdir = validate_output_dir(entry, _required_text(arguments, "outdir"))
        script = Path(entry["script"]).expanduser().resolve()
        data = script.read_bytes()
        actual = sha256_bytes(data)
        if actual != entry["sha256"].lower():
            raise CapabilityError(
                "builder script bytes changed after allowlist verification (expected %s, actual %s)"
                % (entry["sha256"], actual)
            )
        module = types.ModuleType("guard_approved_builder")
        module.__file__ = str(script)
        exec(compile(data, str(script), "exec"), module.__dict__)  # noqa: S102 - allowlisted bytes
        entrypoint = entry["entrypoint"]
        function = getattr(module, entrypoint, None)
        if not callable(function):
            raise CapabilityError("approved builder entrypoint is not callable: %s" % entrypoint)

        wrapper = contextlib.nullcontext()
        if hasattr(desktop, "is_grpc_api"):
            if not desktop.is_grpc_api:
                raise CapabilityError(
                    "approved builders require a verified gRPC desktop (COM attach cannot be guarded)"
                )
            from pyaedt_backend import (  # noqa: PLC0415 - avoid import cycle at module load
                _attach_only_desktop,
                _audited_desktop_module,
                _existing_target_owner,
            )

            expected_pid, expected_started = _existing_target_owner(
                {"new_desktop": False, "port": desktop.port}
            )
            wrapper = _attach_only_desktop(
                _audited_desktop_module(),
                {"new_desktop": False, "port": desktop.port},
                verified_owner=(expected_pid, expected_started),
                grpc_port_hint=desktop.port,
            )

        stdout = StringIO()
        stderr = StringIO()
        with wrapper:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = function(
                    port=getattr(desktop, "port", None),
                    project=project,
                    outdir=outdir,
                    **params,
                )

        # The build must not have attached/opened a project outside the working/output roots.
        active_path = None
        try:
            getter = getattr(desktop, "active_project_name", None)
            name = getter() if callable(getter) else getter
            path_getter = getattr(desktop, "project_path", None)
            if name and callable(path_getter):
                active_path = path_getter(name)
        except Exception:  # noqa: BLE001 - post-check is best effort on vendor quirks
            active_path = None
        allowed_dirs = [Path(project).resolve().parent, Path(outdir).resolve()]

        def _contained(candidate: str | None) -> bool:
            if not candidate:
                return True
            try:
                resolved = Path(str(candidate)).resolve()
            except OSError:
                return False
            return any(resolved == root or root in resolved.parents for root in allowed_dirs)

        if active_path and not _contained(active_path):
            raise CapabilityError(
                "approved builder opened a project outside the approved roots: %s" % active_path
            )
        # A builder can open more than one project; verify every open project path, not only active.
        try:
            names = getattr(desktop, "project_list", None)
            names = names() if callable(names) else names
            path_getter = getattr(desktop, "project_path", None)
            if isinstance(names, (list, tuple)) and callable(path_getter):
                for name in names:
                    candidate = path_getter(name)
                    if not _contained(candidate):
                        raise CapabilityError(
                            "approved builder has a project open outside the approved roots: %s"
                            % candidate
                        )
        except CapabilityError:
            raise
        except Exception:  # noqa: BLE001 - vendor quirks; active-project check still applies
            pass

        return {
            "target": self._target(target),
            "builder_id": builder_id,
            "script": str(script),
            "script_sha256": actual,
            "project": project,
            "outdir": outdir,
            "executed": True,
            "result": result,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
        }

    def _create_design(self, desktop, target, arguments, connection_kwargs):
        app_type = _required_text(arguments, "app_type")
        if app_type not in APPLICATION_TYPES:
            raise CapabilityError(
                f"unsupported application type: {app_type}; supported: "
                + ", ".join(APPLICATION_TYPES)
            )
        app_class = self._app_class_resolver(app_type)
        if app_class is None:
            raise CapabilityError(f"PyAEDT class is unavailable for {app_type}")
        design_name = _optional_text(arguments, "design_name") or None
        project_name = _optional_text(arguments, "project_name") or None
        solution_type = _optional_text(arguments, "solution_type") or None
        kwargs = dict(connection_kwargs)
        if design_name:
            kwargs["design"] = design_name
        if project_name:
            kwargs["project"] = project_name
        if solution_type:
            hfss_map = {
                "DrivenModal": "Modal",
                "DrivenTerminal": "Terminal",
            }
            kwargs["solution_type"] = (
                hfss_map.get(solution_type, solution_type)
                if app_type == "Hfss"
                else solution_type
            )
        app = app_class(**kwargs)
        self._apps.append(app)
        return {
            "target": self._target(target),
            "created": True,
            "app_type": app_type,
            "project_name": getattr(app, "project_name", project_name),
            "design_name": getattr(app, "design_name", design_name),
            "solution_type": getattr(app, "solution_type", solution_type),
        }

    @staticmethod
    def _validate_app(app):
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_file = Path(temporary_directory) / "validation.log"
            valid = bool(app.validate_simple(log_file=log_file))
            details = None
            if not valid:
                details = (
                    log_file.read_text(encoding="utf-8", errors="replace")
                    if log_file.is_file()
                    else "Design validation failed."
                )
            return valid, details

    def _validate_design(self, desktop, target, arguments, connection_kwargs):
        project_name = _optional_text(arguments, "project_name") or None
        design_name = _optional_text(arguments, "design_name") or None
        app = self._resolve_app(desktop, project_name, design_name)
        valid, details = self._validate_app(app)
        return {
            "target": self._target(target),
            "valid": valid,
            "project_name": getattr(app, "project_name", project_name),
            "design_name": getattr(app, "design_name", design_name),
            "details": details,
        }

    def _analyze_design(self, desktop, target, arguments, connection_kwargs):
        setup_name = _optional_text(arguments, "setup_name") or None
        project_name = _optional_text(arguments, "project_name") or None
        design_name = _optional_text(arguments, "design_name") or None
        from aedt_guard.audit import enforce_analyze as _enforce_analyze

        _verdict = _enforce_analyze(
            "analyze_design",
            arguments,
            project=project_name,
            design=design_name,
            setup=setup_name,
        )
        if not _verdict["allowed"]:
            raise CapabilityError("E_UNGUARDED_ANALYZE: " + _verdict["reason"])
        analyze_all = _boolean(arguments, "analyze_all_designs", False)
        if analyze_all:
            if setup_name:
                raise CapabilityError(
                    "setup_name cannot be used when analyze_all_designs is true"
                )
            accepted = bool(
                desktop.analyze_all(project=project_name, design=design_name)
            )
            return {
                "target": self._target(target),
                "mode": "analyze_all",
                "project_name": project_name,
                "design_name": design_name,
                "completed": False,
                "api_returned_success": accepted,
                "solver_completion_confirmed": False,
                "convergence_confirmed": False,
                "completion_evidence_required": "Inspect each setup's native profile and convergence for this submission; API success is not completion.",
            }

        app = self._resolve_app(desktop, project_name, design_name)
        valid, details = self._validate_app(app)
        if not valid:
            return {
                "target": self._target(target),
                "started": False,
                "solver_completion_confirmed": False,
                "convergence_confirmed": False,
                "validation_passed": False,
                "validation_details": details,
                "project_name": getattr(app, "project_name", project_name),
                "design_name": getattr(app, "design_name", design_name),
            }
        solve_in_batch = _boolean(arguments, "solve_in_batch", False)
        requested_cores = _positive_int(arguments, "num_cores")
        requested_tasks = _positive_int(arguments, "num_tasks")
        requested_gpus = _positive_int(arguments, "num_gpus")
        requested_auto_settings = _boolean(arguments, "use_auto_settings", True)
        acf_file = _optional_text(arguments, "acf_file") or None
        icepak_safe_mode = _boolean(arguments, "icepak_safe_mode", True)
        safe_mode_applied = bool(_is_icepak(app) and icepak_safe_mode and not acf_file)
        effective_cores = 1 if safe_mode_applied else requested_cores
        effective_tasks = 1 if safe_mode_applied and requested_tasks else requested_tasks
        effective_gpus = None if safe_mode_applied else requested_gpus
        effective_auto_settings = (
            False if safe_mode_applied else requested_auto_settings
        )
        adjustments = []
        if safe_mode_applied and requested_cores not in (None, 1):
            adjustments.append(
                f"Icepak safe mode changed num_cores from {requested_cores} to 1"
            )
        if safe_mode_applied and requested_tasks not in (None, 1):
            adjustments.append(
                f"Icepak safe mode changed num_tasks from {requested_tasks} to 1"
            )
        if safe_mode_applied and requested_gpus is not None:
            adjustments.append("Icepak safe mode disabled GPU allocation")
        if safe_mode_applied and requested_auto_settings:
            adjustments.append("Icepak safe mode disabled automatic DSO settings")
        revert_to_initial_mesh = _boolean(arguments, "revert_to_initial_mesh", False)
        save_before_solve = _boolean(arguments, "save_before_solve", False)
        setup_readback = _hfss_setup_readback(app, setup_name)
        if save_before_solve:
            # A save has reset Solver Type in this environment.  Do not silently
            # submit a changed or no longer readable solver/frequency setting.
            if app.save_project() is False:
                raise CapabilityError("Project save failed; no solve was submitted")
            after_save = _hfss_setup_readback(app, setup_name)
            _check_saved_hfss_setup(setup_readback, after_save)
            setup_readback = after_save
        setup_readback["scope"] = (
            "before_pyaedt_batch_save" if solve_in_batch else "immediately_before_interactive_submit"
        )
        submission_warnings = []
        if solve_in_batch:
            submission_warnings.append(
                "PyAEDT batch analyze() may save internally after this snapshot. "
                "This readback does not confirm the settings ultimately used by the batch solver."
            )
        if setup_readback["applicable"] and not setup_readback["readback_confirmed"]:
            submission_warnings.append("Native HFSS solver/frequency readback was incomplete; inspect the setup and native profile.")
        submitted_at_utc = datetime.now(timezone.utc).isoformat()
        if solve_in_batch:
            # Batch solve closes and reopens the .aedt file, so it needs a saved
            # project; keep the original PyAEDT path for that mode.
            started = bool(
                app.analyze(
                    setup=setup_name,
                    cores=effective_cores,
                    tasks=effective_tasks,
                    gpus=effective_gpus,
                    acf_file=acf_file,
                    use_auto_settings=effective_auto_settings,
                    solve_in_batch=True,
                    machine=_optional_text(arguments, "machine", "localhost")
                    or "localhost",
                    run_in_thread=_boolean(arguments, "run_in_thread", False),
                    revert_to_initial_mesh=revert_to_initial_mesh,
                    blocking=False,
                )
            )
        else:
            # Interactive solve. PyAEDT's analyze() calls save_project()
            # unconditionally (analysis.py), so calling analyze_setup() directly is
            # the only way to submit without saving first.
            started = bool(
                app.analyze_setup(
                    name=setup_name,
                    cores=effective_cores,
                    tasks=effective_tasks,
                    gpus=effective_gpus,
                    acf_file=acf_file,
                    use_auto_settings=effective_auto_settings,
                    revert_to_initial_mesh=revert_to_initial_mesh,
                    blocking=False,
                )
            )
        # v1.5 additive evidence fields (None = could not determine)
        ff_sphere_present: bool | None = None
        try:
            field_setups = getattr(app, "field_setup_names", None)
            if isinstance(field_setups, (list, tuple)):
                ff_sphere_present = bool(len(field_setups))
        except Exception:
            ff_sphere_present = None
        solution_data_present_before_submit: bool | None = None
        if setup_name:
            try:
                known = []
                for setup_obj in (getattr(app, "setups", None) or []):
                    if str(getattr(setup_obj, "name", "")) != str(setup_name):
                        continue
                    solved = getattr(setup_obj, "is_solved", None)
                    if solved is not None:
                        known.append(bool(solved))
                if known:
                    solution_data_present_before_submit = any(known)
            except Exception:
                # vendor internals can raise KeyError('HFSS Terminal Network') for
                # Terminal/mixed designs; keep the evidence field inconclusive.
                solution_data_present_before_submit = None
        return {
            "target": self._target(target),
            "started": started,
            "validation_passed": True,
            "project_name": getattr(app, "project_name", project_name),
            "design_name": getattr(app, "design_name", design_name),
            "setup_name": setup_name,
            "mode": "batch" if solve_in_batch else "interactive",
            "solver_completion_confirmed": False,
            "convergence_confirmed": False,
            "submitted_at_utc": submitted_at_utc,
            "ff_sphere_present": ff_sphere_present,
            "solution_data_present_before_submit": solution_data_present_before_submit,
            "setup_readback": setup_readback,
            "submission_warnings": submission_warnings,
            "completion_evidence_required": "Inspect the native profile and convergence for this exact submission; API success, idle state, or existing results are insufficient.",
            "icepak_safe_mode_applied": safe_mode_applied,
            "requested_resources": {
                "num_cores": requested_cores,
                "num_tasks": requested_tasks,
                "num_gpus": requested_gpus,
                "use_auto_settings": requested_auto_settings,
            },
            "effective_resources": {
                "num_cores": effective_cores,
                "num_tasks": effective_tasks,
                "num_gpus": effective_gpus,
                "use_auto_settings": effective_auto_settings,
            },
            "resource_adjustments": adjustments,
        }

    def _export_results(self, desktop, target, arguments, connection_kwargs):
        output = str(
            Path(_required_text(arguments, "output_path")).expanduser().resolve()
        )
        export_type = _optional_text(arguments, "export_type", "touchstone").lower()
        setup_name = _optional_text(arguments, "setup_name") or None
        sweep_name = _optional_text(arguments, "sweep_name") or None
        methods = {
            "touchstone": "export_touchstone",
            "profile": "export_profile",
            "convergence": "export_convergence",
            "mesh": "export_mesh_stats",
        }
        if export_type not in methods:
            raise CapabilityError(
                "export_type must be touchstone, profile, convergence, or mesh"
            )
        app = self._resolve_app(desktop, None, None)
        if export_type != "touchstone" and not setup_name:
            get_setups = getattr(app, "get_setups", None)
            setups = list(get_setups()) if callable(get_setups) else []
            if len(setups) == 1:
                setup_name = str(setups[0])
            elif not setups:
                raise CapabilityError(
                    f"{export_type} export requires a solved setup, but none exists"
                )
            else:
                raise CapabilityError(
                    f"{export_type} export requires setup_name because multiple "
                    f"setups exist: {', '.join(map(str, setups))}"
                )
        output_path = Path(output)
        details = None
        if _is_icepak(app) and export_type == "convergence":
            details = _export_icepak_convergence(app, output_path)
            result = details["path"]
            export_method = details["source"]
        elif _is_icepak(app) and export_type == "mesh":
            details = _export_icepak_mesh_stats(app, str(setup_name), output_path)
            result = details["path"]
            export_method = details["source"]
        else:
            method = getattr(app, methods[export_type], None)
            if not callable(method):
                raise CapabilityError(
                    f"{export_type} export is unavailable for {type(app).__name__} designs"
                )
            kwargs = {"output_file": output}
            if setup_name:
                kwargs["setup"] = setup_name
            if sweep_name and export_type == "touchstone":
                kwargs["sweep"] = sweep_name
            # ExportNetworkData fails intermittently right after a solve (gRPC
            # backend_error) and a swept export occasionally writes only the
            # adaptive point. Retry before declaring a solved run failed, and
            # report the row count so the caller can fail closed instead of
            # silently computing a zero bandwidth from a single row.
            attempts = 1
            result = None
            last_error: Exception | None = None
            for attempt in range(3):
                attempts = attempt + 1
                try:
                    result = method(**kwargs)
                    last_error = None
                except Exception as exc:  # noqa: BLE001 - vendor errors vary
                    last_error = exc
                    result = None
                if last_error is not None:
                    time.sleep(2.0)
                    continue
                if export_type != "touchstone" or not output_path.is_file():
                    break
                rows = _count_touchstone_rows(output_path)
                if not sweep_name or rows >= 3:
                    break
                time.sleep(2.0)
            if last_error is not None and result is None:
                raise CapabilityError(
                    "export_touchstone failed after %d attempts: %s: %s"
                    % (attempts, type(last_error).__name__, str(last_error)[:200])
                )
            export_method = methods[export_type]
            if export_type == "touchstone":
                expected_rows = None
                if sweep_name:
                    try:
                        get_setup = getattr(app, "get_setup", None)
                        setup_obj = get_setup(setup_name) if (callable(get_setup) and setup_name) else None
                        get_sweep = getattr(setup_obj, "get_sweep", None)
                        sweep_obj = get_sweep(sweep_name) if callable(get_sweep) else None
                        freqs = list(getattr(sweep_obj, "frequency", []) or [])
                        expected_rows = len(freqs) or None
                    except Exception:  # noqa: BLE001 - vendor objects vary
                        expected_rows = None
                details = {
                    "row_count": _count_touchstone_rows(output_path) if output_path.is_file()
                    else 0,
                    "expected_rows": expected_rows,
                    "attempts": attempts,
                    "sweep_name": sweep_name,
                }
        evidence = None
        if str(getattr(app, "design_type", "")).strip().casefold() == "hfss" and output_path.is_file():
            if export_type in {"profile", "convergence"}:
                content = output_path.read_text(encoding="utf-8", errors="replace")
                parser = summarize_hfss_profile if export_type == "profile" else summarize_hfss_convergence
                evidence = parser(content)
        return {
            "target": self._target(target),
            "export_type": export_type,
            "setup_name": setup_name,
            "output_path": output,
            "result": result,
            "file_exists": output_path.exists(),
            "export_method": export_method,
            "details": details,
            "evidence_summary": evidence,
            "project_name": getattr(app, "project_name", None),
            "design_name": getattr(app, "design_name", None),
            "solver_completion_confirmed": False,
            "convergence_confirmed": False,
            "evidence_note": "Exports can belong to an earlier solve. Summaries describe the artifact only; link profile, convergence, frequency and project identity to the intended submission before accepting results.",
        }

    def _screenshot(self, desktop, target, arguments, connection_kwargs):
        raw_path = (
            _optional_text(arguments, "path", "screenshot.jpg") or "screenshot.jpg"
        )
        output = Path(raw_path).expanduser().resolve()
        if output.suffix.lower() not in {".jpg", ".jpeg"}:
            output = output.with_suffix(".jpg")
        resolution = _optional_text(arguments, "resolution", "1080p") or "1080p"
        if resolution not in {"1080p", "4k"}:
            raise CapabilityError("resolution must be 1080p or 4k")
        plot_type = _optional_text(arguments, "plot_type", "model") or "model"
        if plot_type not in {"model", "field", "mesh"}:
            raise CapabilityError("plot_type must be model, field, or mesh")
        project = _optional_text(arguments, "project") or None
        design = _optional_text(arguments, "design") or None
        open_viewer = _boolean(arguments, "open_viewer", True)
        app = self._resolve_app(desktop, project, design)
        width, height = (3840, 2160) if resolution == "4k" else (1920, 1080)
        output.parent.mkdir(parents=True, exist_ok=True)
        app.post.export_model_picture(
            full_name=str(output),
            width=width,
            height=height,
        )
        if not output.is_file():
            raise RuntimeError(f"screenshot file was not created: {output}")
        viewer_error = _open_viewer(output) if open_viewer else None
        return {
            "target": self._target(target),
            "path": str(output),
            "project": getattr(app, "project_name", project),
            "design": getattr(app, "design_name", design),
            "plot_type": plot_type,
            "resolution": resolution,
            "mime_type": "image/jpeg",
            "data_base64": base64.b64encode(output.read_bytes()).decode("ascii"),
            "viewer_opened": bool(open_viewer and viewer_error is None),
            "viewer_error": viewer_error,
        }

    def _export_config(self, desktop, target, arguments, connection_kwargs):
        output = _optional_text(arguments, "output") or None
        project = _optional_text(arguments, "project") or None
        design = _optional_text(arguments, "design") or None
        overwrite = _boolean(arguments, "overwrite", False)
        app = self._resolve_app(desktop, project, design)
        temporary = False
        if output:
            target_path = Path(output).expanduser().resolve()
            if target_path.suffix.lower() != ".json":
                target_path = target_path.with_suffix(".json")
        else:
            descriptor, temporary_name = tempfile.mkstemp(suffix=".json")
            os.close(descriptor)
            target_path = Path(temporary_name)
            target_path.unlink(missing_ok=True)
            temporary = True
        try:
            config_file = app.configurations.export_config(
                config_file=str(target_path),
                overwrite=overwrite,
            )
            if not config_file:
                raise RuntimeError("PyAEDT did not export a configuration file")
            config_path = Path(config_file)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            result = {
                "target": self._target(target),
                "config": config,
                "design": getattr(app, "design_name", design),
                "project": getattr(app, "project_name", project),
            }
            if output:
                result["config_file"] = str(config_path.resolve())
            return result
        finally:
            if temporary:
                target_path.unlink(missing_ok=True)

