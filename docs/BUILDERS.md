# Approved model builders (`run_approved_builder`)

`run_approved_builder` is the **autonomous** path for repeated modelling runs. It lets an agent
execute an operator-approved builder with typed parameters — no per-run arbitrary-code opt-in and
no `run_python_code` gate.

## Why

The default-deny `run_python_code` is correct for unbounded code, but design campaigns need to run
the same reviewed modelling code many times with different parameters. The allowlist approves the
**capability once**; the agent runs it autonomously afterwards.

## Allowlist

The operator creates `builders.local.json` next to the guard (or sets `AEDT_GUARD_BUILDERS`):

```json
{
  "version": 1,
  "builders": {
    "p0_model": {
      "script": "C:\\ProgramData\\ansys-aedt-mcp\\approved\\p0_model.py",
      "sha256": "<sha256 of the script>",
      "entrypoint": "run",
      "params": {
        "theta_deg": {"type": "number", "min": 0, "max": 60, "required": true},
        "family": {"type": "enum", "values": ["A", "B"], "required": true},
        "feed": {"type": "enum", "values": ["ideal", "coax50"], "required": true},
        "gap_mm": {"type": "number", "min": 0, "max": 40},
        "r_tip_mm": {"type": "number", "min": 0, "max": 40}
      },
      "output_roots": ["C:\\Users\\Administrator\\Desktop\\da project\\artifacts"]
    }
  }
}
```

Copy/ACL the reviewed builder into a non-agent-writable directory (for example
`C:\ProgramData\ansys-aedt-mcp\approved\p0_model.py`) and use that path here. The example
above intentionally does not point into the campaign workspace.

`builders.local.json` is gitignored. The script must be an absolute path; its SHA-256 is checked at
the MCP layer and again, on the exact bytes that are executed, in the worker. Changing the script
requires the operator to review and update the allowlist entry.

## Tool contract

```text
run_approved_builder(
    builder_id="p0_model",
    project_path=<existing .aedt>,
    outdir=<directory under output_roots>,
    params={"theta_deg": 20.6, "family": "A", "feed": "ideal", ...},
    pid/port=<optional existing managed AEDT>,
) -> {result, stdout, artifacts, builder, ...}
```

- No explicit target → the tool launches a managed AEDT session and leaves it open for reuse.
- The source project is copied to `outdir/_work/<uuid>/p0work_<uuid>_<name>.aedt`; the unique stem
  prevents PyAEDT from silently attaching to an already-open same-name project. The original is
  never passed to the builder.
- Parameters are validated against the allowlist schema (numbers finite/in-range, enums, unknown
  parameters refused). `outdir` must be under an allowed output root and outside guard state.
- The builder entrypoint receives `port`, `project` (working copy) and `outdir`, plus the validated
  parameters. It must return a JSON-serializable dict.
- Every run is audited (`run_approved_builder`, builder id, param digest, refusal/success).
- The attach-only wrapper stays active while the builder executes; the active project must remain
  inside the working/output roots.

## Windows GUI (recommended)

Double-click `AEDT Builder Approver.exe` (desktop copy; source at
`tools/builder_approver_gui.py`). It is a small Tkinter operator tool:

1. select the builder (for example `p0_model`);
2. choose the candidate script;
3. click **Check candidate version** to inspect the summary/diff;
4. click **Approve & update allowlist**.

The GUI recomputes the SHA-256, copies the candidate to
`C:\ProgramData\ansys-aedt-mcp\approved\<builder>.py`, backs up the allowlist, and updates it
atomically. The CLI (`tools/approve_builder.py`) remains available for automation.

## GUI v4 request metadata

The candidate file path is now a full-width editable field plus a wrapping full-path line.
Request source is read from a sidecar next to the candidate:
`<candidate>.approval-request.json` (or `approval_requests/<stem>.json`). It should contain
`platform`, `session_id`, `session_title`, `requested_at`, `reason` and `candidate_sha256`.
Agents can write it with `tools/request_builder_approval.py`; the GUI also offers an
"Edit source" dialog. The approval record stores this metadata plus the candidate hash.

