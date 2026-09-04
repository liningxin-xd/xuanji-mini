# Registered Alert Task Coordinator

The task coordinator is the production entry point for one raw DataWorks DQC
payload. It wraps the frozen single-investigation Runtime without changing its
queue, contribution math, candidate thresholds, SQL repair protocol, or locked
query assets.

## Identity and State

A caller supplies a unique `task_id` and the complete `dqc_payload` to
`xuanji_run_task`. The Host serializes the payload as canonical JSON and freezes
its SHA-256. Reusing the task ID with different content is rejected.

State is stored atomically with mode `0600` at:

```text
/var/lib/xuanji/tasks/<task-id>/state.json
```

The state contains the normalized alert, ordered investigations, current
investigation index, immutable run identities, compiled metric-definition
bundle hash, writer patch hashes, and an integrity hash. It remains private to
the Host. Resume fails closed if the current definition bundle differs.

## Investigation Formation

The normalizer preserves zero-based rule indexes and unknown input fields. The
route resolver first checks observed names, then deterministically resolves the
registered `object_table + metric_hint + rule_kind` binding in
`contracts/dqc-routes.yaml`. `canonical_metric` must resolve through the
compiled metric-definition lock and the selected execution plan. Title,
operator, field, and threshold drift is audit metadata; the resolver never
uses an LLM, fuzzy similarity, or inferred game type.

Registered rules are grouped by project, table, partition, canonical metric,
chain, and game type. Each rule index belongs to exactly one ordered
investigation. An unmatched rule becomes its own `insufficient_definition`
investigation instead of being dropped.

## Root Preflight

Before a full queue exists, the Host derives `analysis_dt`, executes the locked
registered root QuerySpec, verifies one-row scope, recomputes the materialized
rate from its registered numerator and denominator, and reconciles four decimal
places. It then loads the adjacent previous day and the preceding seven-day
pooled baseline.

The preflight applies direction, range, sample, and newness gates. A continuing
absolute-threshold anomaly with no 5bp of new adverse change returns a compact
writer pack without starting attribution. Otherwise it freezes
`canonical_root_metric` and creates the internal full-queue run. Candidate
families must rehook that frozen root.

The first investigation for one task-level root scope executes all eight days
and atomically stores a private snapshot at:

```text
/var/lib/xuanji/tasks/<task-id>/root-snapshots/<scope-hash>.json
```

The scope hash binds the locked root QuerySpec hash, object table, game type,
and alert date. Later investigations in the same scope revalidate and reuse the
complete snapshot. App and sandbox scopes remain separate. Partial snapshots
are never stored. Snapshot integrity, per-day result hashes, and all eight
result contracts are checked before reuse; corruption is an operational error,
not a `query_failed` investigation.

## Three-Tool Protocol

Only these tools cross the model boundary:

```text
xuanji_run_task(task_id, dqc_payload)
xuanji_submit_repair(task_id, investigation_id, run_id, repair fields...)
xuanji_finalize(task_id, investigation_id, writer_patch)
```

`xuanji_run_task` and `xuanji_submit_repair` return one of
`write_conclusion`, `repair_required`, or `task_complete`. At most one writer
pack is visible in a response, and it cannot exceed 12 KB.

`xuanji_finalize` never accepts `analysis_context`. Project, table, partition,
rule indexes, rule names, metric, dates, and root facts come from frozen machine
state. The model supplies only summary, candidate text, evidence limits, and a
recommended action for the current investigation.

## Assembly and Sink

Investigations execute serially. A blocked investigation is written as a typed
result and does not prevent later investigations from running. Semantic repair
pauses only its current investigation.

After every investigation has a legal result, the assembler verifies exact
rule-index coverage and preserves investigation order. It computes task status:

```text
all investigations successful -> completed
successful and blocked results -> partial
no successful investigation   -> failed
```

The complete task analysis and receipt are written atomically to:

```text
/var/lib/xuanji/results/tasks/<task-id>/validated-task-result.json
```

The model receives a recursively redacted preview, compact receipt hashes, and
a signed public pipeline handoff derived from the reloaded task sink. The
handoff binds the exact task ID, canonical payload hash, public projection hash,
and private receipt self-hash. It is verified downstream with a pinned Ed25519
public key; the preview alone remains unverified.
SQL, raw rows, query IDs, raw-result hashes, and private receipts stay in Host
state and machine-only sinks. The authoritative task receipt binds the compiled
definition bundle hash and deduplicated root snapshot hashes; those fields are
not added to the model-visible response.

## Resume

Calling `xuanji_run_task` again with the identical payload returns the pending
repair/writer action or the completed preview. Identical finalize retries are
idempotent. A changed payload, investigation identity, run identity, writer
patch, state hash, or sink payload is rejected rather than overwritten.

Classified DView failures continue through the typed analytical states. An
unexpected Python exception, state corruption, or internal Host contract error
does not create an analytical result. The outer MCP boundary returns a generic
ToolError with an opaque `error_id` and leaves the task available for an
identical retry. Private structured telemetry records the safe task/query stage,
counts, timings, and bounded exception class tree under the same operation; see
[Operational Telemetry](operational-telemetry.md). Normal logs never record
exception messages or private query evidence. A separate mode-`0600` failure
bundle retains the original task/query material and traceback for trusted local
diagnosis while recursively redacting credential values.
