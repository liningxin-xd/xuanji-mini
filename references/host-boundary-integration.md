# Primary Host Boundary Integration

Both analysis profiles expose exactly three task-level model tools
from a Host process below the model/UI boundary:

- `xuanji_run_task`: freezes one raw DQC payload, resolves zero or more
  investigations, and returns one writer pack, one bounded repair packet, or
  the completed task preview.
- `xuanji_submit_repair`: accepts one semantic SQL repair for the current
  task/investigation/run identity and resumes that investigation.
- `xuanji_finalize`: accepts only task ID, investigation ID, and the current
  text-only writer patch. It advances to the next investigation or returns the
  completed task preview.

`PrimaryInvestigationHost` remains an internal stable library. The task
coordinator owns DQC normalization, deterministic registered binding, root preflight,
newness gating, serial investigation order, and final task assembly. It creates
trusted schema-v4 runs only after root preflight selects `full_queue` and passes
the frozen canonical root metric into every family. Investigation-level methods
must not be registered as model tools.

The deployment-only `XUANJI_ANALYSIS_PROFILE` selects `primary_v1` or
`primary_v2`; it is never a tool argument. Both profiles execute the same frozen
primary queue. `primary_v2` then runs only the post-primary steps enabled by the
machine contract and returns one merged writer pack without another model call.

The platform integration constructs the coordinator with the private
`PrimaryInvestigationHost`, DView callable, Host-owned receipt authority,
isolated run/task roots, and machine-only run/task sinks. The DView callable,
runner, adapters, normalizer, route registry, state, sinks, and artifact readers
remain private to the Host process.

When the existing DView MCP service cannot be modified, deploy the repository's
independent `host_service` and follow [Native Primary Host Deployment](native-host-deployment.md).
Its server-side MCP client calls the existing read-only DView endpoint below the
UI boundary. Calling DView from a model-visible terminal remains invalid.

Task resume requires the identical payload hash, analysis profile, compiled metric-definition
bundle, and a valid task-state integrity hash. Run resume additionally requires
current schema-v4 state, identical
immutable identity and profile-specific contract hashes, and the same canonical root metric. A
changed task payload, writer patch, or run identity is rejected.

Normal tool results never contain SQL, raw rows, query IDs, receipts, or raw
result hashes. A `repair_required` result is the sole exception for SQL and
contains only the current SQL, raw semantic error, fixed triage rules, and
required repair fields. Query IDs remain private even during repair.

Each complete investigation is loaded from its validated run sink. The task
assembler verifies exact rule-index coverage, computes `overall_status`, and
writes the complete ordered result to the task sink. The model receives only a
recursive redaction of that task result plus receipt hashes. The Host also
derives a signed `pipeline_handoff` from the reloaded and revalidated task sink;
the redacted preview is acceptable to a pipeline writer only when paired with
that handoff and verified against a pinned public key, immutable task ID, and
payload hash. The private task artifact itself never crosses the model boundary.
Both sinks are idempotent. An
identical finalize retry is accepted, while a conflicting writer patch or sink
payload is rejected.

Unexpected Runtime exceptions cross only the outer MCP error boundary. That
boundary writes private structured operation/query telemetry and returns a
generic ToolError with an opaque `error_id`, but without exception text. The
Runtime captures the original exception class before MCP/AnyIO context teardown
can wrap it in an `ExceptionGroup`; the boundary separately records the wrapper
class. See [Operational Telemetry](operational-telemetry.md).
Only `RootPreflightError` and already classified DView evidence may become an
analytical blocked status.

Repository tests prove exact three-tool exposure, serial task behavior, public
redaction, fixed queues, and sink conflict rejection. Production acceptance
still requires a fresh isolated task-level Host/session and inspection of the
entire model-visible transcript. A shell wrapper, background terminal, or
model-issued nested DView call does not satisfy this boundary.