## GUI v3 notes

- The candidate action is labelled **Check candidate version** (not "view diff").
- Default output is a concise summary (add/remove counts, changed blocks, key lines); full diff is
  opt-in.
- The approval confirmation shows the request time (candidate mtime) and source (candidate path),
  and writes `approval` metadata into the allowlist entry; the status panel displays it.

## GUI v2 notes

`AEDT Builder Approver v2.exe` adds:

- **candidate status**: shows the current approved hash vs the candidate's freshly computed hash
  (ignore stale hashes quoted in chat; the GUI always recomputes);
- **Edit params schema**: e.g. add `"gap"` to `feed.values` before approving; saving updates the
  allowlist with a backup.

Approve the candidate script only after reviewing the diff; the allowlist keeps pointing at the
frozen `approved/<builder>.py` copy (do not follow old instructions that point it back into the
campaign workspace).

## Keeping the approved copy in sync

The repository ships an operator helper at `tools/approve_builder.py` (a local copy is also
available as `D:\AgentAEDT_Collab\approve_builder.py`). It keeps a frozen approved copy under
`C:\ProgramData\ansys-aedt-mcp\approved\<builder_id>.py` and updates the allowlist atomically:

```powershell
& $py .\tools\approve_builder.py review p0_model "<new p0_model.py>"
& $py .\tools\approve_builder.py apply  p0_model "<new p0_model.py>" --yes
& $py .\tools\approve_builder.py check  p0_model
```

`apply` preserves the parameter schema/output roots; changing the builder code or adding parameters
still requires a human review. `check` must print `STATUS: OK`.

## Operator workflow

1. Review the builder script (geometry, boundaries, no destructive operations).
2. Compute its SHA-256, add an allowlist entry.
3. Run a smoke build; inspect the artifact and validation result.
4. Agent runs candidates by changing only typed parameters.

## Security boundary

The allowlist is an **integrity control for same-user deployments, not an OS sandbox**. An agent
with unrestricted file-write access can edit the allowlist and the builder; a same-user shell can
already bypass the guard. Keep the allowlist and builder scripts outside agent-writable workspace
locations where possible, protect them with OS ACLs, or use a separate account/VM for a hard
boundary. Approved code can still call PyAEDT analysis APIs; do not approve scripts you would not
run yourself.

Residual risks:

- Allowlist and builder scripts are same-user writable unless OS ACLs are applied.
- Approved code can create arbitrary AEDT objects/analysis; approval covers every branch of the
  script.
- The guard verifies the working project and audited session, but it is not a solver sandbox.


## GUI v5 appearance

The approver ships with a custom shield/check logo (`tools/assets/aedt_builder_approver.ico`),
a dark navy header, Segoe UI typography, teal primary actions, amber accent and a dark log panel.
The window is resizable; the candidate path expands and wraps. Regenerate the icon with
`python tools/make_approver_icon.py`.

## GUI v6: form-based parameter schema

The **“编辑参数（表单）”** dialog removes raw-JSON editing from the operator workflow:

- parameters are rows: name, type (`number` / `enum` / `string`), required, range or values;
- **添加参数 / 编辑 / 删除** generate valid JSON; there are no commas or braces to type;
- **目标设计（design）** is a dedicated switch, not a normal geometry parameter;
- **从候选脚本检测** parses the candidate's `run()` signature and obvious enum values
  (`family` comparisons, argparse `choices`) and offers to add missing parameters/enums;
- save validates every row and writes the allowlist atomically with a timestamped backup;
- approval blocks with a prompt if the candidate declares parameters/enums that are not yet in
  the schema, and fixes the approval metadata's `candidate_sha256` to the file actually approved.

The guard schema format is unchanged; this is an operator UX layer over the same strict
`params` object. Example: after approval, `design` must be supplied in the builder call
(`{"design": "Trunc_10GHz", ...}`) because the builder's `run()` requires it.

## GUI v7: remembers the last candidate

