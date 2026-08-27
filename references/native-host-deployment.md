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

## Deployment Contract

Build the repository `Dockerfile` and configure the variables documented in
`env.host.example` through the deployment secret manager. Do not commit a real
token or receipt secret. The service fails closed unless all three independent
credentials are present:

1. a model/platform-to-Host bearer token;
2. a read-only Host-to-DView service token or PAT;
3. a Host-owned receipt secret of at least 32 bytes.

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
```

Only a trusted pipeline writer may read that directory. It must never be
exposed as another model tool or returned by an artifact download endpoint.

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
resumed or used as evidence.
