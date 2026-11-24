# Experimental builder lane

## Why

The production builder must stay a stable, approved artifact. Debugging a new
feed/port/geometry mode by editing it creates approval churn: every byte change
is a new SHA-256 and therefore a new operator approval.

## Recommended layout

```text
p0_model          production builder, delivery evidence only
p0_model_dev      experimental builder, non-production output
```

`p0_model_dev` is a separate allowlist entry with its own approved script and
parameter schema. It can be changed during an experiment, but:

- its outputs are tagged `experimental`;
- its campaigns cannot be used as delivery evidence;
- its results must be re-run through the production builder before acceptance;
- a final promotion is one `builder_change` request for `p0_model`.

## Better: parameterize the experiment

Before creating a dev builder, add the complete experimental knob set in one
schema request:

```text
feed
port_type
port_inset_mm
mc_steps_spec
ff_step_deg
```

Then iterate values, not script bytes. Value changes do not require approval.

## Operator settings

For a production builder that is drifting into development:

```json
"p0_model": {
  "approval_churn_warn_after": 3,
  "approval_churn_limit": 5
}
```

The next request after the hard cap is refused with a consolidation message.

## Promotion checklist

- [ ] dev geometry validated;
- [ ] one real solve completed and exported;
- [ ] production defaults bit-identical or explicitly changed;
- [ ] schema contains all required parameters;
- [ ] one consolidated promotion request for `p0_model`;
- [ ] production evidence replay rerun after promotion.
