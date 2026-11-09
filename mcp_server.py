#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from aedt_guard import (
    SpecError,
    Store,
    evaluate as guard_evaluate,
    hard_metrics,
    load_spec,
    new_spec,
    resolve_frequencies,
    resolve_solver_settings,
    save_spec,
    task_context,
    update_spec,
)
from aedt_guard.archive import (
    commit_best,
    default_objectives,
    is_feasible,
    pareto_front,
    rank,
    read_best,
)
from aedt_guard.audit import summarize as audit_summary
from aedt_guard.sim import CampaignRunner, hard_results, last_stage, named_metrics
from aedt_guard.tokens import issue_token, revoke_token
from aedt_guidelines import get_guidelines
from aedt_installation import check_aedt_installation
from aedt_launcher import AedtLauncher
from aedt_target import AedtTarget
from session_discovery import SessionDiscovery
from worker_client import WorkerClient
from maxwell_tools import register_maxwell_tools

AEDTAppType = Literal[
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
]
GuidelinesContent = Literal[
    "workflow",
    "hfss",
    "maxwell",
    "icepak",
    "circuit",
    "geometry",
    "mesh",
    "boundaries",
    "postprocessing",
    "parametric",
]


INSTRUCTIONS = """Control Ansys Electronics Desktop through external PyAEDT brokers.

Call check_aedt_installed and check_aedt_status first. Every operation that
touches AEDT requires exactly one explicit PID or gRPC port; there is no implicit
or automatic session target. Prefer the gRPC port returned by launch_aedt for
MCP-launched sessions. The official PyAEDT-style tools support HFSS, Maxwell,
Icepak, Q2D/Q3D, Circuit, Twin Builder, Mechanical, Emit, RMxprt, and HFSS 3D
Layout. Each target reuses one external broker. Call disconnect_from_aedt or
release_connection when finished; MCP shutdown also releases PyAEDT.
For Maxwell, use the maxwell_* tools with explicit project_name/design_name.
Create geometry first, assign coils, create a winding, then link named coils.
Motion tools assign an existing enclosing band in a magnetic Transient design.
Validate before analyze_design. API acceptance, cached properties, existing
results, and desktop idle state are not proof of latest-solve completion.
"""

mcp = FastMCP("ansys-aedt-mcp-server", instructions=INSTRUCTIONS)
worker_client = WorkerClient()
session_discovery = SessionDiscovery(worker_client=worker_client)
aedt_launcher = AedtLauncher(worker_client=worker_client)


def _target(pid: int | None, port: int | None) -> AedtTarget:
    return AedtTarget.from_values(pid=pid, port=port)


