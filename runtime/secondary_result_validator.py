from __future__ import annotations

import math
from typing import Any

from .contracts import RepositoryContracts
from .contribution import ContributionError, calculate_contributions
from .models import QueryBinding
from .result_validator import (
    ResultValidationError,
    ResultValidationOutcome,
    ResultValidator,
)


class SecondaryResultValidator:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts
        self.primary = ResultValidator(contracts)

    def validate(
        self,
        *,
        raw_result: dict[str, Any],
        binding: QueryBinding,
        chain: str,
        metric: str,
        analysis_date: str,
        game_type: str,
        parent_value: str,
        parent_counts: dict[str, Any],
        root_counts: dict[str, Any],
    ) -> ResultValidationOutcome:
        schema = self.contracts.result_schema("secondary_bucket")
        columns_by_chain = schema.get("columns_by_chain")
        columns = (
            columns_by_chain.get(chain)
            if isinstance(columns_by_chain, dict)
            else None
        )
        if not isinstance(columns, dict) or not columns:
            raise ResultValidationError(
                "schema_invalid", f"secondary schema is missing chain: {chain}"
            )
        rows = self.primary._normalize_rows(raw_result, columns, {})
        self.primary._validate_context(rows, analysis_date, game_type)
        if any(row["parent_value"] != parent_value for row in rows):
            raise ResultValidationError(
                "schema_invalid", "secondary parent identity does not match the plan"
            )
        defaults = self.contracts.result_defaults
        if any(
            row["baseline_day_count"] != int(defaults["baseline_day_count"])
            for row in rows
        ):
            raise ResultValidationError(
                "quality_gate_failed", "secondary baseline_day_count must equal 7"
            )
        self._validate_bucket_kinds(rows)
        metric_contract = self.contracts.metric_result_contract(metric)
        self.primary._validate_metric_counts(
            rows, numerator_subset=metric_contract["numerator_subset"]
        )
        self.primary._validate_overall_constants_and_rehook(rows)
        self.primary._validate_source_bucket_audit(rows, business_kind="child")
        self.primary._validate_metric_quality(rows)
        self._validate_row_audit(rows)
        self._validate_frozen_counts(rows, parent_counts, root_counts)
        if chain == "install":
            self._validate_install_window(rows, game_type)

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

        row_by_identity = {
            (row["bucket_kind"], str(row["dimension_value"])): row
            for row in rows
        }
        candidates: list[dict[str, Any]] = []
        for contribution in contributions:
            if contribution.bucket_kind != "child" or self.primary._is_quality_value(
                contribution.dimension_value
            ):
                continue
            row = row_by_identity[
                (contribution.bucket_kind, contribution.dimension_value)
            ]
            if max(
                float(row["current_denominator"]),
                float(row["baseline_denominator"])
                / float(defaults["baseline_day_count"]),
            ) < float(defaults["minimum_sample"]):
                continue
            if max(
                contribution.current_share, contribution.baseline_share
            ) < float(defaults["minimum_share"]):
                continue
            if contribution.adverse_impact + 1e-15 < float(
                defaults["minimum_adverse_impact"]
            ):
                continue
            candidate = contribution.as_candidate(binding.dimension or "secondary")
            candidate["private_counts"] = {
                "current_numerator": row["current_numerator"],
                "current_denominator": row["current_denominator"],
                "baseline_numerator": row["baseline_numerator"],
                "baseline_denominator": row["baseline_denominator"],
            }
            candidates.append(candidate)
        candidates.sort(key=lambda item: (-item["adverse_impact_bp"], item["value"]))
        candidates = candidates[:3]
        warnings = self.primary._bucket_warning_codes(rows)
        first = rows[0]
        return ResultValidationOutcome(
            status="succeeded",
            candidate_count=len(candidates),
            candidates=tuple(candidates),
            warning_codes=tuple(sorted(warnings)),
            root_current_value=(
                first["overall_current_numerator"]
                / first["overall_current_denominator"]
            ),
            root_baseline_value=(
                first["overall_baseline_numerator"]
                / first["overall_baseline_denominator"]
            ),
            root_delta=root_delta,
            root_current_numerator=float(first["overall_current_numerator"]),
            root_current_denominator=float(first["overall_current_denominator"]),
            root_baseline_numerator=float(first["overall_baseline_numerator"]),
            root_baseline_denominator=float(first["overall_baseline_denominator"]),
            family_adverse_impact_bp=sum(
                item.adverse_impact * 10000
                for item in contributions
                if item.bucket_kind != "quality"
            ),
        )

    def _validate_bucket_kinds(self, rows: list[dict[str, Any]]) -> None:
        allowed = {"child", "quality", "residual", "outside_parent"}
        outside = []
        for row in rows:
            kind = row["bucket_kind"]
            value = str(row["dimension_value"])
            if kind not in allowed:
                raise ResultValidationError(
                    "schema_invalid", f"unknown secondary bucket_kind: {kind}"
                )
            if kind == "outside_parent":
                outside.append(row)
                if value != "outside_parent":
                    raise ResultValidationError(
                        "schema_invalid", "outside_parent bucket identity changed"
                    )
            elif value == "outside_parent":
                raise ResultValidationError(
                    "schema_invalid", "outside_parent cannot be a candidate bucket"
                )
            if kind == "residual" and value != "__other_below_threshold__":
                raise ResultValidationError(
                    "schema_invalid", "secondary residual bucket identity changed"
                )
            if kind == "quality" and not self.primary._is_quality_value(value):
                raise ResultValidationError(
                    "schema_invalid", "secondary quality bucket is unregistered"
                )
            if kind == "child" and self.primary._is_quality_value(value):
                raise ResultValidationError(
                    "schema_invalid", "secondary child uses a quality identity"
                )
        if len(outside) != 1:
            raise ResultValidationError(
                "result_incomplete", "outside_parent must exist exactly once"
            )

    def _validate_row_audit(self, rows: list[dict[str, Any]]) -> None:
        first = rows[0]
        count_fields = (
            "current_row_count",
            "baseline_row_count",
            "duplicate_row_count",
            "invalid_metric_row_count",
            "overall_current_row_count",
            "overall_baseline_row_count",
            "overall_duplicate_row_count",
            "overall_invalid_metric_row_count",
        )
        if any(row[field] < 0 for row in rows for field in count_fields):
            raise ResultValidationError(
                "quality_gate_failed", "secondary row audit counts cannot be negative"
            )
        for field in (
            "overall_current_row_count",
            "overall_baseline_row_count",
            "overall_duplicate_row_count",
            "overall_invalid_metric_row_count",
        ):
            if len({row[field] for row in rows}) != 1:
                raise ResultValidationError(
                    "result_incomplete", f"{field} is inconsistent across rows"
                )
        if sum(row["current_row_count"] for row in rows) != first[
            "overall_current_row_count"
        ] or sum(row["baseline_row_count"] for row in rows) != first[
            "overall_baseline_row_count"
        ]:
            raise ResultValidationError(
                "result_incomplete", "secondary row counts do not close"
            )
        if (
            sum(row["duplicate_row_count"] for row in rows) != 0
            or first["overall_duplicate_row_count"] != 0
            or sum(row["invalid_metric_row_count"] for row in rows) != 0
            or first["overall_invalid_metric_row_count"] != 0
        ):
            raise ResultValidationError(
                "quality_gate_failed", "secondary row quality gates are non-zero"
            )

    def _validate_frozen_counts(
        self,
        rows: list[dict[str, Any]],
        parent_counts: dict[str, Any],
        root_counts: dict[str, Any],
    ) -> None:
        first = rows[0]
        mappings = {
            "current_numerator": "overall_current_numerator",
            "current_denominator": "overall_current_denominator",
            "baseline_numerator": "overall_baseline_numerator",
            "baseline_denominator": "overall_baseline_denominator",
        }
        for count_field, total_field in mappings.items():
            expected = root_counts.get(count_field)
            if not self._same_number(first[total_field], expected):
                raise ResultValidationError(
                    "result_incomplete",
                    f"secondary {total_field} does not rehook the primary root",
                )
            inside = sum(
                row[count_field]
                for row in rows
                if row["bucket_kind"] != "outside_parent"
            )
            if not self._same_number(inside, parent_counts.get(count_field)):
                raise ResultValidationError(
                    "result_incomplete",
                    f"secondary {count_field} does not rehook the parent slice",
                )

    def _validate_install_window(
        self, rows: list[dict[str, Any]], game_type: str
    ) -> None:
        expected = 3 if game_type == "app" else 1
        overall_fields = (
            "overall_current_observation_days_min",
            "overall_current_observation_days_max",
            "overall_baseline_observation_days_min",
            "overall_baseline_observation_days_max",
        )
        for field in overall_fields:
            values = {row[field] for row in rows}
            if values != {expected}:
                raise ResultValidationError(
                    "quality_gate_failed",
                    f"secondary install observation window must equal {expected}",
                )
        for row in rows:
            for period in ("current", "baseline"):
                denominator = row[f"{period}_denominator"]
                window = (
                    row[f"{period}_observation_days_min"],
                    row[f"{period}_observation_days_max"],
                )
                expected_window = (
                    (expected, expected) if denominator > 0 else (None, None)
                )
                if window != expected_window:
                    raise ResultValidationError(
                        "quality_gate_failed",
                        "secondary install bucket observation window does not "
                        f"match its {period} denominator",
                    )

    @staticmethod
    def _same_number(actual: Any, expected: Any) -> bool:
        if (
            isinstance(actual, bool)
            or isinstance(expected, bool)
            or not isinstance(actual, (int, float))
            or not isinstance(expected, (int, float))
        ):
            return False
        return math.isclose(
            float(actual), float(expected), rel_tol=0.0, abs_tol=1e-12
        )
