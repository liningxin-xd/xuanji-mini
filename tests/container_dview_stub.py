from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from runtime.contracts import RepositoryContracts
from runtime.query_builder import QueryBuilder
from tests.runtime_result_fixtures import _bucket_rows


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
    query_counts = {"root": 0, "primary": 0, "post_primary": 0}
    primary_results = _primary_results()
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
        if database_type != "MaxCompute" or limit != 250:
            raise ToolError("container smoke query contract changed")
        primary = primary_results.get(sql)
        if primary is not None:
            query_counts["primary"] += 1
            count_path.write_text(
                json.dumps(query_counts, sort_keys=True), encoding="utf-8"
            )
            return {
                "query_id": (
                    f"private-container-primary-{query_counts['primary']}"
                ),
                "columns": primary["columns"],
                "rows": primary["rows"],
            }

        partition = re.search(r"SELECT\s+'(\d{4}-\d{2}-\d{2})'\s+AS alert_date", sql)
        game = re.search(r"game_type\s*=\s*'(app|sandbox)'", sql)
        if partition is None or game is None:
            raise ToolError("container smoke received an unlocked query")
        query_counts["root"] += 1
        count_path.write_text(
            json.dumps(query_counts, sort_keys=True), encoding="utf-8"
        )

        row = {}
        for name, value_type in columns.items():
            if value_type == "string":
                row[name] = "value"
            elif value_type == "date":
                row[name] = partition.group(1)
            else:
                row[name] = 0
        denominator = 1000
        is_current = query_counts["root"] % 8 == 1
        numerator = 790 if is_current else 800
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
            "query_id": f"private-container-root-{query_counts['root']}",
            "columns": list(columns),
            "rows": [[row[name] for name in columns]],
        }

    mcp.run(transport="streamable-http")


def _primary_results() -> dict[str, dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    contracts = RepositoryContracts(root)
    builder = QueryBuilder(contracts)
    plan = contracts.select_plan("download", "app", "下载完成率")
    state = {
        "analysis_date": "2026-08-24",
        "game_type": "app",
        "metric": "下载完成率",
    }
    results: dict[str, dict[str, Any]] = {}
    for step in plan.steps:
        binding = contracts.binding_for(
            plan, step.id, state["metric"], state["game_type"]
        )
        built = builder.build(
            binding,
            {
                "business_date": state["analysis_date"],
                "game_type": state["game_type"],
            },
        )
        schema = contracts.result_schema(binding.result_schema_id)
        if schema.get("columns_from_query_spec"):
            columns, _ = contracts.query_spec_result_contract(binding)
        else:
            columns = schema["columns"]
        rows = _bucket_rows(
            columns,
            state,
            business_kind=schema["business_bucket_kind"],
            candidate=False,
            source_audit=bool(schema.get("require_source_bucket_audit")),
        )
        results[built.sql] = {
            "columns": list(columns),
            "rows": [[row[name] for name in columns] for row in rows],
        }
    return results


if __name__ == "__main__":
    main()
