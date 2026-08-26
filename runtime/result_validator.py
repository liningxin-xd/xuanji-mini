from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from .contracts import RepositoryContracts
from .contribution import ContributionError, calculate_contributions
from .models import QueryBinding


class ResultValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResultValidationOutcome:
    status: str
    candidate_count: int
    candidates: tuple[dict[str, Any], ...]
    warning_codes: tuple[str, ...]
    root_delta: float | None


class ResultValidator:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def validate(
        self,
        *,
        raw_result: dict[str, Any],
        binding: QueryBinding,
        step_id: str,
        metric: str,
        analysis_date: str,
        game_type: str,
        produces_candidates: bool,
    ) -> ResultValidationOutcome:
        schema = self.contracts.result_schema(binding.result_schema_id)
        quality: dict[str, Any] = {}
        if schema.get("columns_from_query_spec"):
            columns, quality = self.contracts.query_spec_result_contract(binding)
        else:
            columns = schema.get("columns")
        if not isinstance(columns, dict) or not columns:
            raise ResultValidationError("schema_invalid", "result columns are undefined")
        rows = self._normalize_rows(raw_result, columns, quality)
        self._validate_context(rows, analysis_date, game_type)

        validator = schema["validator"]
        if validator == "install_stage":
            return self._validate_install_stage(rows)
        if validator != "contribution_buckets":
            raise ResultValidationError(
                "schema_invalid", f"unsupported result validator: {validator}"
            )
        return self._validate_contribution_buckets(
            rows=rows,
            schema=schema,
            step_id=step_id,
            metric=metric,
            produces_candidates=produces_candidates,
        )

    def _normalize_rows(
        self,
        raw_result: dict[str, Any],
        expected_columns: dict[str, str],
        quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_result, dict):
            raise ResultValidationError(
                "schema_invalid", "raw_result must be an object"
            )
        columns = raw_result.get("columns")
        rows = raw_result.get("rows")
        expected_names = list(expected_columns)
        if columns != expected_names:
            raise ResultValidationError(
                "schema_invalid",
                "raw_result columns must exactly match the registered output schema",
            )
        if not isinstance(rows, list):
            raise ResultValidationError(
                "schema_invalid", "raw_result.rows must be an array"
            )
        defaults = self.contracts.result_defaults
        row_limit = int(defaults["row_limit_exclusive"])
        configured_max = quality.get("max_rows")
        if isinstance(configured_max, int):
            row_limit = min(row_limit, configured_max + 1)
        if len(rows) >= row_limit:
            raise ResultValidationError(
                "result_incomplete",
                f"result row count {len(rows)} violates the < {row_limit} budget",
            )
        if quality.get("require_non_empty", True) and not rows:
            raise ResultValidationError("result_incomplete", "query returned no rows")

        normalized: list[dict[str, Any]] = []
        ranges = quality.get("ranges", {})
        for row_index, raw_row in enumerate(rows):
            if isinstance(raw_row, list):
                if len(raw_row) != len(expected_names):
                    raise ResultValidationError(
                        "schema_invalid",
                        f"row {row_index} does not match the registered column count",
                    )
                row = dict(zip(expected_names, raw_row, strict=True))
            elif isinstance(raw_row, dict):
                if set(raw_row) != set(expected_names):
                    raise ResultValidationError(
                        "schema_invalid",
                        f"row {row_index} keys do not match the registered columns",
                    )
                row = {name: raw_row[name] for name in expected_names}
            else:
                raise ResultValidationError(
                    "schema_invalid", f"row {row_index} must be an array or object"
                )
            for name, value_type in expected_columns.items():
                range_contract = ranges.get(name, {})
                if (
                    row[name] is None
                    and isinstance(range_contract, dict)
                    and range_contract.get("allow_null") is True
                ):
                    continue
                self._validate_type(row_index, name, row[name], value_type)
            for name, range_contract in ranges.items():
                if name in row:
                    self._validate_range(row_index, name, row[name], range_contract)
            normalized.append(row)

        unique_by = quality.get("unique_by")
        if not isinstance(unique_by, list):
            unique_by = [
                name
                for name in (
                    "analysis_date",
                    "game_type",
                    "bucket_kind",
                    "dimension_value",
                )
                if name in expected_columns
            ]
        seen: set[tuple[Any, ...]] = set()
        for row in normalized:
            key = tuple(row[name] for name in unique_by)
            if key in seen:
                raise ResultValidationError(
                    "schema_invalid", f"duplicate result key: {key}"
                )
            seen.add(key)
        return normalized

    def _validate_contribution_buckets(
        self,
        *,
        rows: list[dict[str, Any]],
        schema: dict[str, Any],
        step_id: str,
        metric: str,
        produces_candidates: bool,
    ) -> ResultValidationOutcome:
        defaults = self.contracts.result_defaults
        metric_contract = self.contracts.metric_result_contract(metric)
        baseline_days = int(defaults["baseline_day_count"])
        for row in rows:
            if row["baseline_day_count"] != baseline_days:
                raise ResultValidationError(
                    "quality_gate_failed",
                    f"baseline_day_count must equal {baseline_days}",
                )
        self._validate_bucket_kinds(rows, schema["business_bucket_kind"])
        self._validate_metric_counts(
            rows, numerator_subset=metric_contract["numerator_subset"]
        )
        self._validate_overall_constants_and_rehook(rows)
        if schema.get("require_source_bucket_audit"):
            self._validate_source_bucket_audit(
                rows, business_kind=schema["business_bucket_kind"]
            )
            self._validate_dimension_quality_rehook(rows)
        self._validate_metric_quality(rows)

        try:
            contributions, root_delta = calculate_contributions(
                rows,
                direction=metric_contract["direction"],
                tolerance=float(defaults["contribution_tolerance"]),
            )
        except ContributionError as exc:
            raise ResultValidationError(
                "contribution_not_closed", str(exc)
            ) from exc

        warnings = self._bucket_warning_codes(rows)
        candidates: list[dict[str, Any]] = []
        if produces_candidates:
            row_by_identity = {
                (row["bucket_kind"], str(row["dimension_value"])): row
                for row in rows
            }
            for contribution in contributions:
                row = row_by_identity[
                    (contribution.bucket_kind, contribution.dimension_value)
                ]
                if contribution.bucket_kind != schema["business_bucket_kind"]:
                    continue
                if self._is_quality_value(contribution.dimension_value):
                    continue
                current_sample = float(row["current_denominator"])
                baseline_daily_sample = float(row["baseline_denominator"]) / baseline_days
                if max(current_sample, baseline_daily_sample) < float(
                    defaults["minimum_sample"]
                ):
                    continue
                if max(
                    contribution.current_share, contribution.baseline_share
                ) < float(defaults["minimum_share"]):
                    continue
                if contribution.adverse_impact + 1e-15 < float(
                    defaults["minimum_adverse_impact"]
                ):
                    continue
                candidate = contribution.as_candidate(step_id)
                if "bucket_baseline_active_day_count" in row:
                    candidate["bucket_baseline_active_day_count"] = row[
                        "bucket_baseline_active_day_count"
                    ]
                    if row["bucket_baseline_active_day_count"] < 7:
                        warnings.add("short_bucket_baseline")
                candidates.append(candidate)
        candidates.sort(
            key=lambda item: (-item["adverse_impact_bp"], item["value"])
        )
        return ResultValidationOutcome(
            status="succeeded",
            candidate_count=len(candidates),
            candidates=tuple(candidates),
            warning_codes=tuple(sorted(warnings)),
            root_delta=root_delta,
        )

    def _validate_install_stage(
        self, rows: list[dict[str, Any]]
    ) -> ResultValidationOutcome:
        if len(rows) != 1:
            raise ResultValidationError(
                "schema_invalid", "install_stage must return exactly one row"
            )
        row = rows[0]
        if row["baseline_day_count"] != 7:
            raise ResultValidationError(
                "quality_gate_failed", "install_stage baseline_day_count must equal 7"
            )
        hard_zero_fields = (
            "current_invalid_metric_row_count",
            "baseline_invalid_metric_row_count",
            "current_invalid_start_row_count",
            "baseline_invalid_start_row_count",
            "current_invalid_event_match_row_count",
            "baseline_invalid_event_match_row_count",
            "current_anchor_duplicate_excess",
            "baseline_anchor_duplicate_excess",
            "current_official_loss_closure_gap",
            "baseline_official_loss_closure_gap",
        )
        non_zero = [name for name in hard_zero_fields if row[name] != 0]
        if non_zero:
            raise ResultValidationError(
                "quality_gate_failed",
                f"install_stage quality gates are non-zero: {non_zero}",
            )
        for prefix in ("current", "baseline"):
            download = row[f"{prefix}_download_count"]
            start = row[f"{prefix}_start_count"]
            complete = row[f"{prefix}_complete_count"]
            started_complete = row[f"{prefix}_started_complete_count"]
            started_not_complete = row[f"{prefix}_started_not_complete_count"]
            pre_start_unfinished = row[f"{prefix}_pre_start_unfinished_count"]
            if not (complete <= download and start <= download):
                raise ResultValidationError(
                    "quality_gate_failed", f"{prefix} C/S must be subsets of D"
                )
            if started_complete > start or started_complete + started_not_complete != start:
                raise ResultValidationError(
                    "quality_gate_failed", f"{prefix} started buckets do not close"
                )
            if pre_start_unfinished + started_not_complete != download - complete:
                raise ResultValidationError(
                    "quality_gate_failed", f"{prefix} official loss does not close"
                )
            if row[f"{prefix}_no_observed_start_count"] != download - start:
                raise ResultValidationError(
                    "quality_gate_failed",
                    f"{prefix} no-observed-start count is inconsistent",
                )
            if download > 0:
                for suffix in ("observation_days_min", "observation_days_max"):
                    if row[f"{prefix}_{suffix}"] != 3:
                        raise ResultValidationError(
                            "quality_gate_failed",
                            f"{prefix} observation window must be exactly 3 days",
                        )
            self._check_ratio(
                row[f"{prefix}_no_observed_start_rate"], download - start, download
            )
            self._check_ratio(
                row[f"{prefix}_pre_start_unfinished_rate"],
                pre_start_unfinished,
                download,
            )
            self._check_ratio(
                row[f"{prefix}_started_not_complete_share"],
                started_not_complete,
                download,
            )
            self._check_ratio(
                row[f"{prefix}_post_start_completion_rate"], started_complete, start
            )

        coverage_fields = (
            "current_complete_without_start_count",
            "baseline_complete_without_start_count",
            "current_start_without_download_count",
            "baseline_start_without_download_count",
            "current_start_without_event_match_count",
            "baseline_start_without_event_match_count",
        )
        warnings = (
            ("install_stage_coverage_risk",)
            if any(row[name] > 0 for name in coverage_fields)
            else ()
        )
        return ResultValidationOutcome(
            status="succeeded",
            candidate_count=0,
            candidates=(),
            warning_codes=warnings,
            root_delta=None,
        )

    def _validate_context(
        self, rows: list[dict[str, Any]], analysis_date: str, game_type: str
    ) -> None:
        for row in rows:
            if row.get("analysis_date") != analysis_date:
                raise ResultValidationError(
                    "schema_invalid", "result analysis_date does not match the run"
                )
            if row.get("game_type") != game_type:
                raise ResultValidationError(
                    "schema_invalid", "result game_type does not match the run"
                )

    def _validate_bucket_kinds(
        self, rows: list[dict[str, Any]], business_kind: str
    ) -> None:
        allowed = {business_kind, "quality", "residual"}
        for row in rows:
            bucket_kind = row["bucket_kind"]
            value = row["dimension_value"]
            if bucket_kind not in allowed:
                raise ResultValidationError(
                    "schema_invalid", f"unknown bucket_kind: {bucket_kind}"
                )
            if bucket_kind == "residual" and value != "__other_below_threshold__":
                raise ResultValidationError(
                    "schema_invalid", "residual bucket must use the registered value"
                )
            if bucket_kind == "quality" and not self._is_quality_value(str(value)):
                raise ResultValidationError(
                    "schema_invalid", "quality bucket uses an unregistered value"
                )
            if bucket_kind == business_kind and self._is_quality_value(str(value)):
                raise ResultValidationError(
                    "schema_invalid", "business bucket uses a registered quality value"
                )

    def _validate_metric_counts(
        self, rows: list[dict[str, Any]], *, numerator_subset: bool
    ) -> None:
        for row in rows:
            for period in ("current", "baseline"):
                denominator = row[f"{period}_denominator"]
                numerator = row[f"{period}_numerator"]
                if denominator < 0 or numerator < 0:
                    raise ResultValidationError(
                        "quality_gate_failed", "metric counts cannot be negative"
                    )
                if numerator_subset and numerator > denominator:
                    raise ResultValidationError(
                        "quality_gate_failed",
                        f"{period} numerator exceeds its denominator",
                    )

    def _validate_overall_constants_and_rehook(
        self, rows: list[dict[str, Any]]
    ) -> None:
        fields = (
            "overall_current_denominator",
            "overall_baseline_denominator",
            "overall_current_numerator",
            "overall_baseline_numerator",
        )
        for field in fields:
            values = {row[field] for row in rows}
            if len(values) != 1:
                raise ResultValidationError(
                    "result_incomplete", f"{field} is inconsistent across rows"
                )
        first = rows[0]
        mappings = (
            ("current_denominator", "overall_current_denominator"),
            ("baseline_denominator", "overall_baseline_denominator"),
            ("current_numerator", "overall_current_numerator"),
            ("baseline_numerator", "overall_baseline_numerator"),
        )
        for bucket_field, total_field in mappings:
            if sum(row[bucket_field] for row in rows) != first[total_field]:
                raise ResultValidationError(
                    "result_incomplete",
                    f"bucket {bucket_field} does not rehook {total_field}",
                )

    def _validate_source_bucket_audit(
        self, rows: list[dict[str, Any]], *, business_kind: str
    ) -> None:
        source_counts = {row["source_bucket_count"] for row in rows}
        if len(source_counts) != 1:
            raise ResultValidationError(
                "result_incomplete", "source_bucket_count is inconsistent"
            )
        source_count = next(iter(source_counts))
        collapsed_counts = [row["collapsed_source_bucket_count"] for row in rows]
        if source_count < len(rows) or any(count <= 0 for count in collapsed_counts):
            raise ResultValidationError(
                "result_incomplete", "source bucket audit contains impossible counts"
            )
        if sum(collapsed_counts) != source_count:
            raise ResultValidationError(
                "result_incomplete",
                "collapsed_source_bucket_count does not close to source_bucket_count",
            )
        if any(
            row["bucket_kind"] == business_kind
            and row["collapsed_source_bucket_count"] != 1
            for row in rows
        ):
            raise ResultValidationError(
                "result_incomplete",
                "an eligible business bucket must map to exactly one source bucket",
            )
        residuals = [row for row in rows if row["bucket_kind"] == "residual"]
        if len(residuals) > 1:
            raise ResultValidationError(
                "schema_invalid", "more than one residual bucket was returned"
            )

    def _validate_dimension_quality_rehook(
        self, rows: list[dict[str, Any]]
    ) -> None:
        first = rows[0]
        for period in ("current", "baseline"):
            matched_field = f"overall_{period}_dimension_matched_denominator"
            unmatched_field = f"overall_{period}_dimension_unmatched_denominator"
            total_field = f"overall_{period}_denominator"
            rate_field = f"overall_{period}_dimension_match_rate"
            for field in (matched_field, unmatched_field, rate_field):
                if len({row[field] for row in rows}) != 1:
                    raise ResultValidationError(
                        "result_incomplete", f"{field} is inconsistent across rows"
                    )
            matched = first[matched_field]
            unmatched = first[unmatched_field]
            total = first[total_field]
            if matched < 0 or unmatched < 0 or matched + unmatched != total:
                raise ResultValidationError(
                    "result_incomplete",
                    f"{period} dimension matched/unmatched counts do not close",
                )
            self._check_ratio(first[rate_field], matched, total)

    def _validate_metric_quality(self, rows: list[dict[str, Any]]) -> None:
        hard_fields = (
            "invalid_metric_row_count",
            "overall_invalid_metric_row_count",
            "overall_anchor_duplicate_excess",
        )
        failures = {
            field: max(row[field] for row in rows)
            for field in hard_fields
            if field in rows[0] and max(row[field] for row in rows) > 0
        }
        if failures:
            raise ResultValidationError(
                "quality_gate_failed", f"metric quality gates failed: {failures}"
            )

    def _bucket_warning_codes(self, rows: list[dict[str, Any]]) -> set[str]:
        warnings: set[str] = set()
        if any(row["bucket_kind"] == "quality" for row in rows):
            warnings.add("quality_bucket_present")
        unmatched_fields = (
            "overall_current_dimension_unmatched_denominator",
            "overall_baseline_dimension_unmatched_denominator",
        )
        if any(
            field in rows[0] and any(row[field] > 0 for row in rows)
            for field in unmatched_fields
        ):
            warnings.add("dimension_unmatched_present")
        observation_fields = (
            "overall_current_observation_days_min",
            "overall_current_observation_days_max",
            "overall_baseline_observation_days_min",
            "overall_baseline_observation_days_max",
        )
        if any(
            field in rows[0]
            and any(row[field] != 3 for row in rows)
            for field in observation_fields
        ):
            warnings.add("install_observation_window_risk")
        return warnings

    def _is_quality_value(self, value: str) -> bool:
        return any(
            fnmatch.fnmatchcase(value.lower(), pattern.lower())
            for pattern in self.contracts.result_defaults["quality_values"]
        )

    def _validate_type(
        self, row_index: int, name: str, value: Any, expected: str
    ) -> None:
        if expected == "integer_or_null" and value is None:
            return
        if expected == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif expected == "integer_or_null":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif expected == "number":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        elif expected == "string":
            valid = isinstance(value, str)
        elif expected == "date":
            valid = isinstance(value, str) and self._is_iso_date(value)
        else:
            raise ResultValidationError(
                "schema_invalid", f"unsupported registered column type: {expected}"
            )
        if not valid:
            raise ResultValidationError(
                "schema_invalid",
                f"row {row_index} column {name} must be {expected}",
            )

    def _validate_range(
        self, row_index: int, name: str, value: Any, contract: Any
    ) -> None:
        if not isinstance(contract, dict):
            raise ResultValidationError(
                "schema_invalid", f"invalid registered range for {name}"
            )
        if value is None:
            if contract.get("allow_null") is True:
                return
            raise ResultValidationError(
                "quality_gate_failed", f"row {row_index} column {name} is null"
            )
        minimum = contract.get("min")
        maximum = contract.get("max")
        if minimum is not None and value < minimum:
            raise ResultValidationError(
                "quality_gate_failed", f"row {row_index} column {name} is below range"
            )
        if maximum is not None and value > maximum:
            raise ResultValidationError(
                "quality_gate_failed", f"row {row_index} column {name} is above range"
            )

    def _check_ratio(self, actual: Any, numerator: int, denominator: int) -> None:
        expected = numerator / denominator if denominator > 0 else None
        if expected is None:
            if actual is not None:
                raise ResultValidationError(
                    "quality_gate_failed", "undefined stage rate must be null"
                )
            return
        if actual is None or not math.isclose(
            float(actual), expected, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ResultValidationError(
                "quality_gate_failed", "stage rate does not match its counts"
            )

    def _is_iso_date(self, value: str) -> bool:
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
