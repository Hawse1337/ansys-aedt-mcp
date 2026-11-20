# AEDT guard is mandatory

- Every AEDT action goes through the `ansys-aedt` MCP tools (`mcp__...__check_aedt_installed`,
  `launch_aedt`, `connect_to_aedt`, `guard_status`, …).
- If the guard MCP tools are unavailable in this session, **STOP**. Do not run `ansysedt.exe`, do not
  import `ansys.aedt.core` / PyAEDT, and do not edit `.aedt` files. Ask the user to enable the guard.
- Attach only to sessions the guard launched; it refuses unmanaged PIDs/ports. Do not try to work
  around that refusal.
- Never set `AEDT_MCP_ALLOW_UNMANAGED=1` or `AEDT_ALLOW_UNGUARDED=1` the human operator sets them in the client configuration.
- Before a measured run, call `guard_status` and report any `unmanaged` AEDT processes.
- Do not change antenna geometry or project structure without explicit user approval; state the
  change, the expected effect and the evidence you will collect.
