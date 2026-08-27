from __future__ import annotations

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
from .runtime import XuanjiHostRuntime

_HOST_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


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
            )
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
            )
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
            )
        )

    return mcp


async def _safe_call(operation: Any) -> dict[str, Any]:
    try:
        result = await operation
    # Every internal exception crosses a model-visible boundary here. Expose only
    # its type; exception text may contain SQL, query IDs, or private paths.
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"xuanji Host request failed ({type(exc).__name__})") from None
    if not isinstance(result, dict):
        raise ToolError("xuanji Host returned an invalid result envelope")
    return result


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
