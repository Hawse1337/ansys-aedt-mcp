# AEDT guard is mandatory

- Every AEDT action goes through the `mcp__aedt__*` guard tools.
- **Session start:** before the first AEDT action, call `mcp__aedt__guard_status`. If that tool is
  missing or errors, **STOP** and tell the user the guard MCP bundle failed to start. Do not run
  `ansysedt.exe`, do not import `ansys.aedt.core` / PyAEDT, and do not edit `.aedt` files.
- Attach only to sessions the guard launched (`scan.enforcement == "managed_only"`); report
  `scan.unmanaged`. A refusal about an unmanaged PID/port is final.
- Never set `AEDT_MCP_ALLOW_UNMANAGED`, `AEDT_ALLOW_UNGUARDED`, `AEDT_MCP_ALLOW_ARBITRARY_CODE`,
  `AEDT_GUARD_ALLOW_CREATE`, `AEDT_GUARD_ATTACH_OPEN` or `AEDT_GUARD_SEARCH_DIRS` yourself; those are
  human operator decisions in the client configuration.
- Call `mcp__aedt__guard_status` before a measured run and report any `unmanaged` AEDT processes.
- Do not change antenna geometry or project structure without explicit user approval.
- Do not drive the AEDT GUI through computer-use/browser automation; use the guard tools.
