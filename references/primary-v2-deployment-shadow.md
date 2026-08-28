# Primary V2 Deployment And Shadow

This runbook releases the existing `primary_v2` Runtime as an isolated Host.
It does not add analysis modules, change the daily-push request or writer
contract, apply Kubernetes resources, route production traffic, render alert
cards, or send Feishu messages.

## Release Boundary

`primary_v1` and `primary_v2` may use the same reviewed image digest. Their
deployment identities remain separate:

| Boundary | primary_v1 | primary_v2 |
| --- | --- | --- |
| Kubernetes prefix | `xuanji-primary-v1` | `xuanji-primary-v2` |
| analysis profile | `primary_v1` | `primary_v2` |
| local endpoint | `127.0.0.1:8091` | `127.0.0.1:8092` |
| PVC | `xuanji-primary-v1-data` | `xuanji-primary-v2-data` |
| Host auth Secret | `xuanji-primary-v1-host-auth` | `xuanji-primary-v2-host-auth` |
| DView Secret | `xuanji-primary-v1-dview-readonly` | `xuanji-primary-v2-dview-readonly` |
| receipt Secret object | `xuanji-primary-v1-receipt-auth` | `xuanji-primary-v2-receipt-auth` |

The v2 receipt Secret object must reference the same current secret-manager
version and key ID as v1. daily-push pins one public trust anchor, so a new
receipt authority requires a separate downstream key-rotation design. The v2
Host bearer token, endpoint, and PVC must never be shared with v1. Prefer an
independent read-only DView machine identity; record any time-bounded reuse as
an exception.

The committed NetworkPolicy restricts ingress to clients labeled
`xuanji.taptap/client-profile=primary-v2`. It does not implement egress
restriction. Record the actual CNI, service-mesh, or platform egress policy
before claiming a staged or production deployment is isolated.

## Preflight

From the service root, fresh-fetch and check the fixed `main` checkouts for
`taptap-data-alert-workflow`, `grafana-lark-daily-push-poc`, and `xuanji-mini`.
All three must be clean and have `HEAD...origin/main` divergence `0 0`. Record
their commits. Do not reset, stash, delete files, or switch to another checkout
to pass this gate.

Record, without displaying credential values:

- deployment target, Kubernetes context, namespace, registry, storage class,
  and PVC retention/backup owner;
- the three Secret resource versions and access principals;
- DView read-only dataset scope;
- ingress and egress enforcement evidence;
- MCP endpoint registration, sticky task router owner, configuration location,
  change audit, and rollback entry point;
- v1/v2 latency and cost thresholds, maximum concurrency, and outer task
  deadline.

Unknown platform values do not block local development. They do block
Kubernetes apply and production canary.

## Deployment Contract

Verify both committed templates:

```bash
python scripts/primary_v1_deployment.py verify --allow-template-image
python scripts/primary_v2_deployment.py verify --allow-template-image
```

Render v2 only with the reviewed immutable image digest:

```bash
python scripts/primary_v2_deployment.py render \
  --image registry.example/xuanji-mini@sha256:<64-hex-digest> \
  --host-public-url http://xuanji-primary-v2:8091 \
  --dview-mcp-url https://dview-mcp-public.tapsvc.com/mcp/query \
  --output /private/release/xuanji-primary-v2.yaml
```

The verifier rejects mutable or placeholder images, cross-profile manifests,
unexpected resources, Secret documents, shared v1 names, profile drift,
multiple replicas, ephemeral data, broad client ingress, or weakened container
security. It does not prove that the referenced Secret objects, PVC,
machine identity, or platform network controls exist.

## Container Acceptance

The default smoke remains the v1 compatibility path. Run both profiles against
the same release source:

```bash
python tests/container_smoke.py --analysis-profile primary_v1
python tests/container_smoke.py --analysis-profile primary_v2
```

The v2 smoke binds localhost port 8092 and uses a fresh container volume. It
checks unauthenticated `401`, the exact three-tool surface, UID 10001, cwd
`/app`, writable persistent data, restart/resume, task and finalize conflicts,
cross-profile resume rejection, stable signed handoff, and distinct
root/primary/post-primary query counts. Its deterministic DView stub executes a
full seven-step primary queue and a completed six-step post-primary plan with
zero selected post-primary queries. Unit fixtures separately cover every
post-primary query module, threshold, failure, repair, and total budget branch.

