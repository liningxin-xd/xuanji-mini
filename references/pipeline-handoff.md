# Signed Pipeline Handoff

The private task sink remains the complete audit artifact. A model-facing
pipeline receives only a redacted analysis projection plus an Ed25519
attestation derived by the Host after the private artifact has passed these
checks:

- task artifact and receipt identities equal the requested `task_id`;
- `status=valid` and the receipt payload hash is present;
- the complete analysis matches `analysis_sha256`;
- the receipt without its self-hash matches `validation_receipt_sha256`;
- overall status and investigation count agree with the complete analysis.

The Host then recursively removes private query, SQL, raw-result, receipt, and
snapshot fields using the same projection function as `analysis_preview`. The
normal `task_complete` response adds:

```json
{
  "analysis_preview": {"schema_version": 5, "overall_status": "completed", "investigations": []},
  "pipeline_handoff": {
    "schema_version": 1,
    "provider": "xuanji-mini",
    "task_id": "daily-push-<batch-id>-<request-id>",
    "payload_sha256": "<canonical DQC payload sha256>",
    "analysis_preview_sha256": "<canonical public projection sha256>",
    "validation_receipt_sha256": "<private task receipt self-hash>",
    "signing_key_id": "<receipt-key-id>.pipeline-handoff-ed25519-v1",
    "signature": "<unpadded base64url Ed25519 signature>"
  }
}
```

The signature covers canonical UTF-8 JSON of every `pipeline_handoff` field
except `signature`: sorted object keys, no insignificant whitespace, Unicode
preserved, and non-finite numbers rejected. The signing seed is derived from
the Host-owned receipt secret with HKDF-SHA256, salt
`xuanji-pipeline-handoff-v1`, info `ed25519-signing-key`, and length 32. The
receipt secret and derived private key never leave the Host.

The upstream request supplies the exact `task_id`; the model must not generate
or rewrite it. For a successful daily-push result, return exactly the immutable
`request_id` and `task_complete_json`. The latter is the unchanged unique non-empty
`TextContent.text` from the same completed MCP CallToolResult. Python parses it once
and extracts the paired `analysis_preview` and `pipeline_handoff`. The writer pins the public key, verifies the signature,
recomputes both public hashes, and requires the handoff task and payload to
match the immutable request before using the projection.

Current mcp==1.26.0 FastMCP generates that text from the Python dict before JavaScript can alter numeric
representations such as 2.0, 4.0 and 0.0. It is not an HTTP/SSE wire capture. Preserve structuredContent only for
action and continuation identity gates; never serialize it into writer input. Capture both representations in one
awaited call, from whichever of run_task/submit_repair/finalize actually returns task_complete. The workflow relay
helper binds them to one captured handle. Missing/extra/malformed text blocks or mixed-call representations yield
contract_mismatch; isError=true yields invocation_failed or a more specific invocation error.

Do not parse and re-stringify the inner text. Only JSON.stringify the outer result array. Use structured file APIs
or controlled stdin, a mode-0600 file in the system temporary directory outside the batch, and finally cleanup on
writer success or failure. Never interpolate the response into shell commands or log its complete contents.

The Python consumer requires a non-empty string containing strict JSON without NaN/Infinity, an object root,
action=task_complete, a non-empty string task_id, and object preview/handoff. Malformed envelopes or required
fields yield contract_mismatch / analysis_requests_degraded. A well-formed top-level task_id that differs from the
immutable request yields unverified_result / analysis_handoff_unverified. Once handoff is an object, all existing
field, signer, signature, task and hash verification failures remain unverified_result. Verified invalid v5/v2
content yields contract_mismatch. Additional top-level overall_status, compact validation_receipt, audit_detail and
future fields are ignored and never persisted; only the signed preview supplies facts. Failures are per request.

