# AEDT guard is mandatory

- Every AEDT action goes through the `mcp__aedt__*` guard tools.
- If those tools are not visible in this session: **STOP**. Do not run `ansysedt.exe`, do not import
  `ansys.aedt.core` / PyAEDT, and do not edit `.aedt` files. Ask the user to enable the AEDT guard
  MCP bundle.
- Attach only to sessions the guard launched. A refusal about an unmanaged PID/port is final; do not
  work around it.
- Never set `AEDT_MCP_ALLOW_UNMANAGED=1` or `AEDT_ALLOW_UNGUARDED=1` yourself.
- Call `mcp__aedt__guard_status` before a measured run and report any `unmanaged` AEDT processes.
- Do not change antenna geometry or project structure without explicit user approval.
