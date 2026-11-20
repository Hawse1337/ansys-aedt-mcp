# Changelog

## Unreleased

Follow-up fixes from the second real-agent session (kept on 0.3.2; no new release was cut).

- **PID-target attach.** PyAEDT 1.10.0 rejects `version` + `aedt_process_id` together with a
  spurious "version ... doesn't correspond to the pid" error, while PID-only attach needs a
  version. The broker now verifies the PID and attaches through the process's recorded gRPC port;
  a PID without a discoverable gRPC port is refused with a clear message instead of hitting the
  vendor bug. Subsequent app construction uses the port.
- **Open-project path/name resolution.** Tools now track the absolute path of projects opened via
  `open_project`, so `validate_design`, `analysis_status`, `export_config` and friends accept a
  project name or an absolute path for an already-open project regardless of its directory (the
  project guard's search roots otherwise only cover the process working directory).
- **Truthful close.** `disconnect_from_aedt(close_desktop=true)` now waits for the verified PID,
  force-terminates after a grace period, and returns `closed_desktop`, `process_alive`,
  `forced_termination` and the PID instead of echoing the request.
- **PID-reuse safety.** Close verification records the AEDT process creation time and will
  not terminate a process that merely reused the same PID after AEDT exited.
- Regression tests in `tests/test_guard_followups.py` (469+5 tests).

## 0.3.2 - 2026-11-20

Follow-up from the round-2 adversarial re-review (client consistency + regressions):

- WR90: `build_wr90_waveguide` accepts `run_token` so it can be used under
  `AEDT_GUARD_ENFORCE=1`; the extra guarded-run setups now carry the run token.
- Guarded-run adapter commands (`preflight_design`, `finalize_solve`, `export_*`, …) are appended to
  the guard-wide audit log; launch is audited only after its bookkeeping result is known.
- PyAEDT app objects are identity-checked against the verified desktop after construction
  (`desktop_class.aedt_process_id`/`port`), closing the vendor-fallback path for `create_design`.
- Managed registry prunes dead records on new launches; the launcher test seam was removed
  (tests patch `_trusted_aedt_roots` explicitly instead).
- Client docs consistency: Codex TOML snippet now contains `required` / approval mode / disabled
  tools; shipped AGENTS examples match the live six-variable never-set policy; DSH DETAIL and
  skill text no longer claim flat `enforcement` or "every worker command"; test command corrected to
  `python -m pytest tests -q`.
- Suite: 468 passed, 1 skipped.

## 0.3.1 - 2026-11-20

Hardening after an adversarial review of 0.3.0 (three independent reviewers):

- Managed-session registry: strict schema (PID, numeric create time, port), verified flag,
  cross-process file lock, unique temp file + fsync, non-mutating reads.
- Enforcement: port attach resolves the actual listener owner and compares it with the recorded
  PID; PID attach requires a verified record (a fresh spawn record only while a launch is in
  flight); malformed/missing create times fail closed.
- Launch: the executable is restricted to configured AEDT roots; failed launches drop their
  registry record and keep duplicate-launch protection; `managed: true` is only reported after
  the record is actually visible.
- Arbitrary code: refused by default at the MCP surface **and** at the real worker backend
  (`AEDT_MCP_ALLOW_ARBITRARY_CODE` is a human operator decision). Refusals no longer print the
  enable recipe.
- Tokens: run tokens must name their full scope; a setup-scoped token no longer authorises
  `analyze_all_designs` or a call that omits project/design/setup.
- Solve coverage: `build_wr90_waveguide(solve=True)` and `run_python_script` bodies now pass the
  analysis gate when `AEDT_GUARD_ENFORCE=1`.
- Audit: launch/release events are audited, audit health is reported by `guard_status`,
  `summarize` reports malformed lines (`tampered`), token paths that do not exist no longer divert
  the campaign audit.
- Tool output paths under the guard state directory are refused (`export_results`,
  `export_config`, `screenshot`, `save_project`).
- Client integration: Codex example/live config uses `required = true`,
  `default_tools_approval_mode = "prompt"` and disables `run_python_*`; DSH gets an always-loaded
  fail-closed `AGENTS.md`, guard policy in the MCP instructions, bundle timeouts, and a
  rewritten AEDT skill without direct-launch advice.
- Tests: `tests/conftest.py` redirects guard state to a temp dir for the whole suite; new negative
  tests cover malformed/unmanaged targets, token scope, refusal paths, audit health and the
  arbitrary-code gate.
- Docs: corrected audit-coverage claims, `guard_status` schema, tool count (63), user-opened AEDT
  attach guidance, Codex approval wording, and residual-risk statements (registry/audit are
  user-writable; OS isolation required for a hard boundary).

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