async def _worker_call(
    command: str,
    *,
    pid: int | None,
    port: int | None,
    arguments: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    target = _target(pid, port)
    result = await worker_client.execute_async(
        target,
        command,
        arguments or {},
        timeout=timeout,
    )
    if not isinstance(result, dict):
        raise TypeError(f"AEDT worker command {command} returned a non-object result")
    return result


@mcp.tool()
async def list_aedt_sessions() -> dict[str, Any]:
    """List all detected AEDT processes and listener ports without attaching."""
    sessions = await asyncio.to_thread(session_discovery.list_sessions)
    return {"sessions": sessions, "selection_required": True}


@mcp.tool()
async def check_aedt_installed() -> dict[str, Any]:
    """Check local AEDT and PyAEDT installations without starting AEDT."""
    # PyAEDT's Windows installation discovery may touch thread-affine registry
    # state, so keep this short read-only probe on the MCP request thread.
    return check_aedt_installation()


@mcp.tool()
async def check_aedt_status(
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Discover AEDT sessions, or probe one explicitly selected target."""
    if pid is None and port is None:
        sessions = await asyncio.to_thread(session_discovery.list_sessions)
        return {
            "connected": False,
            "sessions": sessions,
            "session_count": len(sessions),
            "selection_required": bool(sessions),
        }
    return await _worker_call("ping", pid=pid, port=port, timeout=timeout)


@mcp.tool()
async def launch_aedt(
    version: str | None = None,
    non_graphical: bool = False,
    confirm_new_session: bool | None = None,
    application: AEDTAppType | None = None,
    port: int = 0,
    install_dir: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Launch an AEDT gRPC session, optionally opening a solver application."""
    sessions = await asyncio.to_thread(session_discovery.list_sessions)
    if sessions and confirm_new_session is False:
        return {
            "launched": False,
            "confirmation_required": True,
            "message": (
                "AEDT is already running. Select an existing PID/port, or call "
                "launch_aedt again with confirm_new_session=true."
            ),
            "sessions": sessions,
        }
    selected_version = version or "2026.1"
    result = await asyncio.to_thread(
        aedt_launcher.launch,
        version=selected_version,
        port=port,
        install_dir=install_dir or None,
        non_graphical=non_graphical,
        timeout=timeout,
    )
    result["launched"] = True
    if application:
        result["application"] = await _worker_call(
            "create_design",
            pid=None,
            port=int(result["port"]),
            arguments={"app_type": application},
            timeout=timeout,
        )
    return result


@mcp.tool()
async def connect_to_aedt(
    pid: int | None = None,
    port: int | None = None,
    machine: str = "localhost",
    version: str | None = None,
    non_graphical: bool = True,
    project_name: str | None = None,
    design_name: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Connect a broker to one explicit local AEDT PID or gRPC port."""
    if machine.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(
            "this explicit-target AEDT MCP currently supports local sessions only"
        )
    return await _worker_call(
        "connect_to_aedt",
        pid=pid,
        port=port,
        arguments={
            "machine": machine,
            "version": version,
            "non_graphical": non_graphical,
            "project_name": project_name,
            "design_name": design_name,
        },
        timeout=timeout,
    )


@mcp.tool()
async def disconnect_from_aedt(
    pid: int | None = None,
    port: int | None = None,
    close_projects: bool = False,
    close_desktop: bool | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Disconnect one broker, explicitly choosing whether AEDT should close."""
    if close_desktop is None:
        return {
            "disconnected": False,
            "choice_required": True,
            "message": (
                "Set close_desktop=false to keep AEDT open or true to close it."
            ),
        }
    target = _target(pid, port)
    result = await _worker_call(
        "disconnect_from_aedt",
        pid=pid,
        port=port,
        arguments={
            "close_projects": close_projects,
            "close_desktop": close_desktop,
        },
        timeout=timeout,
    )
    await worker_client.release_async(target, timeout=timeout)
    return result


@mcp.tool()
async def check_aedt_connection(
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run a real PyAEDT health probe against one explicit PID or gRPC port."""
    return await _worker_call("ping", pid=pid, port=port, timeout=timeout)


@mcp.tool()
async def release_connection(
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Release the broker for one explicit AEDT target without closing AEDT."""
    target = _target(pid, port)
    return await worker_client.release_async(target, timeout=timeout)


@mcp.tool()
async def get_pyaedt_logs(
    tail_lines: int = 200,
    contains: str | None = None,
    max_chars: int = 40000,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return PyAEDT file logs and native AEDT messages for one target."""
    return await _worker_call(
        "get_pyaedt_logs",
        pid=pid,
        port=port,
        arguments={
            "tail_lines": tail_lines,
            "contains": contains,
            "max_chars": max_chars,
        },
        timeout=timeout,
    )


@mcp.tool()
async def run_python_script(
    script_path: str,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Execute a Python script through the selected AEDT Desktop session."""
    return await _worker_call(
        "run_python_script",
        pid=pid,
        port=port,
        arguments={"script_path": script_path},
        timeout=timeout,
    )


@mcp.tool()
async def run_python_code(
    code: str,
    pid: int | None = None,
    port: int | None = None,
    run_token: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Execute inline Python in the persistent PyAEDT broker namespace."""
    return await _worker_call(
        "run_python_code",
        pid=pid,
        port=port,
        arguments={"code": code, **({"run_token": run_token} if run_token else {})},
        timeout=timeout,
    )


@mcp.tool()
async def list_projects(
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """List all open projects in one explicit AEDT target."""
    return await _worker_call("list_projects", pid=pid, port=port, timeout=timeout)


@mcp.tool()
async def list_designs(
    project_name: str | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """List designs in one project or across all open projects."""
    return await _worker_call(
        "list_designs",
        pid=pid,
        port=port,
        arguments={"project_name": project_name},
        timeout=timeout,
    )


@mcp.tool()
async def open_project(
    project_path: str,
    design_name: str | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Open an AEDT project and optionally activate a named design."""
    return await _worker_call(
        "open_project",
        pid=pid,
        port=port,
        arguments={
            "project_path": project_path,
            "design_name": design_name,
        },
        timeout=timeout,
    )


@mcp.tool()
async def create_design(
    app_type: AEDTAppType,
    design_name: str | None = None,
    project_name: str | None = None,
    solution_type: str | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Create HFSS, Maxwell, Icepak, Q2D/Q3D, Circuit, or another AEDT design."""
    return await _worker_call(
        "create_design",
        pid=pid,
        port=port,
        arguments={
            "app_type": app_type,
            "design_name": design_name,
            "project_name": project_name,
            "solution_type": solution_type,
        },
        timeout=timeout,
    )


@mcp.tool()
async def get_project_info(
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return project and active design metadata for one explicit AEDT target."""
    return await _worker_call("project_info", pid=pid, port=port, timeout=timeout)


@mcp.tool()
async def close_projects(
    project_names: list[str],
    save: bool = False,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Close only the named projects in one explicit AEDT target."""
    return await _worker_call(
        "close_projects",
        pid=pid,
        port=port,
        arguments={"project_names": project_names, "save": save},
        timeout=timeout,
    )


@mcp.tool()
async def create_hfss_design(
    project_name: str,
    design_name: str,
    solution_type: str = "DrivenModal",
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Create or activate a named HFSS design in one explicit AEDT target."""
    return await _worker_call(
        "create_hfss_design",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
            "solution_type": solution_type,
        },
        timeout=timeout,
    )


@mcp.tool()
async def save_project(
    project_name: str | None = None,
    save_as: str | None = None,
    path: str = "",
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Save a named or active project, optionally to a new path."""
    return await _worker_call(
        "save_project",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "save_as": save_as,
            "path": path,
        },
        timeout=timeout,
    )


@mcp.tool()
async def validate_design(
    project_name: str | None = None,
    design_name: str | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Validate an AEDT design without starting a solve."""
    return await _worker_call(
        "validate_design",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
        },
        timeout=timeout,
    )


@mcp.tool()
async def analyze_design(
    setup_name: str | None = None,
    project_name: str | None = None,
    design_name: str | None = None,
    num_cores: int | None = None,
    num_tasks: int | None = None,
    num_gpus: int | None = None,
    acf_file: str | None = None,
    use_auto_settings: bool = True,
    solve_in_batch: bool = False,
    machine: str = "localhost",
    run_in_thread: bool = False,
    revert_to_initial_mesh: bool = False,
    analyze_all_designs: bool = False,
    icepak_safe_mode: bool = True,
    save_before_solve: bool = False,
    blocking: bool = False,
    blocking_timeout_s: float | None = None,
    blocking_poll_interval_s: float = 5.0,
    pid: int | None = None,
    port: int | None = None,
    run_token: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Validate and submit an analysis; Icepak uses safe single-core defaults.

    ``save_before_solve`` defaults to ``False`` to avoid an implicit project
    write. HFSS returns a native solver/frequency snapshot where supported;
    if an explicit save changes a known setting, submission is refused. This
    check does not guarantee settings after batch mode's internal save.
    API success is submission evidence, not completion or convergence proof.

    ``blocking=true`` (v1.5, additive) makes this tool poll analysis_status
    after the submission until the solver reports idle, the timeout expires,
    or polling errors. It requires ``project_name`` and ``design_name``.
    Solver idle is still not convergence proof: run ``finalize_solve`` and
    verify the evidence before reporting results. With ``blocking=false``
    (default) the return value is exactly the pre-v1.5 payload.
    """
    if blocking and (not project_name or not design_name):
        raise ValueError("blocking=true requires project_name and design_name")
    result = await _worker_call(
        "analyze_design",
        pid=pid,
        port=port,
        arguments={
            "setup_name": setup_name,
            "project_name": project_name,
            "design_name": design_name,
            "num_cores": num_cores,
            "num_tasks": num_tasks,
            "num_gpus": num_gpus,
            "acf_file": acf_file,
            "use_auto_settings": use_auto_settings,
            "solve_in_batch": solve_in_batch,
            "machine": machine,
            "run_in_thread": run_in_thread,
            "revert_to_initial_mesh": revert_to_initial_mesh,
            "analyze_all_designs": analyze_all_designs,
            "icepak_safe_mode": icepak_safe_mode,
            "save_before_solve": save_before_solve,
            **({"run_token": run_token} if run_token else {}),
        },
        timeout=timeout if timeout is not None else 600.0,
    )
    if not blocking:
        return result
    started_at = time.monotonic()
    interval = max(1.0, float(blocking_poll_interval_s))
    polls = 0
    blocking_status = "error"
    last_status: dict[str, Any] = {}
    while True:
        polls += 1
        try:
            last_status = await _worker_call(
                "analysis_status",
                pid=pid,
                port=port,
                arguments={
                    "project_name": project_name,
                    "design_name": design_name,
                    "setup_name": setup_name or "",
                },
                timeout=timeout if timeout is not None else 600.0,
            )
            running = bool(last_status.get("running", False))
        except Exception as exc:  # noqa: BLE001
            blocking_status = "error"
            last_status = {"poll_error": repr(exc)[:300]}
            break
        if not running:
            blocking_status = "completed"
            break
        elapsed = time.monotonic() - started_at
        if blocking_timeout_s is not None and elapsed >= float(blocking_timeout_s):
            blocking_status = "timed_out"
            break
        await asyncio.sleep(interval)
    result.update({
        "blocking_requested": True,
        "blocking_status": blocking_status,
        "blocking_elapsed_s": round(time.monotonic() - started_at, 2),
        "blocking_timeout_s": blocking_timeout_s,
        "blocking_poll_interval_s": interval,
        "blocking_poll_count": polls,
        "blocking_last_status": last_status,
        "blocking_note": "blocking_status 'completed' means the solver reported idle, "
                        "not that convergence or result integrity were verified; "
                        "call finalize_solve and verify the evidence.",
    })
    return result


@mcp.tool()
async def export_results(
    output_path: str,
    export_type: str = "touchstone",
    setup_name: str | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Export results, including Icepak residual history and mesh statistics."""
    return await _worker_call(
        "export_results",
        pid=pid,
        port=port,
        arguments={
            "output_path": output_path,
            "export_type": export_type,
            "setup_name": setup_name,
        },
        timeout=timeout if timeout is not None else 600.0,
    )


@mcp.tool()
async def screenshot(
    path: str = "screenshot.jpg",
    project: str | None = None,
    design: str | None = None,
    plot_type: str = "model",
    resolution: Literal["1080p", "4k"] = "1080p",
    open_viewer: bool = True,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> list[TextContent | ImageContent]:
    """Capture an AEDT design view and return MCP text and image content."""
    result = await _worker_call(
        "screenshot",
        pid=pid,
        port=port,
        arguments={
            "path": path,
            "project": project,
            "design": design,
            "plot_type": plot_type,
            "resolution": resolution,
            "open_viewer": open_viewer,
        },
        timeout=timeout,
    )
    encoded = str(result.pop("data_base64"))
    mime_type = str(result.pop("mime_type", "image/jpeg"))
    return [
        TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2),
        ),
        ImageContent(type="image", data=encoded, mimeType=mime_type),
    ]


@mcp.tool()
async def export_config(
    output: str | None = None,
    project: str | None = None,
    design: str | None = None,
    overwrite: bool = False,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Export an active design configuration as JSON and return it inline."""
    return await _worker_call(
        "export_config",
        pid=pid,
        port=port,
        arguments={
            "output": output,
            "project": project,
            "design": design,
            "overwrite": overwrite,
        },
        timeout=timeout,
    )


@mcp.tool()
async def clear_aedt(
    close_projects: bool = True,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Clear AEDT messages and optionally close all open projects."""
    return await _worker_call(
        "clear_aedt",
        pid=pid,
        port=port,
        arguments={"close_projects": close_projects},
        timeout=timeout,
    )


@mcp.tool()
async def get_model_info(
    design_name: str | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return active model, design type, and project path metadata."""
    return await _worker_call(
        "get_model_info",
        pid=pid,
        port=port,
        arguments={"design_name": design_name},
        timeout=timeout,
    )


@mcp.tool()
def get_guidelines_for(content: GuidelinesContent) -> str:
    """Return solver and workflow guidance, including an Icepak checklist."""
    return get_guidelines(content)


@mcp.tool()
async def start_analysis(
    project_name: str,
    design_name: str,
    setup_name: str,
    pid: int | None = None,
    port: int | None = None,
    blocking: bool = False,
    run_token: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Start a named HFSS analysis; non-blocking is the default."""
    return await _worker_call(
        "start_analysis",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
            "setup_name": setup_name,
            "blocking": blocking,
            **({"run_token": run_token} if run_token else {}),
        },
        timeout=timeout,
    )


@mcp.tool()
async def get_analysis_status(
    project_name: str,
    design_name: str,
    setup_name: str = "",
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return running state and setup metadata; idle/old results do not prove a new solve completed."""
    return await _worker_call(
        "analysis_status",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
            "setup_name": setup_name,
        },
        timeout=timeout,
    )


@mcp.tool()
async def build_wr90_waveguide(
    pid: int | None = None,
    port: int | None = None,
    project_name: str = "Classic_WR90_Waveguide",
    design_name: str = "WR90_TE10",
    output_dir: str = "",
    solve: bool = True,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Build, validate, solve, and export a classic WR-90 TE10 HFSS case."""
    destination = output_dir.strip() or str(
        Path(__file__).resolve().parent / "test-artifacts" / "WR90_Waveguide"
    )
    return await _worker_call(
        "build_wr90_waveguide",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
            "solution_type": "DrivenModal",
            "output_dir": destination,
            "solve": solve,
        },
        timeout=timeout if timeout is not None else 1800.0,
    )


register_maxwell_tools(mcp, _worker_call)


# ---------------------------------------------------------------------------
# v1.5 guarded-workflow tools (additive; existing tools unchanged)
# ---------------------------------------------------------------------------

@mcp.tool()
async def preflight_design(
    project_name: str,
    design_name: str,
    setup_name: str | None = None,
    require_ff_sphere: bool = True,
    validate_attempts: int = 3,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Fail-closed pre-submission checks for one design.

    Repeats geometry validation ``validate_attempts`` times (AEDT validation
    can transiently report false right after edits), checks that an
    infinite-sphere/radiation setup exists when ``require_ff_sphere`` is true
    (far fields are all zero when it is inserted only after solving), and
    probes whether the named setup already has solution data. Any hard failure
    raises instead of returning a passing payload.
    """
    return await _worker_call(
        "preflight_design",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
            "setup_name": setup_name,
            "require_ff_sphere": require_ff_sphere,
            "validate_attempts": validate_attempts,
        },
        timeout=timeout,
    )


@mcp.tool()
async def finalize_solve(
    project_name: str,
    design_name: str,
    setups: list[str] | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Save the project and verify per-setup solution data presence.

    Saving is mandatory (``save_project`` returning False or raising makes the
    call fail with E_SAVE_MISSING). Then each setup is probed; setups without
    solution data are listed under ``incomplete_setups`` and
    ``solution_data_complete`` stays false, so a run cannot be reported as
    COMPLETED while data is missing.
    """
    return await _worker_call(
        "finalize_solve",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
            "setups": setups,
        },
        timeout=timeout if timeout is not None else 600.0,
    )


@mcp.tool()
async def export_farfield_report(
    project_name: str,
    design_name: str,
    setup_name: str,
    output_dir: str,
    cuts: list[str] | None = None,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Export far-field cuts bound to a setup that has radiated-field data.

    Supported cuts: theta_phi0, theta_phi90, phi_theta90. Each exported CSV is
    checked to be non-empty; an empty report fails with E_REPORT_EMPTY (a
    report bound to a setup without far-field data, e.g. a plain S11 adaptive
    setup, produces empty files). Non-empty rows do not by themselves prove
    the far field belongs to the latest solve.
    """
    return await _worker_call(
        "export_farfield_report",
        pid=pid,
        port=port,
        arguments={
            "project_name": project_name,
            "design_name": design_name,
            "setup_name": setup_name,
            "output_dir": output_dir,
            "cuts": cuts,
        },
        timeout=timeout if timeout is not None else 600.0,
    )


@mcp.tool()
async def wait_for_solve(
    project_name: str,
    design_name: str,
    setup_name: str = "",
    timeout_s: float | None = None,
    poll_interval_s: float = 5.0,
    pid: int | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Poll analysis status until the solver reports idle, timeout, or error.

    Returns one of ``idle`` / ``timeout`` / ``error``. ``idle`` means the
    desktop reported no running simulation; it is not convergence proof.
    """
    started_at = time.monotonic()
    interval = max(1.0, float(poll_interval_s))
    polls = 0
    while True:
        polls += 1
        try:
            last_status = await _worker_call(
                "analysis_status",
                pid=pid,
                port=port,
                arguments={
                    "project_name": project_name,
                    "design_name": design_name,
                    "setup_name": setup_name,
                },
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "elapsed_s": round(time.monotonic() - started_at, 2),
                "poll_count": polls,
                "error": repr(exc)[:300],
            }
        if not bool(last_status.get("running", False)):
            return {
                "status": "idle",
                "elapsed_s": round(time.monotonic() - started_at, 2),
                "poll_count": polls,
                "last_status": last_status,
                "note": "Solver idle is not convergence proof; run finalize_solve "
                        "and verify the evidence before reporting results.",
            }
        if timeout_s is not None and time.monotonic() - started_at >= float(timeout_s):
            return {
                "status": "timeout",
                "elapsed_s": round(time.monotonic() - started_at, 2),
                "poll_count": polls,
                "last_status": last_status,
            }
        await asyncio.sleep(interval)


@mcp.tool()
def verify_artifacts(
    paths: list[str],
    min_rows: int = 1,
    freq_lo_ghz: float | None = None,
    freq_hi_ghz: float | None = None,
    freq_points_min: int | None = None,
) -> dict[str, Any]:
    """Check exported result files locally (no AEDT involved).

    Each file is checked for existence, readable non-empty rows, and — when
    the first column parses as frequencies — coverage of the requested
    ``freq_lo_ghz``/``freq_hi_ghz`` range and a minimum number of frequency
    points. Error codes: E_EXPORT_MISSING / E_EXPORT_UNREADABLE /
    E_EXPORT_EMPTY / E_FREQ_RANGE_LOW / E_FREQ_RANGE_HIGH / E_FREQ_POINTS.
    """
    results: dict[str, Any] = {}
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        entry: dict[str, Any] = {
            "path": str(path),
            "exists": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else None,
            "ok": False,
            "errors": [],
        }
        if not entry["exists"]:
            entry["errors"].append("E_EXPORT_MISSING")
            results[str(path)] = entry
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            entry["errors"].append("E_EXPORT_UNREADABLE")
            results[str(path)] = entry
            continue
        lines = [
            ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith(("!", "#"))
        ]
        entry["rows"] = len(lines)
        if len(lines) < int(min_rows):
            entry["errors"].append("E_EXPORT_EMPTY")
        freqs = []
        for ln in lines:
            parts = re.split(r"[\s,]+", ln.strip())
            try:
                freqs.append(float(parts[0]))
            except (ValueError, IndexError):
                pass
        if freqs:
            entry["first_freq"] = min(freqs)
            entry["last_freq"] = max(freqs)
            if freq_lo_ghz is not None and entry["first_freq"] > float(freq_lo_ghz):
                entry["errors"].append("E_FREQ_RANGE_LOW")
            if freq_hi_ghz is not None and entry["last_freq"] < float(freq_hi_ghz):
                entry["errors"].append("E_FREQ_RANGE_HIGH")
            if freq_points_min is not None and len(freqs) < int(freq_points_min):
                entry["errors"].append("E_FREQ_POINTS")
        entry["ok"] = not entry["errors"]
        results[str(path)] = entry
    return {"files": results, "all_ok": all(v["ok"] for v in results.values())}


@mcp.tool()
def clear_stale_lock(project_path: str, force: bool = False) -> dict[str, Any]:
    """Remove a stale ``*.aedt.lock`` only when its owner is provably gone.

    Fails closed: no recognizable owner PID, or an owner process that is still
    alive, means the lock is left untouched unless the caller explicitly
    understands the risk. ``force`` never deletes a lock whose owner is alive.
    """
    path = Path(project_path).expanduser().resolve()
    if path.suffix.lower() != ".aedt":
        path = path.with_suffix(".aedt")
    lock = Path(str(path) + ".lock")
    if not lock.exists():
        return {"cleared": False, "reason": "no_lock", "lock_path": str(lock)}
    owner_pid: int | None = None
    unstructured = False
    try:
        text = lock.read_text(encoding="utf-8", errors="replace")
        # AEDT lock files are "$begin 'ConnectionParameters' ... DesktopProcessID=NNN".
        # Parse the structured field first; a loose integer scan may pick up an
        # unrelated number (version/build/port) and misjudge the owner.
        structured = re.search(r"DesktopProcessID\s*=\s*(\d+)", text)
        if structured is None:
            structured = re.search(r"\b(?:ProcID|ProcessID|PID|pid)\s*[=:]\s*(\d+)", text)
        if structured is not None:
            owner_pid = int(structured.group(1))
        else:
            loose = re.search(r"\b\d{3,10}\b", text)
            if loose is not None:
                owner_pid = int(loose.group())
                unstructured = True
    except Exception:
        owner_pid = None
    alive: bool | None = None
    if owner_pid is not None:
        try:
            import psutil
            alive = bool(psutil.pid_exists(owner_pid))
        except ImportError:
            alive = None
        if alive:
            return {
                "cleared": False,
                "reason": "owner_alive",
                "owner_pid": owner_pid,
                "lock_path": str(lock),
                "pid_source": "unstructured_guess" if unstructured else "lock_metadata",
                "note": "refusing to delete a lock whose owner process is alive",
            }
        if alive is None:
            return {
                "cleared": False,
                "reason": "owner_unknown_state",
                "owner_pid": owner_pid,
                "lock_path": str(lock),
                "note": "could not verify the owner process state; use force to override",
            }
    elif not force:
        return {
            "cleared": False,
            "reason": "unknown_owner",
            "lock_path": str(lock),
            "note": "lock content has no recognizable PID; use force=True to delete",
        }
    try:
        lock.unlink()
        return {"cleared": True, "removed_path": str(lock), "owner_pid": owner_pid}
    except OSError as exc:
        return {"cleared": False, "reason": "delete_failed", "error": repr(exc)[:200]}


@mcp.tool()
def acquire_campaign_lock(campaign_dir: str, holder: str, timeout_s: float = 0.0) -> dict[str, Any]:
    """Create ``campaign.lock`` for exclusive campaign ownership (opt-in).

    Returns immediately with ``acquired`` false when the lock is held by
    someone else and no timeout is given; with a positive ``timeout_s`` it
    retries until the deadline. Lock files are only advisory here — the
    worker-side enforcement is a separate integration step.
    """
    directory = Path(campaign_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "campaign.lock"
    started = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing: dict[str, Any] = {}
            try:
                text = lock.read_text(encoding="utf-8", errors="replace")
                existing = json.loads(text) if text.strip().startswith("{") else {"raw": text[:200]}
            except Exception:
                existing = {"raw": "unreadable"}
            if timeout_s > 0 and time.monotonic() - started < float(timeout_s):
                time.sleep(min(1.0, float(timeout_s)))
                continue
            return {
                "acquired": False,
                "reason": "held_by_other",
                "existing": existing,
                "lock_path": str(lock),
            }
        else:
            payload = {
                "holder": str(holder),
                "pid": os.getpid(),
                "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.close(fd)
            return {
                "acquired": True,
                "lock_path": str(lock),
                "holder": str(holder),
                "pid": os.getpid(),
            }


@mcp.tool()
def release_campaign_lock(campaign_dir: str, holder: str) -> dict[str, Any]:
    """Release a campaign lock acquired by the same holder."""
    lock = Path(campaign_dir).expanduser().resolve() / "campaign.lock"
    if not lock.exists():
        return {"released": False, "reason": "no_lock"}
    try:
        payload = json.loads(lock.read_text(encoding="utf-8", errors="replace"))
        current = payload.get("holder")
    except Exception:
        current = None
    if current is not None and str(current) != str(holder):
        return {"released": False, "reason": "holder_mismatch", "existing_holder": current}
    try:
        lock.unlink()
        return {"released": True}
    except OSError as exc:
        return {"released": False, "reason": "delete_failed", "error": repr(exc)[:200]}


# --------------------------------------------------------------------------
# Guarded-workflow layer (v1.6): design spec, evaluation, guarded runs
# --------------------------------------------------------------------------

CAMPAIGN_DEFAULT_ROOT = os.environ.get(
    "AEDT_CAMPAIGN_ROOT", r"D:\AgentAEDT_Collab\campaigns"
)


def _campaign_paths(campaign_dir: str) -> dict[str, Path]:
    root = Path(campaign_dir or CAMPAIGN_DEFAULT_ROOT).expanduser().resolve()
    return {
        "root": root,
        "spec": root / "design_spec.json",
        "db": root / "campaign.sqlite",
        "runs": root / "runs",
    }


def _load_campaign(campaign_dir: str) -> tuple[dict[str, Any], dict[str, Path]]:
    paths = _campaign_paths(campaign_dir)
    if not paths["spec"].is_file():
        raise SpecError(
            f"no design_spec.json under {paths['root']}; call spec_create first"
        )
    return load_spec(paths["spec"]), paths


@mcp.tool()
def spec_create(
    campaign_dir: str,
    project: str,
    design_type: str,
    frequency_ghz: list[float],
    metrics: list[dict[str, Any]],
    evaluation: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the versioned design specification for one campaign (v1.6)."""
    spec = new_spec(
        project,
        design_type,
        frequency_ghz,
        metrics,
        evaluation=evaluation,
        execution_policy=execution_policy,
    )
    paths = _campaign_paths(campaign_dir)
    paths["root"].mkdir(parents=True, exist_ok=True)
    save_spec(spec, paths["spec"])
    with Store(paths["db"]) as store:
        store.add_spec_version(spec, reason="initial spec", approver="spec_create")
    return {"spec_path": str(paths["spec"]), "spec": spec}


@mcp.tool()
def spec_get(campaign_dir: str) -> dict[str, Any]:
    """Return the current design specification and its version history."""
    spec, paths = _load_campaign(campaign_dir)
    with Store(paths["db"]) as store:
        versions = [
            dict(row)
            for row in store.connection.execute(
                "SELECT version, reason, approver, created_utc FROM spec_versions "
                "WHERE project=? ORDER BY version",
                (spec["project"],),
            )
        ]
    return {"spec_path": str(paths["spec"]), "spec": spec, "versions": versions}


@mcp.tool()
def spec_update_with_approval(
    campaign_dir: str,
    changes: dict[str, Any],
    reason: str,
    approver: str,
) -> dict[str, Any]:
    """Apply a spec change only with an explicit reason and approver (v1.6)."""
    spec, paths = _load_campaign(campaign_dir)
    updated = update_spec(spec, changes, reason=reason, approver=approver)
    save_spec(updated, paths["spec"])
    with Store(paths["db"]) as store:
        store.add_spec_version(updated, reason=reason, approver=approver)
    return {
        "spec_path": str(paths["spec"]),
        "version": updated["version"],
        "history_entry": updated["history"][-1],
        "spec": updated,
    }


@mcp.tool()
def campaign_context(campaign_dir: str, limit: int = 5) -> dict[str, Any]:
    """Return the machine-generated status summary for one campaign (v1.6)."""
    spec, paths = _load_campaign(campaign_dir)
    with Store(paths["db"]) as store:
        runs = store.list_runs(spec["project"], limit=int(limit))
        counts = store.counts()
    latest = None
    if runs:
        latest = {
            "run_id": runs[0]["run_id"],
            "status": runs[0]["status"],
            "created_utc": runs[0]["created_utc"],
            "params": json.loads(runs[0]["params_json"]),
        }
        with Store(paths["db"]) as store:
            detail = store.get_run(runs[0]["run_id"]) or {}
        metrics = {
            name: row.get("value") for name, row in (detail.get("metrics") or {}).items()
        }
        latest["metrics"] = metrics
        # Unmet hard constraints come from the stored pass flags, not from the
        # stage log, so a COMPLETED-but-infeasible run is reported correctly.
        unmet = [
            metric["id"]
            for metric in hard_metrics(spec)
            if (detail.get("metrics") or {}).get(metric["id"], {}).get("passed") == 0
        ]
        latest["unmet_constraints"] = unmet
    context = task_context(spec, latest)
    context["campaign_dir"] = str(paths["root"])
    context["counts"] = counts
    context["analysis_audit"] = audit_summary(paths["root"])
    context["recent_runs"] = [
        {"run_id": r["run_id"], "status": r["status"], "created_utc": r["created_utc"]}
        for r in runs
    ]
    return context


@mcp.tool()
def evaluate_exported_results(
    campaign_dir: str,
    s11: str,
    theta_cuts: dict[str, str] | None = None,
    phi_cut: str | None = None,
    frequency_ghz: float | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate already-exported artifacts against the campaign spec (v1.6)."""
    spec, paths = _load_campaign(campaign_dir)
    artifacts: dict[str, Any] = {"s11": s11, "theta_cuts": theta_cuts or {}}
    if phi_cut:
        artifacts["phi_cut"] = phi_cut
    if frequency_ghz is not None:
        artifacts["frequency_ghz"] = float(frequency_ghz)
    result = guard_evaluate(artifacts, spec)
    named = named_metrics(result, spec)
    result["named_metrics"] = named
    if run_id:
        with Store(paths["db"]) as store:
            store.add_metrics(run_id, named, hard_results(result, spec))
        result["stored_run_id"] = run_id
    return result


class _GuardedAdapter:
    """Bridge the runner to the PyAEDT worker over the existing broker."""

    def __init__(
        self,
        *,
        pid: int | None,
        port: int | None,
        project_name: str,
        design_name: str,
        setup_name: str,
        cuts: list[str],
        worker_timeout: float,
        sweep_name: str | None = None,
        run_token: str | None = None,
        solver_settings: dict[str, Any] | None = None,
    ):
        self.pid = pid
        self.port = port
        self.project_name = project_name
        self.design_name = design_name
        self.setup_name = setup_name
        self.cuts = cuts
        self.worker_timeout = worker_timeout
        self.sweep_name = sweep_name
        self.run_token = run_token
        self.solver_settings = solver_settings or {}

    def _call(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = _target(self.pid, self.port)
        result = worker_client.execute(
            target, command, arguments, timeout=self.worker_timeout
        )
        if not isinstance(result, dict):
            raise TypeError(f"AEDT worker command {command} returned a non-object result")
        return result

    def preflight(self) -> dict[str, Any]:
        return self._call(
            "preflight_design",
            {
                "project_name": self.project_name,
                "design_name": self.design_name,
                "setup_name": self.setup_name,
            },
        )

    def submit(self) -> dict[str, Any]:
        return self._call(
            "analyze_design",
            {
                "project_name": self.project_name,
                "design_name": self.design_name,
                "setup_name": self.setup_name,
                "save_before_solve": False,
                **({"run_token": self.run_token} if self.run_token else {}),
                **{
                    key: value
                    for key, value in (self.solver_settings.get("effective") or {}).items()
                    if value not in (None, 0)
                },
            },
        )

    def wait(self, timeout_s: float, poll_interval_s: float) -> dict[str, Any]:
        started = time.monotonic()
        polls = 0
        saw_running = False
        settle_s = min(float(timeout_s), max(2.0, 4.0 * float(poll_interval_s)))
        while True:
            status = self._call(
                "analysis_status",
                {
                    "project_name": self.project_name,
                    "design_name": self.design_name,
                    "setup_name": self.setup_name,
                },
            )
            polls += 1
            running = bool(status.get("running"))
            if running:
                saw_running = True
            if not running:
                # AEDT can report idle for a few seconds before the solver shows
                # up; do not treat that as completion until either the solver was
                # observed running, or the settle window has elapsed.
                if saw_running or (time.monotonic() - started) >= settle_s:
                    return {"running": False, "poll_count": polls, "last": status,
                            "saw_running": saw_running}
            if time.monotonic() - started >= timeout_s:
                return {"running": True, "timed_out": True, "poll_count": polls,
                        "saw_running": saw_running}
            time.sleep(poll_interval_s)

    def finalize(self) -> dict[str, Any]:
        return self._call(
            "finalize_solve",
            {"project_name": self.project_name, "design_name": self.design_name},
        )

    def export_touchstone(self, path: Path) -> dict[str, Any]:
        return self._call(
            "export_results",
            {
                "output_path": str(path),
                "export_type": "touchstone",
                "setup_name": self.setup_name,
                **({"sweep_name": self.sweep_name} if self.sweep_name else {}),
            },
        )

    def export_farfield(self, output_dir: Path, cuts: list[str],
                        frequencies: list[float] | None = None) -> dict[str, Any]:
        arguments = {
            "project_name": self.project_name,
            "design_name": self.design_name,
            "setup_name": self.setup_name,
            "output_dir": str(output_dir),
            "cuts": cuts,
        }
        if self.sweep_name:
            arguments["sweep_name"] = self.sweep_name
        if frequencies:
            arguments["frequencies_ghz"] = list(frequencies)
        return self._call("export_farfield_report", arguments)

    def abort(self) -> dict[str, Any]:
        return self._call("abort_analysis", {"project_name": self.project_name,
                                             "design_name": self.design_name})

    def export_convergence(self, path: Path) -> dict[str, Any]:
        return self._call(
            "export_results",
            {
                "output_path": str(path),
                "export_type": "convergence",
                "setup_name": self.setup_name,
            },
        )


@mcp.tool()
def submit_guarded_run(
    campaign_dir: str,
    project_name: str,
    design_name: str,
    setup_name: str = "Setup1",
    params: dict[str, Any] | None = None,
    cuts: list[str] | None = None,
    timeout_s: float = 3600.0,
    poll_interval_s: float = 5.0,
    worker_timeout: float = 1800.0,
    pid: int | None = None,
    port: int | None = None,
    frequency_ghz: float | None = None,
    sweep_name: str | None = None,
    frequencies_ghz: list[float] | None = None,
    num_cores: int | None = None,
    num_tasks: int | None = None,
    num_gpus: int | None = None,
) -> dict[str, Any]:
    """Submit one guarded run and return immediately with its run_id (v1.6).

    The background runner performs preflight, submission, polling, save +
    solution-data verification, artifact export and evaluation, and only then
    marks the run COMPLETED. Query progress with ``get_run_status``.
    """
    spec, paths = _load_campaign(campaign_dir)
    resolved_frequencies = resolve_frequencies(spec, frequencies_ghz)
    solver = resolve_solver_settings(
        spec,
        num_cores=num_cores,
        num_tasks=num_tasks,
        num_gpus=num_gpus,
        cpu_count=os.cpu_count(),
    )
    cache_key = _cache_key(
        project=project_name,
        design=design_name,
        setup=setup_name,
        spec_version=spec["version"],
        params=params or {},
        cuts=cuts or ["theta_phi0", "phi_theta90"],
        frequency_ghz=frequency_ghz,
        frequencies_ghz=resolved_frequencies,
        sweep_name=sweep_name,
    )
    with Store(paths["db"]) as store:
        cached = store.find_completed_by_cache_key(spec["project"], cache_key)
    if cached is not None:
        return {
            "run_id": cached["run_id"],
            "status": "COMPLETED",
            "cached": True,
            "spec_version": spec["version"],
            "metrics": {
                name: row.get("value") for name, row in (cached.get("metrics") or {}).items()
            },
            "artifact_dir": cached.get("artifact_dir"),
            "note": "identical parameters and spec version already completed; no solve started",
        }
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    runs_root = paths["runs"] / run_id
    artifact_dir = runs_root / "artifacts"
    runs_root.mkdir(parents=True, exist_ok=True)
    (runs_root / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": project_name,
                "design": design_name,
                "setup": setup_name,
                "spec_version": spec["version"],
                "params": params or {},
                "cuts": cuts or ["theta_phi0", "phi_theta90"],
                "frequency_ghz": frequency_ghz,
                "frequencies_ghz": resolved_frequencies,
                "solver_settings": solver,
                "created_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with Store(paths["db"]) as store:
        store.add_run(
            run_id,
            spec["project"],
            spec["version"],
            {
                **(params or {}),
                "project_name": project_name,
                "design_name": design_name,
                "setup_name": setup_name,
            },
            status="CREATED",
            artifact_dir=str(artifact_dir),
            cache_key=cache_key,
        )
    run_token = issue_token(
        paths["root"],
        run_id=run_id,
        project=project_name,
        design=design_name,
        setup=setup_name,
        ttl_s=max(float(timeout_s), 600.0) + 600.0,
    )
    adapter = _GuardedAdapter(
        pid=pid,
        port=port,
        project_name=project_name,
        design_name=design_name,
        setup_name=setup_name,
        cuts=list(cuts or ["theta_phi0", "phi_theta90"]),
        worker_timeout=worker_timeout,
        sweep_name=sweep_name,
        run_token=str(run_token),
        solver_settings=solver,
    )

    def _run_in_background() -> None:
        store = Store(paths["db"])

        def _on_completed(payload: dict[str, Any]) -> None:
            commit_best(
                paths["root"],
                {
                    "run_id": payload.get("run_id") or run_id,
                    "spec_version": spec["version"],
                    "metrics": payload.get("metrics") or {},
                    "params": payload.get("params") or {},
                    "feasible": bool(payload.get("feasible")),
                },
                spec,
            )

        try:
            runner = CampaignRunner(
                store=store,
                adapter=adapter,
                spec=spec,
                run_id=run_id,
                artifact_dir=artifact_dir,
                params=params or {},
                cuts=list(cuts or ["theta_phi0", "phi_theta90"]),
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                frequency_ghz=frequency_ghz,
                frequencies=resolved_frequencies,
                on_completed=_on_completed,
            )
            runner.run()
        except Exception as exc:  # noqa: BLE001 - a dead thread must still be visible
            try:
                store.set_status(
                    run_id,
                    "FAILED",
                    notes=json.dumps([{"stage": "FAILED", "error": str(exc)[:600]}]),
                )
            except Exception:
                pass
        finally:
            store.close()
            revoke_token(run_token)

    thread = threading.Thread(target=_run_in_background, name=f"guarded-{run_id}", daemon=True)
    thread.start()
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "artifact_dir": str(artifact_dir),
        "spec_version": spec["version"],
        "frequencies_ghz": resolved_frequencies,
        "solver_settings": solver,
        "note": "background runner started; poll get_run_status until a terminal state",
    }


@mcp.tool()
def get_run_status(campaign_dir: str, run_id: str | None = None) -> dict[str, Any]:
    """Return the persisted status, stage log and metrics of one run (v1.6)."""
    spec, paths = _load_campaign(campaign_dir)
    with Store(paths["db"]) as store:
        if run_id is None:
            runs = store.list_runs(spec["project"], limit=1)
            if not runs:
                return {"run_id": None, "status": None, "note": "no runs recorded"}
            run_id = runs[0]["run_id"]
        detail = store.get_run(run_id)
        if detail is None:
            return {"run_id": run_id, "status": None, "note": "unknown run_id"}
        stage_log: list[dict[str, Any]] = []
        if detail.get("notes"):
            try:
                stage_log = json.loads(detail["notes"])
            except json.JSONDecodeError:
                stage_log = [{"stage": detail.get("status"), "note": detail["notes"]}]
    solver_settings = None
    run_json = paths["runs"] / run_id / "run.json"
    if run_json.is_file():
        try:
            solver_settings = json.loads(run_json.read_text(encoding="utf-8")).get("solver_settings")
        except (json.JSONDecodeError, OSError):
            solver_settings = None
    return {
        "run_id": run_id,
        "status": detail["status"],
        "params": detail["params"],
        "artifact_dir": detail["artifact_dir"],
        "solver_settings": solver_settings,
        "created_utc": detail["created_utc"],
        "updated_utc": detail["updated_utc"],
        "metrics": {
            name: {"value": row.get("value"), "unit": row.get("unit"), "passed": row.get("passed")}
            for name, row in (detail.get("metrics") or {}).items()
        },
        "stage_log": stage_log,
        "terminal": detail["status"] in {"COMPLETED", "FAILED", "RECOVERY_REQUIRED"},
    }


def _cache_key(**fields: Any) -> str:
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@mcp.tool()
def archive_query(campaign_dir: str, limit: int = 20) -> dict[str, Any]:
    """Return the archived best candidate, Pareto front and ranked runs (v1.10)."""
    spec, paths = _load_campaign(campaign_dir)
    with Store(paths["db"]) as store:
        runs = store.list_runs(spec["project"], limit=int(limit))
        details = []
        for run in runs:
            detail = store.get_run(run["run_id"])
            if detail is None:
                continue
            metrics = {
                name: {"value": row.get("value"), "passed": row.get("passed")}
                for name, row in (detail.get("metrics") or {}).items()
            }
            details.append({
                "run_id": detail["run_id"],
                "status": detail["status"],
                "metrics": metrics,
                "feasible": is_feasible(metrics, spec),
                "params": detail["params"],
            })
    objectives = default_objectives(spec)
    ranked = rank(details, objectives)
    return {
        "campaign_dir": str(paths["root"]),
        "objectives": objectives,
        "best": read_best(paths["root"]),
        "pareto_front": pareto_front(details, objectives),
        "ranked": ranked,
    }


@mcp.tool()
def resume_guarded_run(
    campaign_dir: str,
    run_id: str,
    pid: int | None = None,
    port: int | None = None,
    cuts: list[str] | None = None,
    sweep_name: str | None = None,
    frequency_ghz: float | None = None,
    worker_timeout: float = 1800.0,
) -> dict[str, Any]:
    """Resume a crashed run from the save/export stage without re-solving (v1.9).

    Use this when the process that owned ``submit_guarded_run`` died after the
    solve (run stuck in SOLVING/SOLVED/EXPORTING). The solver is never started
    again here: the run is finished from the saved solution data, and it fails
    closed when that evidence is missing.
    """
    spec, paths = _load_campaign(campaign_dir)
    with Store(paths["db"]) as store:
        detail = store.get_run(run_id)
    if detail is None:
        raise ValueError(f"unknown run_id: {run_id}")
    if detail["status"] in {"COMPLETED", "FAILED"}:
        return {
            "run_id": run_id,
            "status": detail["status"],
            "resumed": False,
            "note": "run already reached a terminal state",
        }
    try:
        stage_log = json.loads(detail.get("notes") or "[]")
    except json.JSONDecodeError:
        stage_log = []
    stage = last_stage(stage_log) or "CREATED"
    artifact_dir = Path(detail["artifact_dir"] or (paths["runs"] / run_id / "artifacts"))
    adapter = _GuardedAdapter(
        pid=pid,
        port=port,
        project_name=str(detail["params"].get("project_name") or ""),
        design_name=str(detail["params"].get("design_name") or ""),
        setup_name=str(detail["params"].get("setup_name") or ""),
        cuts=list(cuts or ["theta_phi0", "phi_theta90"]),
        worker_timeout=worker_timeout,
        sweep_name=sweep_name,
    )
    if not adapter.project_name or not adapter.design_name:
        raise ValueError(
            "run metadata lacks project/design; cannot resume without them"
        )

    def _resume_in_background() -> None:
        store = Store(paths["db"])

        def _on_completed(payload: dict[str, Any]) -> None:
            commit_best(
                paths["root"],
                {
                    "run_id": payload.get("run_id") or run_id,
                    "spec_version": spec["version"],
                    "metrics": payload.get("metrics") or {},
                    "params": payload.get("params") or {},
                    "feasible": bool(payload.get("feasible")),
                },
                spec,
            )

        try:
            runner = CampaignRunner(
                store=store,
                adapter=adapter,
                spec=spec,
                run_id=run_id,
                artifact_dir=artifact_dir,
                cuts=list(cuts or ["theta_phi0", "phi_theta90"]),
                frequency_ghz=frequency_ghz,
                on_completed=_on_completed,
            )
            runner.resume(stage)
        except Exception as exc:  # noqa: BLE001
            try:
                store.set_status(
                    run_id,
                    "FAILED",
                    notes=json.dumps([{"stage": "FAILED", "error": f"resume: {exc}"[:600]}]),
                )
            except Exception:
                pass
        finally:
            store.close()

    thread = threading.Thread(target=_resume_in_background, name=f"resume-{run_id}", daemon=True)
    thread.start()
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "resumed_from": stage,
        "artifact_dir": str(artifact_dir),
        "note": "resuming from the save/export stage; no solve is started",
    }


@mcp.resource("aedt://status")
def aedt_status() -> str:
    """Return discovery-only status without attaching to any AEDT process."""
    return json.dumps(
        {
            "connected": False,
            "selection_required": True,
            "sessions": session_discovery.list_sessions(),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("aedt://agent-instructions")
def agent_instructions() -> str:
    return INSTRUCTIONS


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
















