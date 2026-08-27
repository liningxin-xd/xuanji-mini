# Primary V1 Production Shadow

This phase validates the frozen `primary_v1` path. It does not add analysis
features, parallel queries, cross-task caches, or additional model tools.

For a local macOS run, including the safe token handoff and process-isolation
pitfalls, read [Local Primary V1 Shadow Runbook](local-primary-v1-shadow-runbook.md)
before starting the Host.

## Release Identity

Build from the merged release commit and record both the Git commit and image
digest. Render `deploy/primary-v1/manifests.yaml` with
`scripts/primary_v1_deployment.py`; tagged images and the committed placeholder
fail closed. Keep one `Recreate` replica and one persistent volume mounted at
`/var/lib/xuanji`.

Before building, mount the current internal `taptap-data-analysis` knowledge
base and require the external definition gate to pass:

```bash
TAPTAP_DATA_ANALYSIS_SKILL_ROOT=/absolute/path/to/taptap-data-analysis \
  python scripts/compile_metric_definitions.py --check
```

This command reads the external Skill but must never modify, install, or sync
it.

Provision these credentials as three independent secret resources:

- `xuanji-primary-v1-host-auth`: platform-to-Host bearer token;
- `xuanji-primary-v1-dview-readonly`: DView read-only machine token;
- `xuanji-primary-v1-receipt-auth`: Host-owned receipt secret.

The platform session must expose exactly `xuanji_run_task`,
`xuanji_submit_repair`, and `xuanji_finalize`. Do not expose DView `query` in
that session. Label only approved client pods with
`xuanji.taptap/client=true`.

## Restart Acceptance

Use a fresh task ID and immutable DQC payload. Complete the first
investigation, restart the Host before the second writer pack, and resume with
the same task ID and payload. Verify that completed investigations and root
snapshots issue no repeated queries, pending work continues, task and run sinks
agree, identical finalize retries are accepted, and conflicting retries fail.

## Required Shadows

Run all three shapes with fresh task IDs and real read-only DView evidence:

1. `same-metric`: the APK download-complete absolute, seven-day-relative, and
   three-week rules form one investigation with indexes `[0,1,2]` and eight
   root queries.
2. `same-scope`: APK download complete, failure, and manual-stop metrics form
   three serial investigations sharing one app snapshot and eight total root
   queries.
3. `mixed-scope`: APK download, APK install, and sandbox download remain three
   stable investigations. App and sandbox use two snapshots and sixteen total
   root queries; install analysis date is alert date minus two days.

Export the complete model-visible transcript for each task. On a trusted
operator host with read-only access to the persistent volume, run:

```bash
python scripts/primary_v1_shadow_acceptance.py \
  --data-root /var/lib/xuanji \
  --task-id <fresh-task-id> \
  --scenario same-metric \
  --transcript /private/transcripts/<fresh-task-id>.jsonl
```

Repeat with `same-scope` and `mixed-scope`. The verifier checks task/snapshot
integrity, fixed queues, writer-pack size, stable ordering, scenario query
counts, and transcript leakage. It prints only bounded counts and the downstream
idempotency identity:

```text
task_id + analysis_sha256 + validation_receipt_sha256
```

The trusted acceptance verifier must still inspect
`/var/lib/xuanji/results/tasks/<task-id>/validated-task-result.json`. A
model-mediated daily-push writer must not persist a bare `analysis_preview`; it
may persist the public projection only after verifying the paired
`pipeline_handoff` against the pinned key, immutable task ID, and payload hash.

## Logs And Exit Gate

Retain only the structured `xuanji_operation` fields: `task_id`,
`investigation_id`, `phase`, `duration_ms`, `root_query_count`,
`attribution_query_count`, `root_snapshot_reused`, `writer_pack_bytes`,
`investigation_status`, `overall_status`, and `exception_type`. SQL, rows,
query IDs, receipts, hashes, paths, and credentials are forbidden in logs.

Collect 10 to 20 real tasks or three to five consecutive days. Proceed to a
small canary only when every shadow passes routing, query reuse, fixed-queue,
resume, writer budget, transcript, receipt, and operational-retry checks.