The approver remembers the last candidate script and builder in
`%LOCALAPPDATA%\ansys-aedt-mcp\builder_approver_ui.json` (override for testing with
`AEDT_APPROVER_UI_CONFIG`). On the next launch it pre-fills the candidate path and starts the
file picker in the last folder, so repeated approvals for the same project do not require
navigating back to `scripts\p0_model.py` every time. It only stores paths, never credentials.

## GUI v8: desktop notification for pending approvals

The GUI runs a small background watcher while it is open (it can be minimized). When the selected
candidate script differs from the approved SHA-256, or when an approved script declares parameters/
enums missing from the allowlist schema, it shows a **Windows toast**:

- title: `AEDT Builder 待审批` / `AEDT Builder 参数表待更新`;
- body includes builder id, candidate file, short SHA-256, and sidecar reason/source when available;
- one notification per candidate hash/schema mismatch, so it does not spam;
- the **桌面通知** checkbox enables/disables it; **测试通知** verifies toast delivery;
- the setting and last paths live in `builder_approver_ui.json`; no credentials.

`--test-notify` on the exe sends a test toast and exits with the helper status. The GUI must be
running (minimized is fine) for notifications; it does not install a Windows service.

## AI-originated approval requests

Agents should not hand-write the approval sidecar. Use the non-executing MCP tool:

```python
request_builder_approval(
    builder_id="p0_model",
    candidate_path=r"C:\...\scripts\candidates\p0_model_codex_20261122.py",
    reason="fix half-model port integration line and boundaries",
)
```

