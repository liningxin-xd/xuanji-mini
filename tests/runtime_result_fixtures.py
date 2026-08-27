from __future__ import annotations

from typing import Any

from runtime.contracts import canonical_sha256


def self_reported_result_event(
    ticket: dict[str, Any], raw_result: dict[str, Any], query_id: str
) -> dict[str, Any]:
    return {
        "event": "query_returned",
        "step_id": ticket["step_id"],
        "attempt_no": ticket["attempt_no"],
        "receipt_type": "self_reported_receipt",
        "submitted_sql_sha256": ticket["rendered_sql_sha256"],
        "query_id": query_id,
        "raw_result": raw_result,
        "raw_result_sha256": canonical_sha256(raw_result),
    }


def self_reported_error_event(
    ticket: dict[str, Any], *, query_id: str, error_class: str
) -> dict[str, Any]:
    return {
        "event": "query_error",
        "step_id": ticket["step_id"],
        "attempt_no": ticket["attempt_no"],
        "receipt_type": "self_reported_receipt",
        "submitted_sql_sha256": ticket["rendered_sql_sha256"],
        "query_id": query_id,
        "error_class": error_class,
        "error_code": "ODPS-0130071",
        "error_message": "column is not in GROUP BY",
    }


def raw_result_for_ticket(
    runner: Any,
    run_id: str,
    ticket: dict[str, Any],
    *,
    candidate: bool = False,
    break_source_closure: bool = False,
) -> dict[str, Any]:
    state = runner.load_state(run_id)
    if ticket["step_id"] == "secondary":
        secondary = next(
            step
            for step in state["post_primary"]["steps"]
            if step["id"] == "secondary"
        )
        binding = runner._binding_from_step(secondary)
        schema = runner.contracts.result_schema(binding.result_schema_id)
        columns = schema["columns_by_chain"][state["chain"]]
        return {
            "columns": list(columns),
            "rows": _secondary_bucket_rows(columns, state, secondary),
        }
    step = state["steps"][state["cursor"]]
    binding = runner._binding_from_step(step)
    schema = runner.contracts.result_schema(binding.result_schema_id)
    if schema.get("columns_from_query_spec"):
        columns, _ = runner.contracts.query_spec_result_contract(binding)
    else:
        columns = schema["columns"]
    if schema["validator"] == "install_stage":
        rows = [_stage_row(columns, state)]
    else:
        rows = _bucket_rows(
            columns,
            state,
            business_kind=schema["business_bucket_kind"],
            candidate=candidate,
            source_audit=bool(schema.get("require_source_bucket_audit")),
        )
        if break_source_closure:
            rows[0]["source_bucket_count"] += 1
    return {"columns": list(columns), "rows": rows}


def _secondary_bucket_rows(
    columns: dict[str, str],
    state: dict[str, Any],
    secondary: dict[str, Any],
) -> list[dict[str, Any]]:
    game_step = next(step for step in state["steps"] if step["id"] == "game_id")
    parent = next(
        candidate
        for candidate in game_step["candidates"]
        if candidate["value"] == secondary["parent_value"]
    )
    parent_counts = parent["private_counts"]
    root_counts = {
        "current_numerator": game_step["root_current_numerator"],
        "current_denominator": game_step["root_current_denominator"],
        "baseline_numerator": game_step["root_baseline_numerator"],
        "baseline_denominator": game_step["root_baseline_denominator"],
    }
    outside_counts = {
        name: root_counts[name] - parent_counts[name]
        for name in root_counts
    }
    buckets = (
        ("child", "secondary-a", "Secondary A", parent_counts),
        ("outside_parent", "outside_parent", "outside_parent", outside_counts),
    )
    observation_days = 3 if state["game_type"] == "app" else 1
    rows: list[dict[str, Any]] = []
    for kind, value, label, counts in buckets:
        row = _empty_row(columns)
        current_denominator = int(counts["current_denominator"])
        baseline_denominator = int(counts["baseline_denominator"])
        values = {
            "analysis_date": state["analysis_date"],
            "game_type": state["game_type"],
            "parent_value": secondary["parent_value"],
            "bucket_kind": kind,
            "dimension_value": value,
            "dimension_label": label,
            "collapsed_source_bucket_count": 1,
            "source_bucket_count": len(buckets),
            "baseline_day_count": 7,
            "current_denominator": current_denominator,
            "baseline_denominator": baseline_denominator,
            "current_numerator": int(counts["current_numerator"]),
            "baseline_numerator": int(counts["baseline_numerator"]),
            "current_row_count": current_denominator,
            "baseline_row_count": baseline_denominator,
            "duplicate_row_count": 0,
            "invalid_metric_row_count": 0,
            "overall_current_denominator": int(
                root_counts["current_denominator"]
            ),
            "overall_baseline_denominator": int(
                root_counts["baseline_denominator"]
            ),
            "overall_current_numerator": int(root_counts["current_numerator"]),
            "overall_baseline_numerator": int(root_counts["baseline_numerator"]),
            "overall_current_row_count": int(
                root_counts["current_denominator"]
            ),
            "overall_baseline_row_count": int(
                root_counts["baseline_denominator"]
            ),
            "overall_duplicate_row_count": 0,
            "overall_invalid_metric_row_count": 0,
            "current_observation_days_min": (
                observation_days if current_denominator > 0 else None
            ),
            "current_observation_days_max": (
                observation_days if current_denominator > 0 else None
            ),
            "baseline_observation_days_min": (
                observation_days if baseline_denominator > 0 else None
            ),
            "baseline_observation_days_max": (
                observation_days if baseline_denominator > 0 else None
            ),
            "overall_current_observation_days_min": observation_days,
            "overall_current_observation_days_max": observation_days,
            "overall_baseline_observation_days_min": observation_days,
            "overall_baseline_observation_days_max": observation_days,
        }
        row.update({name: item for name, item in values.items() if name in row})
        rows.append(row)
    return rows


