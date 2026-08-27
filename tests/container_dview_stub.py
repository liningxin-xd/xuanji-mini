from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings


def main() -> None:
    port = int(os.environ["XUANJI_SMOKE_DVIEW_PORT"])
    count_path = Path(os.environ["XUANJI_SMOKE_DVIEW_COUNT_FILE"])
    query_spec = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "references"
            / "queries"
            / "registered-monitor-root.yaml"
        ).read_text(encoding="utf-8")
    )
    columns = query_spec["output"]["columns"]
    query_count = 0
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
        nonlocal query_count
        if database_type != "MaxCompute" or limit != 250:
            raise ToolError("container smoke query contract changed")
        partition = re.search(r"SELECT\s+'(\d{4}-\d{2}-\d{2})'\s+AS alert_date", sql)
        game = re.search(r"game_type\s*=\s*'(app|sandbox)'", sql)
        if partition is None or game is None:
            raise ToolError("container smoke received an unlocked query")
        query_count += 1
        count_path.write_text(str(query_count), encoding="utf-8")

        row = {}
        for name, value_type in columns.items():
            if value_type == "string":
                row[name] = "value"
            elif value_type == "date":
                row[name] = partition.group(1)
            else:
                row[name] = 0
        denominator = 1000
        numerator = 740
        rate = numerator / denominator
        row.update(
            {
                "platform": "ANDROID",
                "game_type": game.group(1),
                "game_download_device_num_1d": denominator,
                "game_download_cnt_1d": denominator,
                "game_download_complete_device_num_1d": numerator,
                "game_download_failed_device_num_1d": numerator,
                "game_download_failed_cnt_1d": numerator,
                "game_download_stop_device_num_1d": numerator,
                "game_download_complete_rate_1d": rate,
                "game_download_failed_rate_1d": rate,
                "game_download_failed_pv_rate_1d": rate,
                "game_download_stop_rate_1d": rate,
                "game_download_complete_prev_2d_device_num_1d": denominator,
                "game_download_complete_and_install_complete_prev_2d_device_num_p3d": numerator,
                "game_download_complete_and_install_complete_prev_2d_rate_p3d": rate,
            }
        )
        return {
            "query_id": f"private-container-root-{query_count}",
            "columns": list(columns),
            "rows": [[row[name] for name in columns]],
        }

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
