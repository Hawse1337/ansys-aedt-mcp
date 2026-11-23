# Changelog

## Unreleased

- **Astra audit fixes (2026-11-23).** Applied the external audit patch set: file-lock
  serialization for approval/allowlist writes, content-addressed candidate snapshots and
  approval pre-state checks, launcher ownership/close verification, and stricter approval
  request content validation. Full offline guard suite: 517 passed, 1 skipped (regular
  Windows long-path case; the extended-length variant passes separately). GUI selftest passes.
- **Approval/pending cleanup and report-name fix.** Superseded approvals are archived and removed
  from `pending/` (old tombstones are purged on reconcile), and `export_report_image` accepts an
  omitted `report_name` without passing `None` through the backend.
- **Approved builders (autonomous modelling).** New `run_approved_builder` tool executes an
  operator-allowlisted builder script (SHA-256 verified at MCP and worker layers, typed parameter
  validation, output roots, unique project copy, managed session, attach-only wrapper, artifact
  and audit detail). No per-run arbitrary-code opt-in is required. `docs/BUILDERS.md` documents
  the allowlist, operator workflow and residual same-user risk.
- **Guard-generated approval requests (2026-11-23).** New non-executing `request_builder_approval`
  tool: an agent supplies a candidate path + reason; the guard computes the candidate SHA-256,
  reads the old approved hash, writes a pending request/sidecar, deduplicates repeated calls and
  audits the request. `run_approved_builder` returns `approval_required` with
  `next_tool=request_builder_approval` and a minimal template on hash/schema mismatch. The v9
  approver GUI adds a pending-request inbox, notification, candidate loading and request
  archival; approval remains operator-only.
- **Registered Windows notifications (2026-11-23).** v10/v11 fix the toast helper and add
  `tools/register_windows_notifications.py`: per-user AUMID `Hawse1337.AEDTBuilderApprover`,
  Start-menu shortcut with `System.AppUserModel.ID`, toast/action-center enablement and a
  best-effort notification-service refresh. Verified with the Windows notification platform
  event log (`Toast ... delivered`); Focus Assist / Do Not Disturb can still suppress banners.
- **Optimisation provenance and completion gate (2026-11-23).** `run_approved_builder` accepts
  optional campaign/candidate/purpose/source/optimizer provenance and writes
  `optimization_runs.jsonl`; the aster `approved_builder` adapter evaluates through the approved
  builder (no arbitrary code), writes a campaign manifest and a completion gate. GUI v13 adds
  **优化运行 (N)**, per-run source (`aster`/`manual`) and gate (`PASS`/未完成) display.
  `submit_guarded_run` also records `builder_id="guarded_run"` provenance, so manual runs that
  bypass the approved builder remain visible. This is visibility/evidence, not yet task-mode
  enforcement.
- **Approval preflight / wait / budget (2026-11-23).** `request_builder_approval` now runs a
  server-side preflight: candidate compiles, `run()` arguments must be present in the allowlist
  or declared in the same request, declared parameter schemas are validated, and a static risk
  level is recorded. Failures return `status=preflight_failed` and create no pending request.
  New `wait_for_builder_approval` waits once for a terminal decision instead of polling; timeout
  is an explicit parked state. Optional `approval_budget_per_day` per builder caps churn and
  asks the agent to consolidate. `docs/APPROVAL_POLICY.md` is the contract. Tests 510.
- **Native report-image export (2026-11-23).** New fixed read-only
  `export_report_image` MCP tool exports AEDT-native 3D Polar / E-plane / H-plane JPGs from
  solved data for any frequency, plot selection and image size. E uses the dB-radial
  `Radiation Pattern`; H uses a native rectangular Phi/dB plot (the H cut varies only ~0.5 dB,
  so AEDT's automatic Radiation Pattern radius was linear and Phi wrapped past 360°). Passing
  `report_name` exports an already-saved operator report (e.g. the 5° dual-trace `Gain Plot1`)
  exactly as configured. It never rebuilds, solves, saves
  or changes geometry, and it does **not** require a builder/allowlist approval. Tool surface 66,
  tests 505.
- **Launcher orphan recovery (2026-11-23).** A timed-out AEDT launch used to discard the
  guard-spawned managed record while keeping the live process tracked, so managed-only policy
  could neither attach nor relaunch; the operator had to close the window by hand. The launcher now
  keeps the spawn record on handshake failure, adopts it on retry when its gRPC port is ready, and
  otherwise auto-releases the orphan before launching a fresh session.\n- **Approval flow v1.4 (2026-11-23).** One active request per builder: new requests supersede old
  pending ones (tombstone + `superseded_by`), already-applied candidates are archived without
  asking again, and the GUI reconciles stale ids. Approval now updates script hash **and** the
  merged parameter schema atomically (manual and automatic paths share it). A GUI checkbox enables
  **低风险自动批准** (default off): only compile-clean, unchanged-import, unchanged-top-level,
  no-dangerous-call, small-diff candidates with valid schema are auto-applied; the rest stay
  pending. Tool surface 65, tests 502.
**Same-version release rebuild (2026-11-21).** The v0.3.2 wheel/sdist/source assets were rebuilt
from this commit (still 0.3.2; no new tag) so installers contain the follow-up fixes: PID->port
attach with version pinning, guarded HFSS app factory, realistic project APIs, truthful close
(\`pid_reused\` / \`release_error\`), per-consumer project normalization and \`clear_stale_lock\` audit.
Test suite at rebuild time: **481 passed, 1 skipped, 118 subtests**.

- **Audit follow-ups.** Restored version pinning on attach (version+port; the vendor bug only
  occurs with version+PID), guarded the HFSS app factory with the attach-only wrapper, use the
  realistic PyAEDT Desktop project APIs (ctive_project_name/project_path), normalize project
  arguments per consumer (path for constructors, name for the app resolver; .aedb supported),
  drop stale project tracking on close_projects, and audit clear_stale_lock deletions.
- Close verification requires a pre-release identity snapshot and process name before any
  force-kill; recycled or unverified PIDs are never terminated.
- Added 	ests/test_close_verification.py; suite is 481 passed, 1 skipped, 118 subtests.
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
- Regression tests in `tests/test_guard_followups.py` (481 tests).

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
