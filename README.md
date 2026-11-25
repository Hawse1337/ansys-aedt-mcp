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
- operator-approved builders with guard-generated approval requests (`request_builder_approval`);
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
| approved builders | `run_approved_builder` executes an operator-allowlisted builder (script hash + typed params + output roots) so agents can run repeated campaigns autonomously without per-run code opt-in |
| approval requests | `request_builder_approval` lets an agent submit a candidate path + reason; the guard computes hashes, writes a pending request, and never edits the allowlist. The v1.7 GUI watches the request inbox, auto-refreshes pending rows, and clears stale approvals |
| tokens | `AEDT_GUARD_ENFORCE=1` binds run tokens to project/design/setup for the dedicated solve entries; operator-approved builders are vetted code and are not token-enforced |
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

## Pending approval requests

When a builder's script hash or parameter schema is not approved, an agent does not need to know
the request JSON format. It calls the non-executing tool:

```python
request_builder_approval(
    builder_id="p0_model",
    candidate_path=r"C:\...\scripts\candidates\p0_model_codex_20261122.py",
    reason="fix half-model port integration line and boundaries",
)
```

The guard computes the candidate SHA-256, reads the old approved hash, writes
`approval_requests/pending/<request_id>.json`, writes the candidate sidecar, and returns the
request id. `run_approved_builder` returns `approval_required` with `next_tool` and a minimal
template when it detects a hash/schema mismatch. The approver GUI (v9) watches the pending inbox,
raises a notification and can load/approve the candidate; approval still requires the operator.
See [docs/AI_BUILDER_APPROVAL_REQUEST.md](docs/AI_BUILDER_APPROVAL_REQUEST.md) and
[docs/BUILDERS.md](docs/BUILDERS.md).

For Windows notification-center delivery, register the approver once with pywin32 from the guard
venv; this creates the per-user AUMID/Start-menu shortcut and enables toast delivery:

```powershell
python tools/register_windows_notifications.py --exe "C:\ProgramData\ansys-aedt-mcp\tools\AEDT Builder Approver.exe"
```

The registration writes only HKCU and the current user's Start-menu folder. `--test-notify` verifies
the toast; Focus Assist / Do Not Disturb can still suppress banners.

