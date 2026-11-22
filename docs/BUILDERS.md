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
