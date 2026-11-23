"""Managed AEDT session registry.

The guard only protects sessions it can *prove* it launched. This module keeps a
small state file recording every AEDT process started through the guard (PID,
process creation time, gRPC port, launch source) and refuses attach attempts to
sessions that are not in that registry. A same-privilege process can still start
AEDT directly; the registry turns that from a silent bypass into a visible one
(``guard_status`` lists it as unmanaged) and makes attach-by-the-guard fail
unless the operator explicitly opts in with ``AEDT_MCP_ALLOW_UNMANAGED=1``.

This is bookkeeping and a fail-closed control, not an OS sandbox. The registry is
a file in the user profile, so a same-user shell can forge it; treat the guard as
an integrity/visibility control and use OS isolation for a hard boundary.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from guard_file_lock import file_lock

_TRUE = {"1", "true", "yes", "on"}
_MAX_CREATE_TIME_DRIFT = 2.0
REGISTRY_NAME = "managed_sessions.json"
LOCK_NAME = "managed_sessions.lock"

def state_dir() -> Path:
    override = os.environ.get("AEDT_MCP_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return Path(os.path.abspath(os.path.expanduser(str(root / "ansys-aedt-mcp"))))


def registry_path() -> Path:
    return state_dir() / REGISTRY_NAME


def allow_unmanaged() -> bool:
    return os.environ.get("AEDT_MCP_ALLOW_UNMANAGED", "").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _locked():
    """Serialize registry read-modify-write operations across guard processes."""
    return file_lock(state_dir() / LOCK_NAME)


def _read() -> list[dict[str, Any]]:
    path = registry_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("sessions") if isinstance(data, dict) else None
    valid: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        created = record.get("create_time")
        port = record.get("port")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        if not isinstance(created, (int, float)) or isinstance(created, bool):
            continue
        if port is not None and (not isinstance(port, int) or isinstance(port, bool)):
            continue
        valid.append(record)
    return valid


def _write(records: Iterable[dict[str, Any]]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_utc": _now(), "sessions": list(records)}
    handle, temporary_name = tempfile.mkstemp(
        prefix="managed_sessions.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=1))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


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
    """Fail closed: identity must be present and match a live ansysedt process."""
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    recorded = record.get("create_time")
    if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
        return False
    identity = _process_identity(pid)
    if identity is None:
        return False
    name, created = identity
    if name not in {"ansysedt.exe", "ansysedt"}:
        return False
    return abs(float(recorded) - created) <= _MAX_CREATE_TIME_DRIFT


def _port_owner(port: int) -> int | None:
    """Return the PID listening on a loopback/any TCP port, or None."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return None
    owners: set[int] = set()
    try:
        for connection in psutil.net_connections(kind="tcp"):
            address = connection.laddr
            address_port = getattr(address, "port", None)
            if address_port is None and isinstance(address, tuple) and len(address) >= 2:
                address_port = address[1]
            if str(connection.status).upper() == "LISTEN" and address_port == int(port):
                if isinstance(connection.pid, int) and connection.pid > 0:
                    owners.add(connection.pid)
    except Exception:  # noqa: BLE001
        return None
    if len(owners) != 1:
        return None
    return owners.pop()


def prune() -> list[dict[str, Any]]:
    """Drop dead records and persist only when the set changed."""
    with _locked():
        current = _read()
        records = [record for record in current if _is_alive(record)]
        if len(records) != len(current):
            _write(records)
        return records


def discard_launch(pid: int) -> None:
    """Remove a prematurely recorded launch (failed readiness handshake)."""
    with _locked():
        records = [record for record in _read() if record.get("pid") != int(pid)]
        _write(records)


