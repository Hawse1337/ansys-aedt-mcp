"""Managed AEDT session registry.

The guard only protects sessions it can *prove* it launched. This module keeps a
small state file recording every AEDT process started through the guard (PID,
process creation time, gRPC port, launch source) and refuses attach attempts to
sessions that are not in that registry. A same-privilege process can still start
AEDT directly; the registry turns that from a silent bypass into a visible one
(``guard_status`` lists it as unmanaged) and makes attach-by-the-guard fail
unless the operator explicitly opts in with ``AEDT_MCP_ALLOW_UNMANAGED=1``.

This is a bookkeeping and fail-closed control, not an OS sandbox.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_TRUE = {"1", "true", "yes", "on"}

REGISTRY_NAME = "managed_sessions.json"


def state_dir() -> Path:
    override = os.environ.get("AEDT_MCP_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "ansys-aedt-mcp"


def registry_path() -> Path:
    return state_dir() / REGISTRY_NAME


def allow_unmanaged() -> bool:
    return os.environ.get("AEDT_MCP_ALLOW_UNMANAGED", "").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> list[dict[str, Any]]:
    path = registry_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("sessions") if isinstance(data, dict) else None
    return [record for record in records or [] if isinstance(record, dict)]


def _write(records: Iterable[dict[str, Any]]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_utc": _now(), "sessions": list(records)}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _process_identity(pid: int) -> tuple[str, float] | None:
    """Return (name, create_time) for a live process, or None."""
    try:
        import psutil
    except Exception:  # noqa: BLE001 - psutil is a hard dependency of the guard, but stay safe
        return None
    try:
        process = psutil.Process(int(pid))
        return process.name().lower(), float(process.create_time())
    except Exception:  # noqa: BLE001 - vanished or inaccessible
        return None


def _is_alive(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    identity = _process_identity(pid)
    if identity is None:
        return False
    name, created = identity
    if name not in {"ansysedt.exe", "ansysedt"}:
        return False
    recorded = record.get("create_time")
    if isinstance(recorded, (int, float)) and abs(float(recorded) - created) > 1.0:
        return False
    return True


def prune() -> list[dict[str, Any]]:
    records = [record for record in _read() if _is_alive(record)]
    _write(records)
    return records


def record_launch(
    *,
    pid: int,
    port: int | None = None,
    version: str | None = None,
    non_graphical: bool | None = None,
    source: str = "guard",
) -> dict[str, Any]:
    """Record a guard-launched AEDT session. Best-effort identity from psutil."""
    identity = _process_identity(int(pid))
    record = {
        "pid": int(pid),
        "port": int(port) if isinstance(port, int) and port > 0 else None,
        "create_time": identity[1] if identity else None,
        "name": identity[0] if identity else None,
        "version": version,
        "non_graphical": bool(non_graphical) if non_graphical is not None else None,
        "source": str(source),
        "recorded_utc": _now(),
        "recorded_monotonic": time.monotonic(),
    }
    records = [existing for existing in _read() if existing.get("pid") != record["pid"]]
    records.append(record)
    _write(records)
    return record


def list_managed(*, prune_dead: bool = True) -> list[dict[str, Any]]:
    records = prune() if prune_dead else _read()
    return sorted(records, key=lambda item: item.get("recorded_utc") or "")


def is_managed(*, pid: int | None = None, port: int | None = None) -> bool:
    records = prune()
    if pid is not None:
        return any(record.get("pid") == int(pid) for record in records)
    if port is not None:
        return any(record.get("port") == int(port) for record in records)
    return False


def managed_record(*, pid: int | None = None, port: int | None = None) -> dict[str, Any] | None:
    records = prune()
    for record in records:
        if pid is not None and record.get("pid") == int(pid):
            return record
        if port is not None and record.get("port") == int(port):
            return record
    return None


def scan_aedt_processes() -> dict[str, Any]:
    """Classify every live AEDT process as managed or unmanaged."""
    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        return {"error": "psutil unavailable: %s" % exc, "managed": [], "unmanaged": []}

    managed = prune()
    managed_pids = {record.get("pid") for record in managed if _is_alive(record)}
    managed_map = {record.get("pid"): record for record in managed}
    found: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            name = (process.info.get("name") or "").lower()
            if name not in {"ansysedt.exe", "ansysedt"}:
                continue
            command_line = process.info.get("cmdline") or []
            port = None
            for index, argument in enumerate(command_line[:-1]):
                if isinstance(argument, str) and argument.lower() == "-grpcsrv":
                    value = command_line[index + 1]
                    if isinstance(value, str) and value.isdecimal():
                        port = int(value)
                    break
            found.append({
                "pid": int(process.info["pid"]),
                "create_time": process.info.get("create_time"),
                "port": port,
                "managed": int(process.info["pid"]) in managed_pids,
                "record": managed_map.get(int(process.info["pid"])),
            })
        except Exception:  # noqa: BLE001 - process vanished
            continue
    discovered = sorted(found, key=lambda item: item["pid"])
    return {
        "managed": [item for item in discovered if item["managed"]],
        "unmanaged": [item for item in discovered if not item["managed"]],
        "enforcement": "allow_unmanaged" if allow_unmanaged() else "managed_only",
        "state_dir": str(state_dir()),
    }


def enforce_target(target: Any) -> None:
    """Raise when the guard is asked to attach to a session it did not launch.

    ``target`` is the backend ``AedtTarget`` (``kind`` is ``pid`` or ``port``).
    The escape hatch is ``AEDT_MCP_ALLOW_UNMANAGED=1``; it is meant for a human
    who explicitly wants the guard to attach to a session they started.
    """
    from pyaedt_backend import BackendCommandError

    if allow_unmanaged():
        return
    kind = getattr(target, "kind", None)
    value = getattr(target, "value", None)
    if kind == "pid":
        if not is_managed(pid=int(value)):
            raise BackendCommandError(
                "refusing to attach to AEDT PID %s: it was not launched by this guard. "
                "Launch through the guard, or set AEDT_MCP_ALLOW_UNMANAGED=1 for an explicit "
                "human-approved attach to a session you started." % value
            )
        return
    if kind == "port":
        if not is_managed(port=int(value)):
            raise BackendCommandError(
                "refusing to attach to gRPC port %s: no guard-launched AEDT session owns that port. "
                "Launch through the guard, or set AEDT_MCP_ALLOW_UNMANAGED=1 for an explicit "
                "human-approved attach to a session you started." % value
            )
        return
    raise BackendCommandError("unsupported AEDT target: %r" % (target,))
