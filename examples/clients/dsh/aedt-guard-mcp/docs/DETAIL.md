# AEDT guard MCP

Connects the guarded AEDT MCP server (`C:\ProgramData\ansys-aedt-mcp`) to DeepSeek Harness as the
server name `aedt`. Tools appear as `mcp__aedt__<tool>`, for example:

- `mcp__aedt__check_aedt_installed`
- `mcp__aedt__list_aedt_sessions`
- `mcp__aedt__guard_status`
- `mcp__aedt__launch_aedt`
- `mcp__aedt__connect_to_aedt`

The guard records guard-launched AEDT sessions and refuses attach to unmanaged PIDs/ports
(`AEDT_MCP_ALLOW_UNMANAGED=1` is a human-only escape hatch). Every worker command is appended to
`<state>/guard_audit.jsonl`.

## Fail-closed rule for the agent

If `mcp__aedt__*` tools are missing, the agent must stop: no `ansysedt.exe`, no direct PyAEDT, no
`.aedt` edits. Copy the rule from `AGENTS.md` in this folder into the project or agent instructions.

## Verify after install

1. Start a new session.
2. `mcp__aedt__check_aedt_installed`
3. `mcp__aedt__guard_status` → `enforcement: managed_only`
4. `mcp__aedt__launch_aedt` → `managed: true`

See the guard repository `docs/CLIENTS.md` and `docs/THREAT_MODEL.md` for the full contract.
