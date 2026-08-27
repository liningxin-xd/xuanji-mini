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
investigation index, immutable run identities, writer patch hashes, and an
integrity hash. It remains private to the Host.

## Investigation Formation

The normalizer preserves zero-based rule indexes and unknown input fields. The
route resolver performs exact normalized matching against
`contracts/dqc-routes.yaml`; it never performs fuzzy metric or game-type
inference.

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

The model receives a recursively redacted preview and compact receipt hashes.
SQL, raw rows, query IDs, raw-result hashes, and private receipts stay in Host
state and machine-only sinks.

## Resume

Calling `xuanji_run_task` again with the identical payload returns the pending
repair/writer action or the completed preview. Identical finalize retries are
idempotent. A changed payload, investigation identity, run identity, writer
patch, state hash, or sink payload is rejected rather than overwritten.