def _bucket_rows(
    columns: dict[str, str],
    state: dict[str, Any],
    *,
    business_kind: str,
    candidate: bool,
    source_audit: bool,
) -> list[dict[str, Any]]:
    direction = "higher_is_better" if state["metric"] in {
        "下载完成率",
        "下载安装完成率",
    } else "lower_is_better"
    if candidate and direction == "higher_is_better":
        buckets = [
            (business_kind, "slice-a", "Slice A", 500, 700, 350, 560),
            ("residual", "__other_below_threshold__", "Residual", 500, 300, 440, 240),
        ]
    elif candidate:
        buckets = [
            (business_kind, "slice-a", "Slice A", 500, 700, 450, 560),
            ("residual", "__other_below_threshold__", "Residual", 500, 300, 360, 240),
        ]
    else:
        residual_current_numerator = 710 if direction == "higher_is_better" else 730
        buckets = [
            (business_kind, "slice-flat", "Slice Flat", 100, 100, 80, 80),
            (
                "residual",
                "__other_below_threshold__",
                "Residual",
                900,
                900,
                residual_current_numerator,
                720,
            ),
        ]

    overall_current_denominator = sum(item[3] for item in buckets)
    overall_baseline_denominator = sum(item[4] for item in buckets)
    overall_current_numerator = sum(item[5] for item in buckets)
    overall_baseline_numerator = sum(item[6] for item in buckets)
    rows: list[dict[str, Any]] = []
    for bucket_kind, value, label, current_den, baseline_den, current_num, baseline_num in buckets:
        row = _empty_row(columns)
        values = {
                "analysis_date": state["analysis_date"],
                "game_type": state["game_type"],
                "bucket_kind": bucket_kind,
                "dimension_value": value,
                "dimension_label": label,
                "bucket_baseline_active_day_count": 7,
                "baseline_day_count": 7,
                "current_denominator": current_den,
                "baseline_denominator": baseline_den,
                "current_numerator": current_num,
                "baseline_numerator": baseline_num,
                "overall_current_denominator": overall_current_denominator,
                "overall_baseline_denominator": overall_baseline_denominator,
                "overall_current_numerator": overall_current_numerator,
                "overall_baseline_numerator": overall_baseline_numerator,
                "invalid_metric_row_count": 0,
                "overall_invalid_metric_row_count": 0,
                "overall_anchor_duplicate_excess": 0,
            }
        row.update({name: value for name, value in values.items() if name in row})
        if source_audit:
            row.update(
                {
                    "collapsed_source_bucket_count": 1,
                    "source_bucket_count": len(buckets),
                    "overall_current_dimension_matched_denominator": overall_current_denominator,
                    "overall_baseline_dimension_matched_denominator": overall_baseline_denominator,
                    "overall_current_dimension_unmatched_denominator": 0,
                    "overall_baseline_dimension_unmatched_denominator": 0,
                    "overall_current_dimension_match_rate": 1.0,
                    "overall_baseline_dimension_match_rate": 1.0,
                }
            )
        for name in (
            "current_observation_days_min",
            "current_observation_days_max",
            "baseline_observation_days_min",
            "baseline_observation_days_max",
            "overall_current_observation_days_min",
            "overall_current_observation_days_max",
            "overall_baseline_observation_days_min",
            "overall_baseline_observation_days_max",
        ):
            if name in row:
                row[name] = 3
        rows.append(row)
    return rows


def _stage_row(
    columns: dict[str, str], state: dict[str, Any]
) -> dict[str, Any]:
    row = _empty_row(columns)
    row.update(
        {
            "analysis_date": state["analysis_date"],
            "game_type": state["game_type"],
            "baseline_day_count": 7,
            "current_download_count": 1000,
            "baseline_download_count": 7000,
            "current_start_count": 800,
            "baseline_start_count": 5600,
            "current_pre_start_unfinished_count": 200,
            "baseline_pre_start_unfinished_count": 1400,
            "current_complete_count": 700,
            "baseline_complete_count": 4900,
            "current_started_complete_count": 700,
            "baseline_started_complete_count": 4900,
            "current_no_observed_start_count": 200,
            "baseline_no_observed_start_count": 1400,
            "current_started_not_complete_count": 100,
            "baseline_started_not_complete_count": 700,
            "current_no_observed_start_rate": 0.2,
            "baseline_no_observed_start_rate": 0.2,
            "current_pre_start_unfinished_rate": 0.2,
            "baseline_pre_start_unfinished_rate": 0.2,
            "current_started_not_complete_share": 0.1,
            "baseline_started_not_complete_share": 0.1,
            "current_post_start_completion_rate": 0.875,
            "baseline_post_start_completion_rate": 0.875,
            "current_official_loss_closure_gap": 0,
            "baseline_official_loss_closure_gap": 0,
            "current_observation_days_min": 3,
            "current_observation_days_max": 3,
            "baseline_observation_days_min": 3,
            "baseline_observation_days_max": 3,
        }
    )
    return row


def _empty_row(columns: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value_type in columns.items():
        if value_type == "date":
            result[name] = "2026-01-01"
        elif value_type == "string":
            result[name] = "value"
        elif value_type == "number":
            result[name] = 0.0
        elif value_type == "integer_or_null":
            result[name] = None
        else:
            result[name] = 0
    return result
