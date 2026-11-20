# Ansys AEDT MCP

<p>
  <a href="https://github.com/Hawse1337/ansys-aedt-mcp/actions/workflows/ci.yml"><img alt="tests" src="https://github.com/Hawse1337/ansys-aedt-mcp/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/Hawse1337/ansys-aedt-mcp/releases/latest"><img alt="release" src="https://img.shields.io/github/v/release/Hawse1337/ansys-aedt-mcp?color=e4572e"></a>
  <a href="LICENSE"><img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-2a9d8f"></a>
  <img alt="python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-23262b">
  <img alt="AES 2026 R1" src="https://img.shields.io/badge/AEDT-2026%20R1-2a9d8f">
</p>

This module lets MCP clients such as Codex and DeepSeek Harness control Ansys Electronics Desktop through PyAEDT. AEDT 2026 R1 is the default and live-tested target.

**Guard rails:** managed-only AEDT attach, unmanaged-session detection, best-effort command audit, arbitrary-code off by default, and a fail-closed policy shipped for both clients. Start with [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) and [docs/CLIENTS.md](docs/CLIENTS.md); DSH/Codex ready-to-use configs and the patched AEDT skill live under [examples/clients/](examples/clients/).

## What it is

`ansys-aedt-mcp` is a guarded MCP server for Ansys Electronics Desktop. It sits between an MCP
client (DeepSeek Harness, Codex, or your own agent) and AEDT, and adds a lifecycle/evidence layer on
top of the official [PyAEDT-MCP](https://github.com/ansys/pyaedt-mcp):

- managed AEDT sessions (the guard records what it launched and refuses unmanaged attach);
- unmanaged-session detection (`guard_status`);
- arbitrary broker code off by default;
- run-token-scoped solves and guarded-run audit;
- PyAEDT app identity checks and launch/release auditing;
- fail-closed client configurations and a threat model.

It is a **guard**, not an OS sandbox: same-user shell access can still start AEDT directly. Use OS
isolation (separate account/VM, AppLocker/WDAC) when you need a hard boundary.

## Quick start

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

Then connect an MCP client to `.\mcp_server.py` (stdio). See
[docs/CLIENTS.md](docs/CLIENTS.md) for DeepSeek Harness and Codex configurations, and
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the security boundary.

```text
1. check_aedt_installed                     # AEDT / PyAEDT availability
2. guard_status                              # managed vs unmanaged sessions, audit health
3. launch_aedt(non_graphical=True)          # returns managed:true with pid/port
4. connect_to_aedt(port=...) / guarded tools # only this session is served
```

## Security controls at a glance

| control | behaviour |
|---|---|
| managed-only attach | guard-launched AEDT is recorded (PID, create time, port, verified); unmanaged sessions are refused |
| unmanaged detection | `guard_status` lists every live `ansysedt.exe` as managed/unmanaged and reports `scan.enforcement` |
| arbitrary code | `run_python_code` / `run_python_script` refused at MCP and worker layers unless a human operator enables them |
| tokens | `AEDT_GUARD_ENFORCE=1` binds run tokens to project/design/setup; solve entries are covered |
| audit | launch/release and guarded-run commands land in `guard_audit.jsonl` with enforcement-mode fields |
| identity | PyAEDT app objects are checked against the verified desktop |

## Clients

Fail-closed, ready-to-use configurations live in [`examples/clients/`](examples/clients/):

- **DeepSeek Harness** — installable MCP bundle (`mcp__aedt__*`), global `AGENTS.md` rule and a
  patched AEDT skill;
- **Codex** — `config.toml` with `required = true`, `default_tools_approval_mode = "prompt"`,
  `disabled_tools = ["run_python_code", "run_python_script"]`, plus `AGENTS.md`.

Both clients stop when the guard tools are missing; they never fall back to direct PyAEDT or
`ansysedt.exe`.

## Verification

- Offline suite: **481 passed, 1 skipped, 118 subtests**.
- Live on AEDT 2026 R1 / PyAEDT 1.10.0: unmanaged attach refused, guard-launched session
  `managed: true`, `run_python_code` refused.
- Releases and the hardened package are published at:
  https://github.com/Hawse1337/pyaedt-mcp-hardening-kit/releases

## Enforcement boundary (v0.3.2)

Guard-launched AEDT sessions are recorded and only those sessions can be attached through
the guard. `guard_status` lists every live `ansysedt.exe` as managed or unmanaged, and every
MCP worker call is appended to a best-effort audit log. This turns a silent bypass into a visible,
refused action:

- managed-only attach (`AEDT_MCP_ALLOW_UNMANAGED=1` is a human-only escape hatch);
- unmanaged AEDT detection in `guard_status` / `check_aedt_status`;
- best-effort per-command audit at `<state>\guard_audit.jsonl` (MCP worker-call path; launch
  readiness is recorded in the managed-session registry, not this log);
- `run_python_code` / `run_python_script` refused by default (operator opt-in
  `AEDT_MCP_ALLOW_ARBITRARY_CODE=1`), because arbitrary broker code can forge managed records.

The guard is still **not an OS sandbox**: a same-user shell can launch AEDT directly. Read
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) before relying on it for access control, and use
[docs/CLIENTS.md](docs/CLIENTS.md) to wire DeepSeek Harness or Codex with the fail-closed rule.

