from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings


def main() -> None:
    port = int(os.environ["XUANJI_SMOKE_DVIEW_PORT"])
    mcp = FastMCP(
        name="Xuanji container smoke DView stub",
        host="0.0.0.0",
        port=port,
        streamable_http_path="/mcp/query",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @mcp.tool()
    async def query(sql: str, database_type: str, limit: int) -> dict[str, Any]:
        del sql, database_type, limit
        raise ToolError("container smoke must not execute DView queries")

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
