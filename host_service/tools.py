from __future__ import annotations

import json
import logging
import re
import time
from typing import Annotated, Any
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, Field

from .auth import StaticBearerTokenVerifier
from .config import HostServiceSettings
from .diagnostics import FailureBundleWriter
from .runtime import XuanjiHostRuntime
from .telemetry import OperationTrace, bind_trace, emit_event, exception_type_name

_HOST_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_LOGGER = logging.getLogger(__name__)
_LOG_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def create_mcp(
    settings: HostServiceSettings,
    *,
    runtime: XuanjiHostRuntime | None = None,
) -> FastMCP:
    public_url = AnyHttpUrl(settings.public_url)
    mcp = FastMCP(
        name="Xuanji Primary Host",
        instructions=(
            "Deterministic Android download/install attribution Host. "
            "Only the registered xuanji tools are available."
        ),
        host=settings.bind_host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=False,
        token_verifier=StaticBearerTokenVerifier(settings.host_bearer_token),
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=public_url,
            required_scopes=["xuanji"],
        ),
        transport_security=_transport_security(settings.public_url),
    )
    host_runtime = runtime or XuanjiHostRuntime(settings)
    failure_bundle_writer = FailureBundleWriter(
        diagnostics_root=settings.results_root / "diagnostics",
        tasks_root=settings.tasks_root,
        runs_root=settings.runs_root,
    )

    @mcp.tool(annotations=_HOST_ANNOTATIONS)
    async def xuanji_run_task(
        task_id: Annotated[str, Field(description="Unique immutable task ID")],
        dqc_payload: Annotated[
            dict[str, Any], Field(description="Raw DataWorks DQC payload")
        ],
    ) -> dict[str, Any]:
        return await _safe_call(
            host_runtime.run_task(
                task_id=task_id,
                dqc_payload=dqc_payload,
            ),
            phase="run_task",
            task_id=task_id,
            investigation_id=None,
            analysis_profile=settings.analysis_profile,
            failure_bundle_writer=failure_bundle_writer,
            tool_input={"task_id": task_id, "dqc_payload": dqc_payload},
        )

    @mcp.tool(annotations=_HOST_ANNOTATIONS)
    async def xuanji_submit_repair(
        task_id: Annotated[str, Field(description="Existing task ID")],
        investigation_id: Annotated[
            str, Field(description="Current investigation ID")
        ],
        run_id: Annotated[str, Field(description="Existing run ID")],
        step_id: Annotated[str, Field(description="Blocked fixed-plan step ID")],
        repair_attempt: Annotated[int, Field(description="Issued repair attempt")],
        repair_reason: Annotated[
            str, Field(description="Evidence-based repair reason")
        ],
        error_evidence: Annotated[
            str, Field(description="Evidence from the issued error")
        ],
        repaired_sql: Annotated[str, Field(description="Bounded semantic SQL repair")],
    ) -> dict[str, Any]:
        return await _safe_call(
            host_runtime.submit_repair(
                task_id=task_id,
                investigation_id=investigation_id,
                run_id=run_id,
                step_id=step_id,
                repair_attempt=repair_attempt,
                repair_reason=repair_reason,
                error_evidence=error_evidence,
                repaired_sql=repaired_sql,
            ),
            phase="submit_repair",
            task_id=task_id,
            investigation_id=investigation_id,
            analysis_profile=settings.analysis_profile,
            failure_bundle_writer=failure_bundle_writer,
            tool_input={
                "task_id": task_id,
                "investigation_id": investigation_id,
                "run_id": run_id,
                "step_id": step_id,
                "repair_attempt": repair_attempt,
                "repair_reason": repair_reason,
                "error_evidence": error_evidence,
                "repaired_sql": repaired_sql,
            },
        )

    @mcp.tool(annotations=_HOST_ANNOTATIONS)
    async def xuanji_finalize(
        task_id: Annotated[str, Field(description="Existing task ID")],
        investigation_id: Annotated[
            str, Field(description="Investigation awaiting one writer patch")
        ],
        writer_patch: Annotated[
            dict[str, Any],
            Field(description="Text-only patch derived from the writer pack"),
        ],
    ) -> dict[str, Any]:
        return await _safe_call(
            host_runtime.finalize(
                task_id=task_id,
                investigation_id=investigation_id,
                writer_patch=writer_patch,
            ),
            phase="finalize",
            task_id=task_id,
            investigation_id=investigation_id,
            analysis_profile=settings.analysis_profile,
            failure_bundle_writer=failure_bundle_writer,
            tool_input={
                "task_id": task_id,
                "investigation_id": investigation_id,
                "writer_patch": writer_patch,
            },
        )

    return mcp


