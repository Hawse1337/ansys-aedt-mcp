# Builder approval policy (v1.5)

This document defines when an approval is legitimate and how the guard/agent must
behave. The goal is to stop using operator approval as a debugging or polling loop.

## 1. Only builder capability changes need approval

Approval is for:

- new builder code / geometry / port behaviour;
- new builder parameters or enum values;
- a new fixed guard capability.

Approval is **not** for:

- choosing a parameter value already allowed by the schema;
- changing frequency, image size, plot selection, cut or report name;
- exporting an existing saved report (`export_report_image(..., report_name=...)`);
- re-running an approved builder;
- recovering an orphan AEDT session (use `disconnect_from_aedt(close_desktop=true)`).

The following fixed tools are zero-approval:

- `export_report_image` (3D / E / H; `report_name` for saved reports);
- `export_farfield_report` (CSV cuts);
- `screenshot` (model view);
- `run_approved_builder` with parameters already in the schema.

## 2. Server-side preflight is mandatory

`request_builder_approval` now runs `aedt_guard.approval_policy.preflight_candidate`
before writing a request. It rejects the request with `status=preflight_failed` when:

- the candidate is missing or does not compile;
- the candidate has no `run` entrypoint;
- a `run(...)` argument is neither in the existing allowlist nor declared in
  `new_params` / `new_enum_values` in the same request;
- a declared parameter has an invalid schema shape.

This prevents the old failure mode “hash approved, but parameter table missing”.
The response contains a `preflight` report with `checks`, `errors`, `warnings`
and a static `risk_level` (`low` / `medium` / `high`).

Request template:

```python
request_builder_approval(
    builder_id="p0_model",
    candidate_path=r"C:\...\scripts\candidates\p0_model_20261123.py",
    reason="implement curve_k profile exponent",
    change_summary=["implement curve_k", "no new parameters"],
    new_params=[],                 # only if run() gained parameters
    new_enum_values={},
    tested=["py_compile", "dry-run", "end-to-end temporary run"],
)
```

Do not submit a request until the candidate has been run end-to-end in a
temporary directory. `tested` empty is reported as a warning; repeated incomplete
requests are discouraged.

## 3. Wait once; do not poll in a loop

Use the single-call wait tool instead of querying the pending file every step:

```python
wait_for_builder_approval(request_id, timeout_s=1800)
```

Returns `approved` / `denied` / `superseded` / `pending` (with `timed_out=true`).
On timeout, park the task and continue independent work; do not immediately call
it again. When approved, retry `run_approved_builder` (or check the allowlist
hash directly).

## 4. One active request per builder

`request_builder_approval` auto-supersedes older pending requests for the same
builder. The old request gets a `superseded` tombstone and the response lists
`superseded_requests`. If the allowlist already matches the candidate and no
schema additions are requested, the tool returns `status=approved` with
`already_approved=true` instead of asking the operator again.

## 5. Optional approval budget

An operator can cap approval churn per builder in `builders.local.json`:

```json
"p0_model": {
  "approval_budget_per_day": 4
}
```

When the cap is reached, `request_builder_approval` returns an error telling the
agent to **consolidate the candidate and all required parameters/test evidence
into one request**. `0` or absent means unlimited.

## 6. Pending state machine for agents

```text
running
  -> need builder capability change
  -> request_builder_approval (preflight must pass)
  -> wait_for_builder_approval once
       approved  -> retry run_approved_builder
       denied    -> stop this line, report
       superseded/new request id -> follow the new request
       timeout   -> parked; continue non-AEDT work; do not hot-loop
```

Do not use goal auto-rounds as an approval poller. A pending approval is a
checkpoint, not a reason to consume rounds or ask the operator to close windows.

## 7. Operator-visible rule

The GUI shows pending requests, source, preflight risk and campaign gate. The
operator should not have to:
- manually add a schema entry after approving a hash;
- approve the same builder capability repeatedly because the agent forgot a
  parameter;
- close AEDT windows to break a launch deadlock.