GUI v13 also shows whether recent approved-builder runs came from the optimisation system:
`优化运行 (N)` lists source (`aster` vs `manual`), campaign, candidate, status and the campaign
completion gate (`PASS` / `未完成`). The guard records optional campaign/candidate provenance
passed by the aster adapter; it does not pass those fields to the builder script. See
[docs/BUILDERS.md](docs/BUILDERS.md#gui-v13-optimisation-provenance-and-completion-gate).

GUI v1.4 adds request supersede/reconciliation, atomic hash+schema approval and an operator toggle
for **低风险自动批准** (off by default; strict static policy, audit-only, not an OS sandbox). See
[docs/BUILDERS.md](docs/BUILDERS.md#request-supersede-and-reconciliation-v1.4).

Basic native report images now have a fixed read-only tool, `export_report_image`
(`3d` / `e` / `h`, arbitrary solved frequency and image size; `report_name` for an existing
saved report). It does not rebuild or solve and does not require a builder/allowlist approval;
changing frequency is just an argument. See
[docs/BUILDERS.md](docs/BUILDERS.md#native-report-image-export-no-allowlist-approval).

Approval requests now run a mandatory server-side preflight (compile, `run()` argument vs
schema completeness, declared-parameter shape, static risk) and can be waited on once with
`wait_for_builder_approval` instead of polling. Optional per-builder daily budgets prevent
approval churn. See [docs/APPROVAL_POLICY.md](docs/APPROVAL_POLICY.md).

### Custom native report specs (read-only)

For report types beyond the fixed 3D/E/H templates, pass `report_specs` to
`export_report_image`. Each entry supplies the AEDT report fields directly:

```python
export_report_image(
    project_path=...,
    design_name="Trunc_10GHz",
    output_dir=...,
    port=59543,
    report_specs=[{
        "name": "E_full_360",
        "expressions": ["dB(GainTotal)"],
        "report_category": "Far Fields",
        "plot_type": "Radiation Pattern",
        "setup_sweep_name": "Setup1 : LastAdaptive",
        "variations": {"Freq": ["10GHz"], "Phi": ["0deg", "180deg"], "Theta": ["All"]},
        "primary_sweep_variable": "Theta",
        "context": "InfiniteSphere",
    }],
)
```

#### Native trace overlay (copy/paste trace data)

AEDT can combine traces whose primary sweeps differ by copying trace data and
pasting it as static data into the target report. The guard exposes this as
`overlay_specs` on a report spec. For example, an H-plane Phi trace can be
pasted into an E-plane Theta polar report:

```python
report_specs=[{
    "name": "EH_native_overlay",
    "expressions": ["dB(GainTotal)"],
    "report_category": "Far Fields",
    "plot_type": "Radiation Pattern",
    "setup_sweep_name": "Setup1 : LastAdaptive",
    "variations": {"Freq": ["10GHz"], "Phi": ["0deg"], "Theta": ["All"]},
    "primary_sweep_variable": "Theta",
    "context": "InfiniteSphere",
    "overlay_specs": [{
        "name": "EH_native_overlay_H",
        "expressions": ["dB(GainTotal)"],
        "plot_type": "Radiation Pattern",
        "setup_sweep_name": "Setup1 : LastAdaptive",
        "variations": {"Freq": ["10GHz"], "Theta": ["90deg"], "Phi": ["All"]},
        "primary_sweep_variable": "Phi",
        "context": "InfiniteSphere",
    }],
}]
```

#### Equal-brightness overlay: save/reopen before export

`PasteTraces` leaves the imported trace selected; in non-graphical exports the
live base trace can then look dim. To get two equally bright traces, keep the
report, save the project, reopen it, then export the saved report by name:

```python
res = export_report_image(..., report_specs=[...], keep_report=True)
report_name = res["files"]["EH_native_overlay"]["kept_report"]
save_project(...)
disconnect_from_aedt(close_desktop=True, ...)
launch_aedt(...)
open_project(...)
export_report_image(..., report_name=report_name, ...)
```

No re-solve is needed; the report is native and both traces are drawn normally
after the reopen.
The base report keeps its live trace; each overlay is copied with
`CopyTracesData` and pasted with `PasteTraces`, so the exported JPG contains
both curves in one native AEDT report. Temporary reports are deleted afterwards.
This is still read-only: it creates native reports, exports JPGs, and removes
the temporary reports. It never rebuilds, solves, saves or changes geometry.
No builder/allowlist approval is required. Up to 25 specs per call; report
names must be safe filenames.

### Far-field sphere ranges (no approval)

Changing the native infinite-sphere sampling range is a fixed, non-code tool:

```python
set_infinite_sphere(
    project_path=...,
    design_name="Trunc_10GHz",
    theta_start=-180,
    theta_stop=180,
    theta_step=5,
    phi_start=0,
    phi_stop=360,
    phi_step=5,
    port=59543,
)
```

`theta_start=-180` / `theta_stop=180` enables a native full-360 E-plane report.
The tool edits only the named `Radiation/InfiniteSphere` field setup. It
reports `requires_resolve=false`; far-field sampling is post-processing, so no
FEM re-solve is needed. Recreate/export the report to read the new angles.
This is zero-approval and does not require editing a builder.

## Verification

- Offline suite: **525 passed, 1 skipped, 118 subtests**.
- Approved-builder workflow: [docs/BUILDERS.md](docs/BUILDERS.md).
- Live on AEDT 2026 R1 / PyAEDT 1.10.0: unmanaged attach refused, guard-launched session
  `managed: true`, `run_python_code` refused.
- Releases and the hardened package are published at:
  https://github.com/Hawse1337/pyaedt-mcp-hardening-kit/releases

## Enforcement boundary (v1.6.1)

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
- `request_builder_approval` creates an operator approval request for a changed builder candidate
  without executing AEDT or editing the allowlist.

## Dedicated Maxwell tools

The server adds 15 `maxwell_*` tools; the full surface is 67 tools. They cover coil terminals, windings and links, current excitations, rotational/translational motion bands, force/torque, eddy effects, setup creation, local mesh operations, design readback, activity, numerical results and field plots. Each requires explicit `project_name`, `design_name` and one `pid`/`port`.

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

## Approval workflow (2026-11-24)

- `request_builder_approval` runs server-side preflight before writing a request: compile,
  `run()` coverage, parameter schema and static risk.
- A new request supersedes older pending requests for the same builder; `already_approved` is
  returned when the allowlist already matches.
- `wait_for_builder_approval` does a single wait. On timeout, park the task; do not poll in a loop.
- Approval applies the script hash and missing schema entries atomically. Explicit
  `min`/`max`/`default`/`required` declarations win over inferred schema.
- `approval_budget_per_day` is an optional hard cap. `approval_churn` warns on repeated incremental
  requests; `approval_churn_limit` is an optional hard cap (see `docs/APPROVAL_POLICY.md`).
- First registration of a brand-new builder uses `request_type="new_builder"`, complete `new_params`,
  absolute `output_roots` and optional `targets`; the operator GUI creates the entry after review.
- `ff_step_deg` (or an equivalent native-sampling parameter) must be approved once; changing its
  value afterwards is a normal builder run, not a new approval.

## Report images and native evidence

`export_report_image` is a fixed, read-only tool and needs no builder approval. It can export a saved
report by `report_name` or create native 3D / E-plane / H-plane images. It cannot turn 10-degree
solved far-field data into true 5-degree data; the builder's infinite-sphere sampling must be set
before the solve. `export_report_image` never saves the project or changes geometry.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

Current offline record: **525 passed, 1 skipped, 118 subtests** (2026-11-25). Live AEDT acceptance
remains separate; an API success is submission evidence, not convergence proof.
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