## Architecture

```text
Codex -> FastMCP stdio server -> external PyAEDT broker -> explicit AEDT PID or gRPC port
```

No MCP script, socket server, extension, or background thread runs inside AEDT. The MCP server creates one external broker for each selected target and reuses that PyAEDT connection across commands. The broker calls `release_desktop(close_projects=False, close_on_exit=False)` only when `release_connection` is called, the MCP process exits, or its stdin closes.

This lifecycle is required for AEDT 2026 R1 gRPC sessions: ending the PyAEDT client after every command also ends that session's gRPC listener. Keeping the connection in an external broker avoids rebuilding AEDT for each tool call without leaving Toolkit/Automation code running in AEDT.

On Windows, the broker watches only its target AEDT process. If the AEDT busy dialog appears, or the main window changes from visible to closed, the broker interprets that as an explicit user close request and calls AEDT `QuitApplication()` through the existing PyAEDT session. The broker then exits. This prevents a connected broker from leaving a hidden AEDT process behind.

## Install

Use Python 3.10 or newer:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

The package pins PyAEDT 1.10.0, the current stable release ([documentation](https://aedt.docs.pyansys.com/version/stable/index.html)). Set `AEDT_INSTALL_DIR` to the directory containing `ansysedt.exe` when using `launch_aedt`.

## MCP Configuration

Use `examples/mcp_config.example.json` and replace `<repo>` with this directory's absolute path.

## Explicit Targeting

There is no implicit AEDT session.

1. Call `check_aedt_installed` and `check_aedt_status`.
2. Call `list_aedt_sessions`, `connect_to_aedt`, or `launch_aedt`.
3. Choose one PID or one gRPC port.
4. Pass exactly one of `pid` or `port` to every targeted tool.

The server never chooses the newest or foreground AEDT window. A successful probe records the returned PID and port as aliases for the same broker, so either explicit identifier continues to address that same session.

## Lifecycle

- `connect_to_aedt` or `check_aedt_connection` creates the broker on first use and performs a real PyAEDT probe.
- Project and analysis tools reuse that broker.
- `disconnect_from_aedt` explicitly chooses whether AEDT stays open; `release_connection` always keeps it open.
- MCP shutdown and broker stdin EOF also release all connections.
- Closing the AEDT window triggers `QuitApplication()` and terminates that target's broker.
- A timed-out broker is terminated; the AEDT process is never force-terminated.

For an MCP-launched session, use the PID/port returned by `launch_aedt`. A user-opened AEDT window is **unmanaged** and refused by default; attaching to it needs an explicit human operator decision (see `docs/THREAT_MODEL.md`).

## Tools

Official PyAEDT MCP-compatible names:

- Lifecycle and diagnostics: `check_aedt_installed`, `check_aedt_status`, `launch_aedt`, `connect_to_aedt`, `disconnect_from_aedt`, `clear_aedt`, and `get_pyaedt_logs`.
- Projects and designs: `list_projects`, `list_designs`, `open_project`, `save_project`, and `create_design`.
- Automation: `run_python_code` and `run_python_script` (refused by default; an operator can
  enable them for a supervised session — autonomous agents must use the dedicated tools).
- Simulation and evidence: `validate_design`, `analyze_design`, `export_results`, `export_config`, `get_model_info`, `screenshot`, and `get_guidelines_for`.

`create_design` supports `Hfss`, `Maxwell2d`, `Maxwell3d`, `Q3d`, `Q2d`, `Icepak`, `Circuit`, `TwinBuilder`, `Mechanical`, `Emit`, `RMXprt`, and `Hfss3dLayout`.

Local extensions retained from this MCP:

- `list_aedt_sessions`, `check_aedt_connection`, and `release_connection` expose explicit-target broker control.
- `get_project_info` and `close_projects` provide structured project inspection and scoped cleanup.
- `create_hfss_design`, `start_analysis`, and `get_analysis_status` preserve the original HFSS workflow.
- `build_wr90_waveguide` builds, validates, solves, and exports the dedicated WR-90 TE10 case.

## Dedicated Maxwell tools

The server adds 15 `maxwell_*` tools; the full surface is 63 tools. They cover coil terminals, windings and links, current excitations, rotational/translational motion bands, force/torque, eddy effects, setup creation, local mesh operations, design readback, activity, numerical results and field plots. Each requires explicit `project_name`, `design_name` and one `pid`/`port`.

See [Maxwell tools, parameters, examples and verification limits](MAXWELL_TOOLS.md) and the [offline/live verification record](MAXWELL_VALIDATION.md). Restart the MCP server and refresh the client's tool list after updating; the existing AEDT window can remain open. Motion tools assign an existing band and do not construct or validate enclosing geometry. Motor transient behavior, mesh/time-step convergence and engineering conclusions require separate validation.

## Icepak example

1. Connect with an explicit `pid` or `port`.
2. Call `create_design(app_type="Icepak", project_name="Cooling", design_name="BoardThermal")`.
3. Use the dedicated design/setup tools for Icepak geometry, materials, sources, openings, fans, mesh operations, monitors, and setup creation; `run_python_code` is refused by default.
4. Call `validate_design`, then `analyze_design`.
5. Confirm solver state and inspect logs, monitor stabilization, maximum temperature, mass-flow conservation, heat balance, mesh/convergence data, and plots.

`analyze_design` is non-blocking for a design-level solve. A returned `started=true` is submission evidence, not proof of solver completion or engineering validity.

For Icepak, `analyze_design` defaults to `icepak_safe_mode=true` when no custom ACF file is supplied. This reliability mode submits one core, disables GPU allocation and automatic DSO settings, and returns both `requested_resources` and `effective_resources`. Set `icepak_safe_mode=false` only after validating the intended parallel configuration on the target AEDT installation; a caller-supplied `acf_file` also takes precedence over safe mode.

For solved Icepak designs, `export_results(export_type="convergence")` exports the residual monitor history from the native `.sd` result files to CSV. `export_results(export_type="mesh")` exports a solution profile and derives node, face, cell, and normal-completion evidence into CSV. The response records `export_method`, source file, and parsed details. These Icepak-specific paths avoid the generic `ExportConvergence` and `ExportMeshStats` calls that are unavailable in some AEDT releases.

Resources `aedt://status` and `aedt://agent-instructions` never attach implicitly.

## Remove Legacy Toolbar

The old `Start AEDT MCP Bridge` and `Stop AEDT MCP Bridge` buttons are not used. Remove only those known entries with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\remove_legacy_aedt_mcp_toolbar.ps1" -AedtRoot "G:\ANSYS206\ANSYS Inc\v261\AnsysEM"
```

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\scripts\run_live_acceptance.ps1 -Mode both
```

Live acceptance covers explicit PID and port targeting, repeated commands on one broker, disposable HFSS project save, and normal AEDT close while the broker is still connected. It fails if the "being used by another application, script or extension wizard" dialog appears.
