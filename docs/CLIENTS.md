# Agent client integration (DSH and Codex)

The guard is only effective when the agent's MCP client actually speaks to it. This page gives
working configurations and the fail-closed instruction both clients should carry.

Core rule for every client:

> If the guard MCP tools are not visible in the session, stop. Do not launch `ansysedt.exe`, do not
> import PyAEDT, do not edit `.aedt` files. Ask the user to connect the guard.

## DeepSeek Harness (DSH)

Install the guard as an MCP bundle (configuration-only). A ready bundle lives in
`examples/clients/dsh/aedt-guard-mcp/`; install its directory through **Plugins → Add plugin** or
the `plugin_manager` tool's `install_bundle` action.

After installation and a new session, these tools appear:

```text
mcp__aedt__check_aedt_installed
mcp__aedt__list_aedt_sessions
mcp__aedt__guard_status
mcp__aedt__launch_aedt
mcp__aedt__connect_to_aedt
mcp__aedt__run_python_code   (guarded surface; prefer dedicated tools)
...
```

Verification:

1. `mcp__aedt__check_aedt_installed` → AEDT/PyAEDT present.
2. `mcp__aedt__guard_status` → `enforcement: managed_only`, empty `unmanaged` list.
3. `mcp__aedt__launch_aedt` → returns `managed: true`; `guard_status` now lists it under
   `managed_sessions`.

Add the fail-closed rule to the agent instructions/skill (for example in an `AGENTS.md` or an AEDT
skill body):

```text
AEDT GUARD IS MANDATORY
- If mcp__aedt__* tools are missing, STOP. Do not run ansysedt.exe, PyAEDT, or edit .aedt files.
- Only attach through the guard; the guard refuses unmanaged sessions.
- Never set AEDT_MCP_ALLOW_UNMANAGED=1 or AEDT_ALLOW_UNGUARDED=1 in automated runs.
- Report mcp__aedt__guard_status before claiming a session is trustworthy.
```

## OpenAI Codex

`config.toml` (global `~/.codex/config.toml` or project `.codex/config.toml`):

```toml
[mcp_servers."ansys-aedt"]
command = 'C:\ProgramData\ansys-aedt-mcp\.venv\Scripts\python.exe'
args = ['C:\ProgramData\ansys-aedt-mcp\mcp_server.py']
cwd = 'C:\ProgramData\ansys-aedt-mcp'
startup_timeout_sec = 300
tool_timeout_sec = 2400

[mcp_servers."ansys-aedt".env]
AEDT_VERSION = '2026.1'
AEDT_INSTALL_DIR = 'D:\ANSYS Inc\v261\AnsysEM'
AEDT_WORKER_TIMEOUT = '1800'
AEDT_LAUNCH_TIMEOUT = '240'
```

Put the same fail-closed rule in the project's `AGENTS.md` so the Codex agent sees it:

```markdown
## AEDT guard is mandatory
- Use the `ansys-aedt` MCP tools for every AEDT action.
- If those tools are unavailable, STOP — never use shell-level PyAEDT or `ansysedt.exe`.
- Never set AEDT_MCP_ALLOW_UNMANAGED=1 or AEDT_ALLOW_UNGUARDED=1 yourself.
- Call `guard_status` before a measured run and report unmanaged sessions.
```

Approval posture: keep a human in the loop around solver time. Codex's approval policy and sandbox
mode are configured in `config.toml` (for example an `approval_policy`/`sandbox_mode` pair that asks
before network or full-disk actions); the guard's managed-session refusal is independent of those
settings and still stops attach to unmanaged sessions.

Example files: `examples/clients/codex/config.toml`, `examples/clients/codex/AGENTS.md`.

## What the client-side rule cannot do

A client instruction is advisory. The enforcing control is the guard's managed-session registry and
audit log. For a hard boundary, isolate the solver at OS level (separate account/VM); see
`docs/THREAT_MODEL.md`.
