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
  "analysis_preview": {"overall_status": "completed", "investigations": []},
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
or rewrite it. For a successful daily-push result, copy only the immutable
`request_id`, the current `analysis_preview`, and the current
`pipeline_handoff`. The writer pins the public key, verifies the signature,
recomputes both public hashes, and requires the handoff task and payload to
match the immutable request before using the projection.

`analysis_preview` without a valid handoff remains unverified. A missing key,
unknown signer, changed preview, task or payload mismatch, malformed signature,
or failed verification becomes `unverified_result`; it never falls back to an
unsigned success. The complete task sink is not exposed as a model tool or an
artifact download endpoint, and the signed projection contains no private
query evidence.

## Troubleshooting

| Symptom | Cause | Minimum check | Correct fix | Never do |
| --- | --- | --- | --- | --- |
| Receipt validation succeeds but `task_complete` has no usable pipeline result | The running Host predates signed handoff support, or the caller retained only the preview | Confirm the one current response contains both `analysis_preview` and `pipeline_handoff` | Deploy the current clean release and repeat with a new task ID, retaining the pair unchanged | Read or export the private task sink, or treat the preview as authoritative |
| The writer reports an unknown signer or invalid signature | Its pinned public anchor does not represent the exact receipt authority used by the running Host | Compare key IDs and deployment identities without printing secrets; derive the public key inside the credential-bearing Host context | Pin the derived key ID/public key pair in the writer workspace and rerun with a new task | Mint a replacement receipt secret just to make verification pass |
| Signature verifies in isolation but the writer returns `unverified_result` | Task ID, payload hash, or preview hash no longer matches the immutable request | Compare the current request identity with the handoff fields and use the unmodified preview from the same response | Reinvoke the current batch task or submit a typed failure for that request | Rewrite `task_id`, edit the preview, recompute a model-side handoff, or reuse an old signature |
| Identical alert content has the same `request_id` in another batch | Content identity is intentionally stable; replay protection is the batch-bound `task_id` | Confirm the upstream task ID includes the current batch ID | Preserve the exact upstream task ID through the Host and writer | Use `request_id` alone as invocation or replay identity |
| An unsigned schema-v2 success is available | It is a legacy-read-only artifact | Check the immutable request schema version | Read it only for explicit legacy audit; use schema v3 plus signed handoff for new work | Convert or copy it into a new batch as a current success |

Any missing or invalid authority field fails closed as `unverified_result`. This is a per-request enrichment failure;
the downstream pipeline must record its degradation explicitly and must never silently reinterpret it as unsigned
success.
