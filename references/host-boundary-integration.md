# Primary Host Boundary Integration

`primary_v1` production execution exposes exactly three model tools from a Host
process below the model/UI boundary:

- `xuanji_run_investigation`: creates a trusted schema-v4 run or identically
  resumes the same immutable run, executes the fixed queue, and returns either
  a compact writer pack or a bounded repair packet.
- `xuanji_submit_repair`: accepts one semantic SQL repair and resumes the fixed
  queue.
- `xuanji_finalize`: assembles and validates the final analysis, sends the full
  authoritative artifact to a machine-only sink, and returns a model-safe copy
  plus a validation receipt summary.

The platform integration constructs `PrimaryInvestigationHost` with its DView
callable, a Host-owned receipt secret of at least 32 bytes, an isolated runs
root, and a validated-result sink. Only the three `xuanji_*` methods
may be registered as model tools. The DView callable, runner, adapter, sink, and
artifact readers must remain private to the Host process.

Resume accepts only the current schema-v4 state with identical immutable
identity and contract hashes. A legacy state or changed run identity is rejected
and must start under a new run ID.

Normal tool results never contain SQL, raw rows, query IDs, receipts, or raw
result hashes. A `repair_required` result is the sole exception for SQL and
contains only the current SQL, raw semantic error, fixed triage rules, and
required repair fields. Query IDs remain private even during repair.

The complete validated analysis contains audit query IDs and is therefore
delivered only through `validated_result_sink`. The model-visible final analysis
is returned as `analysis_preview` with those audit-only fields removed after
validation; it is not the artifact that a pipeline writer should persist. A
sink must be idempotent because an identical `xuanji_finalize` retry redelivers
the already validated artifact. A retry with a different writer patch or
analysis context is rejected.

Repository tests prove the public return surfaces and the fixed download/APK
queues. Production acceptance still requires a fresh isolated Host/session and
inspection of the entire model-visible transcript. A shell wrapper, background
terminal, or model-issued nested DView call does not satisfy this boundary.
