from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


def main() -> None:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings

    port = int(os.environ["XUANJI_SMOKE_DVIEW_PORT"])
    count_path = Path(os.environ["XUANJI_SMOKE_DVIEW_COUNT_FILE"])
    analysis_profile = os.environ["XUANJI_SMOKE_ANALYSIS_PROFILE"]
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
    attribution_results = _attribution_results(analysis_profile)
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
        attribution = attribution_results.get(sql)
        if attribution is not None:
            phase = attribution["phase"]
            query_counts[phase] += 1
            count_path.write_text(
                json.dumps(query_counts, sort_keys=True), encoding="utf-8"
            )
            return {
                "query_id": f"private-container-{phase}-{query_counts[phase]}",
                "columns": attribution["columns"],
                "rows": attribution["rows"],
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


def _attribution_results(analysis_profile: str) -> dict[str, dict[str, Any]]:
    if analysis_profile not in {"primary_v1", "primary_v2"}:
        raise ValueError("container smoke analysis profile is invalid")
    root = Path(__file__).resolve().parents[1]
    results: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        runner = AttributionRunner(
            root,
            runs_root=temp_dir,
            analysis_profile=analysis_profile,
        )
        run_id = "container-dview-fixture"
        runner.init_run(
            run_id=run_id,
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-24",
            receipt_mode="self_reported",
        )
        query_index = 0
        while True:
            ticket = runner.next_action(run_id)
            if ticket["action"] == "queue_complete":
                break
            query_index += 1
            step_id = ticket["step_id"]
            raw_result = raw_result_for_ticket(
                runner,
                run_id,
                ticket,
                candidate=(
                    analysis_profile == "primary_v2" and step_id == "game_id"
                ),
            )
            columns = raw_result["columns"]
            if ticket["rendered_sql"] in results:
                raise RuntimeError("container smoke generated duplicate locked SQL")
            results[ticket["rendered_sql"]] = {
                "phase": (
                    "post_primary"
                    if step_id
                    in {
                        "secondary",
                        "game_background",
                        "error_code",
                        "cross_dimension_overlap",
                    }
                    else "primary"
                ),
                "step_id": step_id,
                "columns": list(columns),
                "rows": [
                    [row[name] for name in columns] for row in raw_result["rows"]
                ],
            }
            runner.record(
                run_id,
                self_reported_result_event(
                    ticket,
                    raw_result,
                    f"fixture-{analysis_profile}-{query_index}",
                ),
            )
    return results


if __name__ == "__main__":
    main()