Schema-v3 success input is a breaking consumer envelope change: old object and mixed envelopes are explicitly
contract_mismatch with no dual-format fallback. Coordinate the consumer and all three repositories' instructions
before starting new batches. Handoff v1, current Python hashes, unsigned signing bytes, key derivation, Ed25519,
request v3, analysis v5, public-facts v2 and current alert-v5.6 are unaffected by raw-text ingestion. No JCS, dependency or Host deployment change
is required. Existing batches stay byte-identical; never reconstruct missing raw text, migrate, re-sign or resend.
Rollback must restore the consumer and all instructions together. Arbitrary manual payload 2.0/2 compatibility is
outside this fix; input tests use the current DQC parser's production-shaped payload.

Rerun the FastMCP/JavaScript/Python regression on SDK upgrades; a failed text-preservation test must not trigger a
structuredContent fallback. After merging all three repositories to clean synchronized main, run a fresh
batch-shaped task with an unregistered synthetic rule through the running primary_v2 Host, formal launcher and
workspace public anchor. Require one writer success, zero failures and no handoff degradation, then stop without
DView business queries, render or send. Feature worktree offline tests do not replace this deployment smoke.

`analysis_preview` without a valid handoff remains unverified. A missing key,
unknown signer, changed preview, task or payload mismatch, malformed signature,
or failed verification becomes `unverified_result`; it never falls back to an
unsigned success. The complete task sink is not exposed as a model tool or an
artifact download endpoint, and the signed projection contains no private
query evidence.

The current public projection is analysis schema v5. Each investigation carries `public_facts` with the complete
user-safe root metric, typed measures, frozen findings, parent links, ordered steps, background signals, calibration
results, audit codes, structured recommendations, channel-neutral narrative, and typed narrative fallback state.
These fields are covered by `analysis_preview_sha256` and therefore by the handoff signature. The projection is not
allowed to reduce machine facts to the Writer's bounded context or to omit a background/calibration fact because the
Writer did not mention it. See [Public Analysis v5](public-analysis-v5.md).

## Troubleshooting

| Symptom | Cause | Minimum check | Correct fix | Never do |
| --- | --- | --- | --- | --- |
| Receipt validation succeeds but `task_complete` has no usable pipeline result | The caller retained objects instead of raw text, or used the old envelope | Confirm the one current CallToolResult contains both representations and a unique non-empty text block | Retain TextContent.text as task_complete_json and submit it with request_id | Rebuild objects, mix calls, export the private task sink, or treat bare preview as authoritative |
| Preview hash fails after JavaScript serialization | Integer-valued floats were re-encoded as integers | Run the synthetic raw-text boundary regression | Preserve the raw text string through outer JSON.stringify and Python parsing | Introduce JCS, guess numeric types, change hash/signature rules, or assume a public-key failure |
| The writer reports an unknown signer or invalid signature | Its pinned public anchor does not represent the exact receipt authority used by the running Host | Compare key IDs and deployment identities without printing secrets; derive the public key inside the credential-bearing Host context | Pin the derived key ID/public key pair in the writer workspace and rerun with a new task | Mint a replacement receipt secret just to make verification pass |
| Signature verifies in isolation but the writer returns `unverified_result` | Task ID, payload hash, or preview hash no longer matches the immutable request | Compare the current request identity with the handoff fields and use the unmodified preview from the same response | Reinvoke the current batch task or submit a typed failure for that request | Rewrite `task_id`, edit the preview, recompute a model-side handoff, or reuse an old signature |
| Identical alert content has the same `request_id` in another batch | Content identity is intentionally stable; replay protection is the batch-bound `task_id` | Confirm the upstream task ID includes the current batch ID | Preserve the exact upstream task ID through the Host and writer | Use `request_id` alone as invocation or replay identity |
| An unsigned schema-v2 success is available | It is a legacy-read-only artifact | Check the immutable request schema version | Read it only for explicit legacy audit; use schema v3 plus signed handoff for new work | Convert or copy it into a new batch as a current success |

An object handoff with missing or invalid authority fields fails closed as `unverified_result`; a missing/non-object
handoff or malformed success envelope is `contract_mismatch`. This is a per-request enrichment failure;
the downstream pipeline must record its degradation explicitly and must never silently reinterpret it as unsigned
success.
