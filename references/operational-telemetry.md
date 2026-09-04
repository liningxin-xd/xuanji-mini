# Operational Telemetry

The native Host emits structured JSON records to its configured process log and
writes a private failure bundle for every unexpected tool-operation failure.
Telemetry must never change task state, retry a query, or become analytical
evidence.

## Correlation Model

Every model-to-Host tool invocation receives a random `operation_id`. An
unexpected failure also receives a random `error_id`; the same `error_id` is
returned in the generic ToolError so an operator can locate the private record
without exposing the underlying exception.

Three record prefixes are stable:

- `xuanji_service`: process start, clean stop, and fatal server failure.
- `xuanji_event`: operation start and per-query lifecycle events.
- `xuanji_operation`: exactly one task-operation summary from the production
  Runtime, or one boundary failure summary when the Runtime does not return.

`xuanji_event` query records expose only the root/attribution bucket, ordinal,
registered stage, registered step ID, attempt number, duration, and exception
class names. They do not expose the query or its result.

## Failure Fields

Operation schema v3 includes:

- `operation_id` and optional `error_id`;
- `analysis_profile`, `stage`, and `failure_stage`;
- the last query's bucket, ordinal, registered stage, step, attempt, and run ID;
- bounded `exception_types`, `exception_leaf_types`, and
  `exception_group_depth`;
- safe task progress: task status, current investigation index/count, and
  investigation status, plus the count of complete root snapshots;
- `diagnostic_bundle_status`, its error-ID-derived filename, and a bounded error
  type if writing the bundle itself fails.

`exception_type` is the earliest uncaught exception captured inside the DView
session. `exception_wrapper_type` is the exception seen at the outer boundary.
For example, `exception_type=RunnerError` with
`exception_wrapper_type=ExceptionGroup` proves that AnyIO wrapped a Runtime
error; it does not turn that error into evidence of concurrent query failure.

Normal process logs never contain exception messages, traceback frames, SQL,
DView response content, query IDs, payloads, or credentials. Exception
traversal in those logs is bounded and records only sanitized class names.

## Private Failure Bundle

An unexpected tool-operation failure is written atomically to:

```text
<XUANJI_RESULTS_ROOT>/diagnostics/<error-id>.json
```

The directory is mode `0700` and each bundle is mode `0600`. The bundle retains
the full tool input, submitted SQL and query options, the raw DView MCP tool
response when one was received, the value returned to result normalization,
the complete exception-group/chain messages, arguments and attributes, the
full traceback, and every file currently present under the task and current run
directories. Text and binary artifacts are retained without size truncation so
an operator can diagnose the original failure from one capture.

Credential fields and credential assignments are recursively replaced with
`[REDACTED]`, including tokens, secrets, passwords, Authorization values, API
keys, private/signing/encryption/access keys, and private-key blocks. Other
business payload, rows, query IDs, SQL, paths, hashes, and exception details are
intentionally retained. The bundle never captures the process environment.
Failure to write a bundle is reflected in the normal operation summary and
never replaces the original Host exception.

## Operator Procedure

1. Copy the `error_id` from the public ToolError and find the matching
   `xuanji_operation` record.
2. Confirm `diagnostic_bundle_status=written`, then open the matching private
   bundle from the Host results volume.
3. Use `operation_id` to collect the operation's `xuanji_event` records and
   align their timings with the bundle.
4. Read `failure_stage`, `exception_type`, and the last-query fields before
   deciding whether the failure is session setup, DView request/response,
   result processing, coordinator state, or pipeline handoff.
5. Treat a `query_failed` event as a query-level signal. It can be converted
   into a typed analytical degradation and followed by a successful operation.
   Alert on the final operation failure, not on the query event alone.
6. Retry only under the task resume contract. Keep the original task ID,
   payload, Host profile, and data root.

Recommended log-derived alerts are a non-null operation `exception_type`, any
`xuanji_service service_failed`, rising failure rate by `failure_stage`, and
query latency percentiles by `query_stage`/`query_step`. Query content must not
be added to labels or notifications. Retain failure bundles according to the
Host volume's restricted operator retention policy; never publish them through
the model-facing MCP endpoint.