Do not use the production v1 PVC to test cross-profile rejection. The smoke
uses only its ephemeral fixture volume.

## Private Artifact Acceptance

Use a fresh mode-`0700` evidence directory and files with mode `0600`. For each
completed real v2 task, retain the private Host artifacts and model transcript,
then run:

```bash
python scripts/primary_v2_shadow_acceptance.py \
  --data-root /private/v2-shadow/data \
  --task-id <fresh-task-id> \
  --scenario app-download \
  --transcript /private/v2-shadow/transcripts/<task-id>.jsonl
```

Supported scenarios are `same-metric`, `app-download`, `sandbox-download`,
`apk-install`, `restart-resume`, and `paired-v2`. Normal release shadows must
not use `--allow-repair`. That flag exists only for a deterministic synthetic
fixture which proves the two-repair bound.

The verifier binds `primary_v2` and every current Runtime contract hash,
fixed primary and post-primary order, query and repair attempts, query identity
uniqueness, root/run/task sinks, receipt summaries, rule-index coverage, writer
budget, and transcript redaction. It reports only bounded counts, status, and
the task-level idempotency hashes. Do not publish SQL, rows, query IDs, complete
receipts, private hashes, paths, or credentials.

## Daily-Push Boundary

Before a real DView shadow, run a no-DView handoff smoke against the actual v2
Host:

1. Create a fresh schema-v3 request outside an alert batch with a new
   batch-shaped `task_id`.
2. Submit one unregistered synthetic rule. It must complete as bounded
   unsupported or `insufficient_definition` without a DView query.
3. Preserve only the `analysis_preview` and `pipeline_handoff` returned by that
   same `task_complete`, beside the immutable request ID.
4. Use the formal daily-push launcher and workspace `.env` with
   `write-alert-analysis`.
5. Require schema v4, one success, zero failures, and no handoff degradation.
6. Repeat preview, task, payload, and signature tamper cases and require
   `unverified_result`.

Stop after the writer. Do not render or send. A writer success proves signer,
task, payload, and preview binding; it does not prove the deployment profile.
The private acceptance evidence above proves `primary_v2`.

## Real Read-Only Shadow

Use fresh task IDs and a fresh v2 data root. Register only the v2 Xuanji MCP
endpoint in the model session. Do not expose DView `query`. Run the required
same-metric, app-download, sandbox-download, APK-install, restart/resume, and
paired v1/v2 shapes. Real data proves only the steps selected by actual frozen
evidence and the configured upper bounds. Deterministic fixtures remain the
hard evidence for threshold boundaries, three-game cap, simultaneous
error-code and overlap selection, query failure, repair, and tamper behavior.

For paired comparison, use different fresh v1 and v2 task IDs for the same raw
case. Compare bounded status, candidate shape, root/primary/post-primary query
counts, duration, and evidence limits. Never move a task or PVC between
profiles.

Collect 10 to 20 real v2 tasks or three to five consecutive business days.
Fill latency p50/p95/max, query cost, DView failures, Host ToolError, repair,
and writer degradation rates before canary review. The recommended initial
latency gate is v2 p95 no more than twice the matched v1 p95.

## Canary And Rollback Gate

Canary requires a real external router which freezes endpoint assignment
before the first task call and keeps every run, repair, finalize, retry, and
resume on the same Host/PVC. There is no repository-owned production router.
Do not claim canary readiness until its owner, configuration, change audit,
task-sticky behavior, stop action, and rollback exercise are recorded.

Stop new v2 assignments immediately for authentication or privacy leakage,
profile/contract/hash mismatch, cross-Host resume, query budget or replay,
registered route regression, writer `unverified_result`, or PVC/receipt
integrity failure. Rollback changes routing only for tasks which have not
started. Keep the v2 Host, PVC, and receipt authority stable while started v2
tasks finish or produce typed failures. Never hand v2 task state to v1, clear
the PVC, rotate the signer, or replay old tasks as rollback.

Kubernetes apply, production routing, scale-down, and cleanup are separate
reviewed actions. This runbook does not authorize them.
