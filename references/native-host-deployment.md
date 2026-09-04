# Native Primary Host Deployment

Use this service when the existing DView MCP cannot be modified. The service is
an independent FastMCP process below the model/UI boundary. It exposes only:

- `xuanji_run_task`
- `xuanji_submit_repair`
- `xuanji_finalize`

The process is an MCP client of the existing read-only DView `/mcp/query`
endpoint. This is a server-to-server call: DView SQL, rows, query IDs, receipts,
and result hashes remain inside the Host process and its private artifacts. The
model never invokes the nested DView tool.

Process, operation, and query lifecycle logging follows
[Operational Telemetry](operational-telemetry.md). Retain the structured records
in the normal platform log sink and index `error_id` plus `operation_id`.
Unexpected operation failures write raw diagnostic material to the private Host
results volume instead of the platform log sink; do not mirror those bundles to
ordinary logs or expose them through MCP.

## Deployment Contract

Build the repository `Dockerfile` and configure the variables documented in
`env.host.example` through the deployment secret manager. Do not commit a real
token or receipt secret. The service fails closed unless all three independent
credentials are present:

1. a model/platform-to-Host bearer token;
2. a read-only Host-to-DView service token or PAT;
3. a Host-owned receipt secret of at least 32 bytes.

`XUANJI_ANALYSIS_PROFILE` is deployment-owned and accepts only `primary_v1` or
`primary_v2`. The existing production deployment remains explicitly pinned to
`primary_v1`. The separate `deploy/primary-v2/manifests.yaml` template is an
isolated release candidate and does not authorize cluster apply or traffic.
Its full gate is documented in
[Primary V2 Deployment And Shadow](primary-v2-deployment-shadow.md).

Grant the DView machine identity only the registered Android download/install
monitor tables required by the locked query assets. Do not reuse an interactive
user OAuth token. Register the model tool endpoint as `<public-url>/mcp`; do not
also register DView `query` in the isolated shadow session.

Run one service replica and mount one persistent volume at `/var/lib/xuanji`.
The deterministic Runtime currently uses filesystem state and per-process task
locks; multiple replicas or an ephemeral volume make resume identity unsafe.
The volume retains task state, investigation runs, and both authoritative sink
levels:

```text
/var/lib/xuanji/tasks/<task-id>/state.json
/var/lib/xuanji/tasks/<task-id>/root-snapshots/<scope-hash>.json
/var/lib/xuanji/runs/<run-id>/...
/var/lib/xuanji/results/<run-id>/validated-result.json
/var/lib/xuanji/results/tasks/<task-id>/validated-task-result.json
/var/lib/xuanji/results/diagnostics/<error-id>.json
```

Only a trusted pipeline writer or operator may read that directory. Diagnostic
bundles intentionally contain raw payloads, SQL, DView responses, query IDs,
tracebacks, and task/run artifacts with credential values redacted. They must
never be exposed as another model tool or returned by an artifact download
endpoint.
The model-facing fallback uses the signed public projection documented in
[Signed Pipeline Handoff](pipeline-handoff.md); it does not expose this volume.

The Host derives a separate Ed25519 signing key from the receipt secret through
domain-separated HKDF. On a trusted operator host with the receipt environment
already injected, derive the public trust anchor without printing the secret:

```bash
python scripts/derive_pipeline_public_key.py
```

Pin the returned key ID and public key in the daily-push workspace as
`XUANJI_PIPELINE_SIGNING_KEY_ID` and
`XUANJI_PIPELINE_SIGNING_PUBLIC_KEY_B64`. The public key is not a secret, but
changing it changes the accepted Host authority and must be reviewed. Do not
run this derivation by putting the receipt secret on a command line.

The existing production Kubernetes source is
`deploy/primary-v1/manifests.yaml`. It is a fail-closed template: the
placeholder image cannot pass the production gate.
Render it with the exact image digest built from the release commit:

```bash
python scripts/primary_v1_deployment.py render \
  --image registry.example/xuanji-mini@sha256:<64-hex-digest> \
  --host-public-url http://xuanji-primary-v1:8091 \
  --dview-mcp-url https://dview-mcp-public.tapsvc.com/mcp/query \
  --output /private/release/xuanji-primary-v1.yaml
```

Create the three referenced Secret resources separately in the deployment
system: `xuanji-primary-v1-host-auth`, `xuanji-primary-v1-dview-readonly`, and
`xuanji-primary-v1-receipt-auth`. Never render or commit their values. The
Service is `ClusterIP`, and ingress is limited to same-namespace pods labeled
`xuanji.taptap/client=true`. Apply cluster egress controls separately so the
Host identity can reach only the approved DView endpoint and DNS.

## Isolated Shadow Acceptance

CI builds and boots the image with a temporary volume, verifies 401 without the
Host token, lists exactly the three task tools with the token, checks non-root
execution and volume writes, restarts the container, and exercises identical
and conflicting task finalization. This is a deployment smoke, not production
DView evidence.

For shadow acceptance, use a new model Host/session and new task IDs. Register
only the native Xuanji MCP endpoint, then run raw DQC payloads that cover a real
download and a real APK-install investigation. Inspect the complete
model-visible transcript, including tool events. It must contain no DView SQL,
Markdown result table, query ID, `raw_result`, raw receipt, or raw-result hash.
A semantic repair may expose only the bounded repair packet defined by the Host
contract.

Inside the service volume, verify that every investigation retained real query
IDs and result hashes, each writer pack stayed within 12 KB, rule indexes are
covered once, task ordering is stable, and the task sink agrees with the run
sinks and returned receipt hashes. Failed or leaking shadows must not be
resumed or used as evidence. The exact three-scenario procedure and artifact
verifier are documented in
[Primary V1 Production Shadow](primary-v1-production-shadow.md).

## Deployment Troubleshooting And Status

| Symptom | Cause | Minimum check | Correct fix | Never do |
| --- | --- | --- | --- | --- |
| A local LaunchAgent is healthy but no cluster workload exists | Local acceptance and Kubernetes production are different deployment targets | Check for the release image digest, rendered manifest, `kubectl` context, PVC, network policy, and three Secret resources | Report the state as local deployment only; complete every production prerequisite before applying | Call a localhost install "production deployed" |
| The committed manifest cannot pass the production gate | Its image is intentionally a placeholder | Run the deployment renderer with an image digest built from the reviewed release commit | Build, scan, and pin `registry/...@sha256:<digest>` | Deploy a mutable tag or bypass the placeholder check |
| A new pod starts but existing handoffs fail verification | The Host receipt authority changed while daily-push still pins the prior public anchor | Compare deployment key IDs and derive the public anchor inside the exact new Host secret context | Roll out the matching public anchor as a reviewed writer configuration change with the Host release | Generate an unrelated secret or copy a private signing value into daily-push |
| Restart/resume identity changes across pods | Runtime state is on ephemeral storage or more than one replica is active | Verify one replica and the mounted `/var/lib/xuanji` PVC | Restore the single-replica, persistent-volume contract before accepting tasks | Resume from another replica or reconstruct state from model-visible output |
| Container smoke passes but production readiness is still unknown | The smoke proves packaging and boundary behavior, not DView authorization or operational policy | Check build tooling, cluster access, Secret provisioning, ingress/egress policy, PVC, and shadow evidence separately | Record each prerequisite and its evidence before declaring production ready | Treat a local or synthetic smoke as real DView or production evidence |

Do not restart or redeploy a healthy Host for documentation-only changes. A
release status must name its actual boundary: local LaunchAgent, isolated
container shadow, staged Kubernetes workload, or production workload.