async def _safe_call(
    operation: Any,
    *,
    phase: str,
    task_id: str,
    investigation_id: str | None,
    analysis_profile: str,
    failure_bundle_writer: FailureBundleWriter,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    started = time.monotonic()
    trace = OperationTrace.create(
        task_id=task_id,
        phase=phase,
        boundary_owned=True,
        analysis_profile=analysis_profile,
    )
    trace.capture_tool_input(tool_input)
    with bind_trace(trace):
        emit_event(_LOGGER, logging.INFO, "operation_started", trace=trace)
        try:
            result = await operation
        except Exception as exc:  # noqa: BLE001
            trace.capture_failure(exc)
            _write_failure_bundle(
                failure_bundle_writer,
                trace=trace,
                exc=exc,
            )
            _log_failure(
                trace=trace,
                investigation_id=investigation_id,
                duration_ms=round((time.monotonic() - started) * 1000),
                outer_exception_type=exception_type_name(exc),
            )
            raise ToolError(
                f"xuanji Host request failed (error_id={trace.error_id})"
            ) from None
    if not isinstance(result, dict):
        trace.set_stage("boundary_result_validation")
        trace.capture_named_failure("InvalidResultEnvelope")
        invalid_result = TypeError("Host returned an invalid result envelope")
        _write_failure_bundle(
            failure_bundle_writer,
            trace=trace,
            exc=invalid_result,
            result=result,
        )
        _log_failure(
            trace=trace,
            investigation_id=investigation_id,
            duration_ms=round((time.monotonic() - started) * 1000),
            outer_exception_type="InvalidResultEnvelope",
        )
        raise ToolError(
            f"xuanji Host returned an invalid result envelope "
            f"(error_id={trace.error_id})"
        )
    return result


def _log_failure(
    *,
    trace: OperationTrace,
    investigation_id: str | None,
    duration_ms: int,
    outer_exception_type: str,
) -> None:
    payload = {
        "schema_version": 3,
        **trace.fields(),
        "investigation_id": (
            investigation_id
            if isinstance(investigation_id, str)
            and _LOG_TASK_ID.fullmatch(investigation_id)
            else trace.investigation_id
        ),
        "duration_ms": duration_ms,
        "writer_pack_bytes": 0,
        "overall_status": None,
        "exception_wrapper_type": outer_exception_type,
    }
    _LOGGER.error(
        "xuanji_operation %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def _write_failure_bundle(
    writer: FailureBundleWriter,
    *,
    trace: OperationTrace,
    exc: BaseException,
    result: Any = None,
) -> None:
    try:
        path = writer.write(trace=trace, exc=exc, result=result)
    except Exception as diagnostic_exc:  # noqa: BLE001
        trace.diagnostic_bundle_status = "write_failed"
        trace.diagnostic_bundle_error_type = exception_type_name(diagnostic_exc)
    else:
        trace.diagnostic_bundle_status = "written"
        trace.diagnostic_bundle_file = path.name


def _transport_security(public_url: str) -> TransportSecuritySettings:
    parsed = urlparse(public_url)
    public_host = parsed.hostname or "localhost"
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if public_host not in {"127.0.0.1", "localhost", "::1"}:
        hosts.extend([public_host, f"{public_host}:*"])
    origins = [f"http://{host}" for host in hosts]
    if parsed.scheme == "https":
        origins.extend(f"https://{host}" for host in hosts)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
