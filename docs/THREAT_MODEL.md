# Threat model

`ansys-aedt-mcp` is a **lifecycle and evidence guard** for AEDT automation. It is not an OS
sandbox. This document states exactly what it can and cannot enforce, so nobody mistakes it for a
security boundary it is not.

## Assets

1. AEDT project files (`.aedt`) and their solved results.
2. The user's interactive AEDT sessions and unsaved work.
3. Solve evidence (S11, far fields, native reports) attributed to a design.
4. The user's approval to change design geometry or run solves.

## Adversaries

- **In scope:** an AI agent or script that uses the guard's MCP tools (or the guard worker API) and
  tries to attach to AEDT sessions, modify projects or run solves without going through the guarded
  path.
- **Out of scope:** a process with the same Windows user and unrestricted shell access. Such a
  process can start `ansysedt.exe`, import PyAEDT and drive AEDT directly. No Python-level guard can
  prevent that; only OS isolation can.

## Controls (v0.3.0)

| control | behaviour |
|---|---|
| managed-session registry | every AEDT launched through the guard is recorded (PID, process create time, gRPC port, launch source) in a state file |
| managed-only attach | `connect_to_aedt` / PyAEDT attach through the guard refuses PIDs/ports that are not in the registry; escape hatch `AEDT_MCP_ALLOW_UNMANAGED=1` is an explicit human opt-in |
| unmanaged detection | `guard_status` classifies every live `ansysedt.exe` as managed or unmanaged and reports the enforcement mode |
| command audit | every MCP worker command appends a JSONL line (command, target, allow/refuse, argument hash) to `<state>/guard_audit.jsonl` |
| analysis enforcement (pre-existing) | when `AEDT_GUARD_ENFORCE=1`, analysis submissions need a valid run token; `AEDT_ALLOW_UNGUARDED=1` records an escape-hatch bypass |

State location: `%LOCALAPPDATA%\ansys-aedt-mcp` (override with `AEDT_MCP_STATE_DIR`).

## Residual risks (known, accepted or mitigated outside the guard)

1. **Same-user direct AEDT.** A shell with the user's rights can launch AEDT directly and bypass all
   MCP controls. Mitigation: run the agent under a restricted account (AppLocker/WDAC deny
   `ansysedt.exe`; separate Windows account/service for AEDT+guard), or run AEDT in a separate
   VM/container and expose only the guard service.
2. **Arbitrary code tools.** The MCP surface includes `run_python_code` / `run_python_script`. Code
   that runs inside the worker can do anything the worker can do. When `AEDT_GUARD_ENFORCE=1`,
   free-form code that mentions analysis is blocked without a run token, but this is a textual check,
   not a sandbox. Prefer dedicated tools, and set `AEDT_GUARD_ENFORCE=1` for production campaigns.
3. **Port reuse / PID reuse.** The registry stores process create times and prunes dead entries;
   a session that exits and a new process reusing its PID is not considered managed.
4. **Guard state tampering.** A same-privilege process could edit the registry file. Integrity is
   therefore advisory; pair the guard with file audit (4663) and process-create audit (4688) when
   accountability matters.

## Client obligations (agent side)

- **Fail closed:** if the guard MCP tools are not available in the session, do not fall back to
  direct PyAEDT or `ansysedt.exe`; stop and ask the user to enable the guard.
- Treat `AEDT_MCP_ALLOW_UNMANAGED=1` as a human decision, never set it inside an automated loop.
- Report `guard_status` (managed/unmanaged sessions) before claiming a session is trustworthy.

See `docs/CLIENTS.md` for concrete DSH and Codex configuration.
