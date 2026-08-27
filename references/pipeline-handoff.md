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