The guard computes the candidate SHA-256, reads the old approved hash, writes a request under
`%LOCALAPPDATA%\ansys-aedt-mcp\approval_requests\pending\`, and returns a request id/path. It never
edits the allowlist or approves anything. `run_approved_builder` additionally returns
`status="approval_required"`, `next_tool="request_builder_approval"` and a minimal template when it
detects a script-hash or parameter-schema mismatch.

GUI v9 adds a **待审批请求 (N)** button and watches that pending directory, so new requests trigger
the same Windows notification. Loading a request fills the candidate path; a `schema_only` request
opens the parameter form; approval/denial archives the request.

### Request supersede and reconciliation (v1.4)

Only one active request is kept per builder. Creating a replacement:
- archives the older request with `status=superseded` and `superseded_by=<new id>`;
- leaves a small superseded tombstone in `pending/` so a waiter polling the old id can see why;
- returns `superseded_requests` from `request_builder_approval`.

The request tool and the GUI also reconcile:
- if the current allowlist hash already equals the candidate and no new schema was requested, the
  request is archived as `approved/already_applied` instead of asking again;
- a pending request older than a newer approved request for the same builder is retired as
  superseded. This is what removes stale ids after a replacement is approved.

### Atomic approval (v1.4)

The GUI approval action now writes **one** allowlist update containing:
- the new script path and SHA-256;
- all newly detected / requested parameter schema entries (including enum value additions).

If the merged schema is invalid, approval is refused. This removes the old
"hash approved but the parameter table was not updated" failure mode. The same merge/apply code is
used by automatic approval.

### Low-risk automatic approval (v1.4)

A checkbox in the GUI (`低风险自动批准`) is **off by default**. When enabled, the GUI scans pending
requests every 5 seconds and auto-approves only when all checks pass:

- candidate hash matches the request;
- candidate compiles;
- imports unchanged;
- top-level function/class set unchanged;
- no network / subprocess / eval/exec / destructive file calls;
- small diff;
- schema can be merged and validated.

Everything else remains pending for manual review. Auto-approval is a same-user integrity control,
not an OS sandbox; the operator opts in explicitly and can turn it off while sitting at the
computer.

## GUI v11: registered Windows notifications

The v8/v9 toast helper used an unregistered app id; Windows could accept the call and silently drop
the toast (`PolicyReason=GlobalSettingDisabled` / missing AUMID). v11 registers a real per-user
Windows app:

```powershell
python tools/register_windows_notifications.py --exe "C:\ProgramData\ansys-aedt-mcp\tools\AEDT Builder Approver.exe"
```

This writes only HKCU + the current user's Start-menu folder:

- AUMID `Hawse1337.AEDTBuilderApprover`;
- `Software\Classes\AppUserModelId\Hawse1337.AEDTBuilderApprover` display name/icon;
- Start-menu shortcut carrying `System.AppUserModel.ID`;
- `ToastEnabled=1` and per-app `Enabled` / `ShowInActionCenter`;
- best-effort restart of the per-user notification service so the setting takes effect.

`notify.py` now posts with that AUMID. `--test-notify` verifies the helper; check the Windows
notification center. If Focus Assist / Do Not Disturb is on, Windows can still suppress banners.
Pass `--no-enable-notifications` if you only want the shortcut/AUMID and prefer to control the
global Windows notification setting yourself.

## GUI v13: optimisation provenance and completion gate

`run_approved_builder` accepts optional provenance fields:
`campaign_id`, `campaign_dir`, `candidate_id`, `purpose`, `source`, `optimizer`. They are
recorded, never passed to the builder script. Defaults stay `source="manual"`, so existing
callers are unchanged. The aster adapter sends `source="aster"` / `optimizer="aster"` and a
campaign/candidate id.

The guard appends each approved-builder run to `%LOCALAPPDATA%\ansys-aedt-mcp\optimization_runs.jsonl`.
`submit_guarded_run` submissions are recorded too (`builder_id="guarded_run"`, default `source=manual`),
so a manual solve that bypasses the approved builder is still visible as a manual run.
GUI v13 adds:

- a **优化运行 (N)** button listing recent runs with source (`aster` vs `manual`), campaign,
  builder, candidate and status;
- a main-window line: `最近 builder 运行：aster/优化系统 ...` or `manual/手工 ...`;
- a **完成门槛** column read from `optimization_gate.json` / `optimization_manifest.json`
  (`PASS` / `未完成` / `—`).

The aster side provides `aster.approved_builder`:
`ApprovedBuilderExecutor` evaluates a candidate through `run_approved_builder` and parses the
returned `result.s1p` (and optional `farfield_metrics.json`) instead of using arbitrary code.
`run_approved_campaign` writes `optimization_manifest.json` and `optimization_gate.json`.
The gate requires an aster campaign, completed runs, a Pareto front and an acceptance result.
These are **evidence and visibility** only; enforcement of "must use the optimiser" remains
deferred until the task-mode rules are agreed.

## Native report-image export (no allowlist approval)

`export_report_image` is a fixed, read-only guard tool for the common "copy the
AEDT report image" request:

```python
export_report_image(
    project_path=r"C:\...\P0_...\.aedt",
    design_name="Trunc_10GHz",
    output_dir=r"C:\...\final_figs\aedt_report",
    setup_name="Setup1",
    frequency="12GHz",          # any solved frequency, no schema approval
    plots=["3d", "e", "h"],
    width=1600,
    height=1200,
    port=50175,
)
```

It opens the solved project, creates the requested native reports
(3D Polar Plot / E-plane Radiation Pattern / H-plane Radiation Pattern), exports
JPGs, and removes the reports again. The E/H reports use the dB-radial
`Radiation Pattern` style; `Polar Plot` would use a linear dBi radius and look
like a nearly straight line. It never rebuilds, solves, saves, or changes geometry. Because it
is a fixed tool rather than a builder parameter, changing frequency, image size,
or plot selection does **not** require a new allowlist approval.

## AEDT launch orphan recovery

If `launch_aedt` handshake times out, the AEDT process may still be alive. The wrong response is
to ask the operator to close the window. Recovery order:

1. try `connect_to_aedt(pid=...)` (a retained spawn record may allow this);
2. if it is not attachable, use the guard's own force-close path:
   `disconnect_from_aedt(pid=..., close_desktop=true)` (this is a management operation, not an
   unmanaged attach);
3. call `launch_aedt` again.

The v1.4 launcher also retains the spawn record, tries adoption on retry, and auto-releases an
orphan when adoption fails. The explicit `disconnect_from_aedt` step remains the deterministic
fallback and must be used before asking a human to close anything.
