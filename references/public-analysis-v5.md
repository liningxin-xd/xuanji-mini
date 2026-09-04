# Public Analysis v5

Xuanji emits analysis schema v5 for every newly finalized task. The private task sink remains the complete audit
artifact. `analysis_preview` is its user-safe projection and is authoritative to a pipeline only when paired unchanged
with the signed `pipeline_handoff` returned by the same `task_complete` response.

## Ownership

The Host owns route identity, status, dates, values, units, candidates, thresholds, parent relationships, steps,
background facts, calibrations, recommendation identity, and evidence strength. The Writer may provide only four
channel-neutral narrative fields from the current `writer_pack`: `summary`, `finding_texts`, `evidence_limits`, and
`recommended_action`. It cannot submit machine fields or remove a frozen fact by omitting it from prose.

The public root retains the existing request-bound audit identity and adds an explicit version:

```text
schema_version = 5
source
project
table
partition
overall_status
investigations[]
```

Each investigation retains `status`, non-overlapping `rule_indexes`, registered metric/date and request-bound rule
identity, legacy narrative mirrors where applicable, and one required `public_facts` v1 object. Existing anomaly,
blocked, unsupported, and completed investigations all use the same v5 envelope.

## Public Facts v1

`public_facts` has exactly these required fields and optional `anomaly_context`:

```text
schema_version = 1
metric
steps[]
findings[]
background_signals[]
calibration_results[]
recommendations[]
audit_codes[]
user_narrative
anomaly_context?
```

All IDs are stable, non-empty, and unique within their namespaces. All references resolve inside the same
investigation. All numbers are finite. Unknown fields fail validation rather than being silently retained.

### Metric and measures

`metric` contains `metric_id=root_metric`, `display_name`, `polarity`, `baseline_label`,
`baseline_window_days`, and either all or none of `current`, `baseline`, and `change`. A blocked result may omit the
three measures when the root metric could not be established.

Every measure contains:

```text
measure_id
semantic_type
value
unit
polarity
direction
comparable_group
additive
display_precision
denominator?
```

Current and baseline values share unit, polarity, denominator, and comparable group. Their semantic types are
`metric_value` and `baseline_value`; the root change is `absolute_change` or `relative_change`. Finding change uses
`absolute_change`, and finding impact uses `adverse_impact` with `direction=adverse`. Every finding measure polarity
matches the root metric. Primary adverse impacts must share semantic type, unit, polarity, denominator, additivity,
and comparable group so downstream priority ordering is meaningful. Ordering always uses the unrounded `value`;
`display_precision` affects presentation only.

Units include `ratio`, `pp`, `bp`, `count`, `duration`, `bytes`, and registered domain units. A consumer must format
from the declared unit and must not infer it from the metric name. Parent and child impacts are independent measures
and cannot be added unless a future schema explicitly provides compatible additive semantics.

### Findings and objects

Every finding contains:

```text
finding_id
candidate_id
level = primary | secondary
host_order
dimension = {id, display_name}
object = {object_ref, value, display_name}
lifecycle
current
baseline
change
adverse_impact
narrative_text
parent_finding_id?
parent_object_ref?
```

Primary findings have no parent fields. A secondary finding has exactly one primary parent, and
`parent_object_ref` equals that parent's object reference. All candidates from the frozen Host state are public when
they pass the existing user-safety boundary. The Writer pack's per-family three-candidate cap does not cap this array.

### Steps

Steps are contiguous and ordered from one. Every step contains `step_id`, `display_name`, `ordinal`, `status`,
`result`, and `measure_ids`. Optional fields are `checked_count`, `candidate_count`, `reason`, `finding_ids`, and
`signal_ids`. Public step status is one of:

```text
signal_found
no_signal
skipped_by_policy
failed
blocked
```

Every public finding and background signal is referenced by at least one real step. A failed or skipped step remains
visible with its typed reason; it cannot be rewritten as a zero or no-signal result.

### Background and calibration

Every background signal contains `signal_id`, `bound_finding_id`, the same bound `object`, structured
`event_type`, canonical ISO `event_at`, structured `temporal_relation`, `evidence_level`, `source_type`, and positive
`evidence_priority`. Direct observations and registered dates remain distinct. Background signals express timing and
investigation relevance, never causal confirmation.

Every calibration contains `calibration_id`, `calibration_type`, `bound_finding_ids`, `operation`, `direction`,
`measures`, `evidence_boundary`, and optional `object_ref` or user-safe structured `details`. Current calibration
types include counterfactual removal, breadth, error-code, and cross-dimension overlap results. Counterfactuals express
arithmetic explanatory power only. `details` can contain nested JSON values but never NaN, Infinity, or private
execution identity.

### Recommendations and narrative

Every structured recommendation contains:

```text
recommendation_id
bound_finding_ids[]
bound_object_refs[]
action_type
scope = {object_ref, display_name}
observation_goal
display_text
```

The Host/FinalAssembler owns recommendation ID, bindings, action type, scope, and observation goal. The Writer
supplies only channel-neutral `display_text` through `recommended_action`; a consumer must not parse free text to
invent recommendation identity or scope.

`user_narrative` contains:

```text
schema_version = 1
summary
finding_texts
evidence_limits[]
recommended_action
fallback_status = not_used | partial | used
fallback_reason?
fallback_candidate_ids?
```

`finding_texts` exactly covers every public `candidate_id` and equals each finding's `narrative_text`.
`recommended_action` equals every current recommendation `display_text`. `not_used` has no fallback metadata;
`partial` names the non-empty set of candidates whose text was deterministically filled; `used` records why the full
narrative was replaced.

All narrative must remain independently readable and channel-neutral. It must not mention CardKit, main/reply cards,
threads, pagination, card budgets, destination, delivery, or recovery state. It cannot add or alter objects, numbers,
dates, statuses, candidates, or causal strength.

## Deterministic Assembly and Validation

`FinalAssembler` builds machine facts from the frozen runtime state and copies Writer content only into permitted
narrative slots. Missing finding copy gets a per-finding deterministic fallback. Channel-specific or prohibited
generic Writer copy triggers a deterministic full narrative fallback. Fallback is part of the public result and is
covered by the signed handoff.

`FinalValidator` rebuilds the expected public machine projection from the same frozen state and requires exact
equality. The comparison removes only narrative text fields; it does not remove measures, findings, steps, background,
calibration, recommendation identity, or audit codes. It separately validates full narrative coverage, fallback
metadata, channel neutrality, and legacy mirror consistency before the task can reach `task_complete`.

The signed projection recursively removes SQL, raw rows, query IDs, receipts, hashes, state/snapshot paths, secrets,
and other private execution evidence. No consumer may reconstruct a missing field from those private artifacts or
substitute an old task result.
