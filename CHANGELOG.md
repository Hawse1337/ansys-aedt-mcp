# Changelog

## 0.3.0 - 2026-11-20

Security hardening after a real agent bypassed the guard:

- **Managed-session registry**: AEDT sessions launched through the guard are recorded
  (`%LOCALAPPDATA%\ansys-aedt-mcp\managed_sessions.json`) with PID, process creation
  time, gRPC port and launch source.
- **Managed-only attach**: `connect_to_aedt` / PyAEDT attach through the broker refuses
  PIDs and ports that are not in that registry. Escape hatch: `AEDT_MCP_ALLOW_UNMANAGED=1`
  (explicit human opt-in for a session the user started).
- **`guard_status` MCP tool**: classifies every live `ansysedt.exe` as managed or
  unmanaged and reports the enforcement mode.
- **Command audit**: every MCP worker command appends a JSONL record (command, target,
  allowed/refused, argument SHA-256) to `<state>\guard_audit.jsonl`.
- **Launch bookkeeping before readiness**: the launcher records ownership immediately
  after spawning AEDT, so its own readiness probe passes the managed-session check.
- **Threat model and client integration docs**: `docs/THREAT_MODEL.md`,
  `docs/CLIENTS.md`, plus ready-to-install DSH MCP bundle and Codex configuration under
  `examples/clients/`.
- Fail-closed guidance for agent clients: if the guard MCP tools are absent, stop instead
  of falling back to direct PyAEDT or `ansysedt.exe`.

Residual risk remains: a same-user process with unrestricted shell can still start AEDT
directly. OS-level isolation (separate account/VM, AppLocker/WDAC) is required for a hard
boundary; see `docs/THREAT_MODEL.md`.

## 0.2.0 - 2026-11-20

- Update the pinned PyAEDT client from 1.9.0 to 1.10.0 (current stable,
  https://aedt.docs.pyansys.com/version/stable/index.html).
- Live-attach audit on AEDT 2026.1 with PyAEDT 1.10.0: ping and list_projects
  succeed through the attach-only guard, and the desktop release path still works.
- Remove the UTF-8 BOM from pyproject.toml so 	omllib/pytest can read it.
- Refresh the PyAEDT version statements in README, deployment and Maxwell documents.

## 0.1.0

- Internal pre-release: PyAEDT-backed MCP server, guarded solve workflow, evidence capture.