def record_launch(
    *,
    pid: int,
    port: int | None = None,
    version: str | None = None,
    non_graphical: bool | None = None,
    source: str = "guard",
) -> dict[str, Any]:
    """Record a guard-launched AEDT session. Fails closed by omitting identity."""
    identity = _process_identity(int(pid))
    created = identity[1] if identity else None
    record = {
        "pid": int(pid),
        "port": int(port) if isinstance(port, int) and port > 0 else None,
        "create_time": created if isinstance(created, (int, float)) else None,
        "name": identity[0] if identity else None,
        "version": version,
        "non_graphical": bool(non_graphical) if non_graphical is not None else None,
        "source": str(source),
        "verified": False,
        "recorded_utc": _now(),
        "recorded_monotonic": time.monotonic(),
    }
    with _locked():
        records = [existing for existing in _read()
                   if existing.get("pid") != record["pid"] and _is_alive(existing)]
        records.append(record)
        _write(records)
    return record


def mark_verified(*, pid: int | None = None, port: int | None = None) -> bool:
    """Mark a recorded session as verified after the readiness handshake."""
    with _locked():
        records = _read()
        changed = False
        for record in records:
            if pid is not None and record.get("pid") == int(pid):
                record["verified"] = True
                changed = True
            elif port is not None and record.get("port") == int(port):
                record["verified"] = True
                changed = True
        if changed:
            _write(records)
        return changed


def list_managed(*, prune_dead: bool = False) -> list[dict[str, Any]]:
    records = prune() if prune_dead else _read()
    alive = [record for record in records if _is_alive(record)]
    return sorted(alive, key=lambda item: item.get("recorded_utc") or "")


def _fresh(record: dict[str, Any], window_s: float = 900.0) -> bool:
    try:
        recorded = float(record.get("recorded_monotonic"))
    except (TypeError, ValueError):
        return False
    return 0.0 <= time.monotonic() - recorded <= window_s


def is_managed(*, pid: int | None = None, port: int | None = None) -> bool:
    """True only when a live, verified record proves ownership of the PID/port.

    A spawn-time record is accepted for PID attaches only while a launch is still in
    flight (its readiness probe must pass); port attaches always need verification.
    """
    records = _read()
    if pid is not None:
        for record in records:
            if record.get("pid") != int(pid):
                continue
            if not _is_alive(record):
                continue
            if record.get("verified") is True:
                return True
            if record.get("source") == "aedt_launcher.spawn" and _fresh(record):
                return True
            continue
        return False
    if port is not None:
        owner = _port_owner(int(port))
        if owner is None:
            return False
        for record in records:
            if record.get("port") != int(port):
                continue
            if record.get("pid") != owner:
                continue
            if not _is_alive(record):
                continue
            if record.get("verified") is True:
                return True
            continue
        return False
    return False


def managed_record(*, pid: int | None = None, port: int | None = None) -> dict[str, Any] | None:
    for record in list_managed(prune_dead=False):
        if pid is not None and record.get("pid") == int(pid):
            return record
        if port is not None and record.get("port") == int(port):
            return record
    return None


def scan_aedt_processes(*, prune_dead: bool = False) -> dict[str, Any]:
    """Classify every live AEDT process as managed or unmanaged (read-only by default)."""
    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        return {"error": "psutil unavailable: %s" % exc, "managed": [], "unmanaged": []}

    records = prune() if prune_dead else _read()
    alive_records = [record for record in records if _is_alive(record)]
    managed_pids = {record.get("pid") for record in alive_records}
    managed_map = {record.get("pid"): record for record in alive_records}
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
    """Raise when the guard is asked to attach to a session it did not launch."""
    from pyaedt_backend import BackendCommandError

    if allow_unmanaged():
        return
    kind = getattr(target, "kind", None)
    value = getattr(target, "value", None)
    if kind == "pid":
        if not is_managed(pid=int(value)):
            raise BackendCommandError(
                "refusing to attach to AEDT PID %s: it was not launched by this guard. "
                "Launch through the guard; attaching to a session you started requires an "
                "explicit human operator decision in the guard configuration." % value
            )
        return
    if kind == "port":
        if not is_managed(port=int(value)):
            raise BackendCommandError(
                "refusing to attach to gRPC port %s: no guard-launched AEDT session owns that port. "
                "Launch through the guard; attaching to a session you started requires an "
                "explicit human operator decision in the guard configuration." % value
            )
        return
    raise BackendCommandError("unsupported AEDT target: %r" % (target,))
