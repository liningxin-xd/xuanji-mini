from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .contribution import ContributionError, calculate_contributions
from .contracts import RepositoryContracts


class CrossDimensionOverlapValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CrossDimensionOverlapOutcome:
    facts: tuple[dict[str, Any], ...]
    limit_codes: tuple[str, ...] = ()


class CrossDimensionOverlapResultValidator:
    COUNT_FIELDS = (
        "current_denominator",
        "baseline_denominator",
        "current_numerator",
        "baseline_numerator",
    )
    AUDIT_FIELDS = (
        "current_row_count",
        "baseline_row_count",
        "duplicate_row_count",
        "invalid_metric_row_count",
    )

    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def validate(
        self,
        *,
        raw_result: dict[str, Any],
        binding: Any,
        metric: str,
        analysis_date: str,
        game_type: str,
        frozen_candidates: list[dict[str, Any]],
        frozen_root_counts: dict[str, Any],
    ) -> CrossDimensionOverlapOutcome:
        columns, quality = self.contracts.query_spec_result_contract(binding)
        rows = self._normalize_rows(raw_result, list(columns))
        policy = self.contracts.cross_dimension_overlap_policy()
        if quality != {
            "max_rows": 4,
            "row_limit_exclusive": 250,
            "baseline_day_count": 7,
            "marginal_rehook_tolerance": policy["marginal_rehook_tolerance"],
            "required_quadrants": policy["quadrants"],
            "duplicate_row_count_must_equal": 0,
            "invalid_metric_row_count_must_equal": 0,
        }:
            raise CrossDimensionOverlapValidationError(
                "schema_invalid", "overlap result quality contract changed"
            )
        if len(rows) != quality["max_rows"]:
            raise CrossDimensionOverlapValidationError(
                "result_incomplete", "overlap result must contain four quadrants"
            )

        candidates = self._expected_candidates(frozen_candidates, policy)
        root_counts = self._expected_root_counts(frozen_root_counts)
        left_game_id = int(candidates[0]["value"])
        right_reserve_value = int(candidates[1]["value"])
        numerator_subset = self.contracts.metric_result_contract(metric)[
            "numerator_subset"
        ]
        normalized: dict[str, dict[str, Any]] = {}
        repeated: dict[str, int] = {}
        for index, row in enumerate(rows):
            if (
                row.get("analysis_date") != analysis_date
                or row.get("game_type") != game_type
                or row.get("left_game_id") != left_game_id
                or row.get("right_reserve_value") != right_reserve_value
            ):
                raise CrossDimensionOverlapValidationError(
                    "identity_mismatch", f"row {index} overlap identity changed"
                )
            quadrant = row.get("quadrant")
            if quadrant not in policy["quadrants"] or quadrant in normalized:
                raise CrossDimensionOverlapValidationError(
                    "result_incomplete", f"row {index} quadrant is invalid"
                )
            if row.get("baseline_day_count") != quality["baseline_day_count"]:
                raise CrossDimensionOverlapValidationError(
                    "result_incomplete", f"row {index} baseline days changed"
                )
            values = {
                field: self._integer(row, index, field)
                for field in (*self.COUNT_FIELDS, *self.AUDIT_FIELDS)
            }
            overall = {
                field: self._integer(row, index, f"overall_{field}")
                for field in (*self.COUNT_FIELDS, *self.AUDIT_FIELDS)
            }
            if numerator_subset and (
                values["current_numerator"] > values["current_denominator"]
                or values["baseline_numerator"] > values["baseline_denominator"]
                or overall["current_numerator"]
                > overall["current_denominator"]
                or overall["baseline_numerator"]
                > overall["baseline_denominator"]
            ):
                raise CrossDimensionOverlapValidationError(
                    "quality_gate_failed", "overlap numerator exceeds denominator"
                )
            if values["duplicate_row_count"] != 0 or values[
                "invalid_metric_row_count"
            ] != 0:
                raise CrossDimensionOverlapValidationError(
                    "quality_gate_failed",
                    f"row {index} overlap grain or metric audit failed",
                )
            if not repeated:
                repeated = overall
            elif repeated != overall:
                raise CrossDimensionOverlapValidationError(
                    "result_incomplete", "overlap root totals are inconsistent"
                )
            normalized[quadrant] = {**row, **values, **{
                f"overall_{field}": value for field, value in overall.items()
            }}

        if set(normalized) != set(policy["quadrants"]):
            raise CrossDimensionOverlapValidationError(
                "result_incomplete", "overlap result omitted a required quadrant"
            )
        for field in self.COUNT_FIELDS:
            if repeated[field] != root_counts[field]:
                raise CrossDimensionOverlapValidationError(
                    "result_incomplete", f"overlap root {field} does not rehook"
                )
        if repeated["current_denominator"] <= 0 or repeated[
            "baseline_denominator"
        ] <= 0:
            raise CrossDimensionOverlapValidationError(
                "quality_gate_failed", "overlap root denominators must be positive"
            )
        for field in (*self.COUNT_FIELDS, *self.AUDIT_FIELDS):
            if sum(row[field] for row in normalized.values()) != repeated[field]:
                raise CrossDimensionOverlapValidationError(
                    "contribution_not_closed", f"overlap {field} does not close"
                )
        if repeated["duplicate_row_count"] != 0 or repeated[
            "invalid_metric_row_count"
        ] != 0:
            raise CrossDimensionOverlapValidationError(
                "quality_gate_failed", "overlap aggregate quality audit failed"
            )

        left_quadrants = {"BOTH", "LEFT_ONLY"}
        right_quadrants = {"BOTH", "RIGHT_ONLY"}
        self._validate_candidate_counts(
            normalized, left_quadrants, candidates[0]
        )
        self._validate_candidate_counts(
            normalized, right_quadrants, candidates[1]
        )

        contribution_rows = [
            self._contribution_row(normalized[quadrant], quadrant, repeated)
            for quadrant in policy["quadrants"]
        ]
        direction = self.contracts.metric_definition(metric)["direction"]
        tolerance = float(policy["marginal_rehook_tolerance"])
        try:
            contributions, _ = calculate_contributions(
                contribution_rows,
                direction=direction,
                tolerance=tolerance,
            )
        except ContributionError as exc:
            raise CrossDimensionOverlapValidationError(
                "contribution_not_closed", str(exc)
            ) from exc
        self._validate_candidate_impact(
            normalized,
            left_quadrants,
            candidates[0],
            repeated,
            direction,
            tolerance,
        )
        self._validate_candidate_impact(
            normalized,
            right_quadrants,
            candidates[1],
            repeated,
            direction,
            tolerance,
        )

        by_quadrant = {item.dimension_value: item for item in contributions}
        facts = []
        for quadrant in policy["quadrants"]:
            item = by_quadrant[quadrant]
            row = normalized[quadrant]
            facts.append(
                {
                    "quadrant": quadrant,
                    "current_sample": row["current_denominator"],
                    "baseline_sample": row["baseline_denominator"],
                    "current_rate": item.current_rate,
                    "baseline_rate": item.baseline_rate,
                    "current_share": item.current_share,
                    "baseline_share": item.baseline_share,
                    "total_impact_bp": item.total_impact * 10000,
                    "adverse_impact_bp": item.adverse_impact * 10000,
                }
            )
        return CrossDimensionOverlapOutcome(tuple(facts))

    @staticmethod
    def _expected_candidates(
        value: Any, policy: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) != 2:
            raise CrossDimensionOverlapValidationError(
                "schema_invalid", "frozen overlap candidates are invalid"
            )
        expected_dimensions = (
            policy["left_dimension"],
            policy["right_dimension"],
        )
        for candidate, dimension in zip(value, expected_dimensions, strict=True):
            if (
                not isinstance(candidate, dict)
                or candidate.get("dimension") != dimension
                or candidate.get("candidate_id")
                != f"{dimension}:{candidate.get('value')}"
                or not isinstance(candidate.get("value"), str)
                or not isinstance(candidate.get("label"), str)
                or not candidate["label"].strip()
            ):
                raise CrossDimensionOverlapValidationError(
                    "schema_invalid", "frozen overlap candidate identity is invalid"
                )
            for field in ("total_impact_bp", "adverse_impact_bp"):
                impact = candidate.get(field)
                if (
                    isinstance(impact, bool)
                    or not isinstance(impact, (int, float))
                    or not math.isfinite(float(impact))
                ):
                    raise CrossDimensionOverlapValidationError(
                        "schema_invalid", "frozen overlap impact is invalid"
                    )
        return value

    @classmethod
    def _expected_root_counts(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict) or set(value) != set(cls.COUNT_FIELDS):
            raise CrossDimensionOverlapValidationError(
                "schema_invalid", "frozen overlap root counts are invalid"
            )
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in value.values()
        ):
            raise CrossDimensionOverlapValidationError(
                "schema_invalid", "frozen overlap root counts are invalid"
            )
        return value

    @classmethod
    def _validate_candidate_counts(
        cls,
        rows: dict[str, dict[str, Any]],
        included: set[str],
        candidate: dict[str, Any],
    ) -> None:
        counts = candidate.get("private_counts")
        if not isinstance(counts, dict) or set(counts) != set(cls.COUNT_FIELDS):
            raise CrossDimensionOverlapValidationError(
                "schema_invalid", "frozen overlap candidate counts are invalid"
            )
        actual = {
            field: sum(rows[quadrant][field] for quadrant in included)
            for field in cls.COUNT_FIELDS
        }
        if actual != counts:
            raise CrossDimensionOverlapValidationError(
                "contribution_not_closed",
                f"overlap candidate counts do not rehook: {candidate['candidate_id']}",
            )

    @classmethod
    def _validate_candidate_impact(
        cls,
        rows: dict[str, dict[str, Any]],
        included: set[str],
        candidate: dict[str, Any],
        totals: dict[str, int],
        direction: str,
        tolerance: float,
    ) -> None:
        selected = {
            field: sum(rows[quadrant][field] for quadrant in included)
            for field in cls.COUNT_FIELDS
        }
        outside = {field: totals[field] - selected[field] for field in cls.COUNT_FIELDS}
        marginal_rows = [
            cls._aggregate_contribution_row("selected", selected, totals),
            cls._aggregate_contribution_row("outside", outside, totals),
        ]
        try:
            contributions, _ = calculate_contributions(
                marginal_rows, direction=direction, tolerance=tolerance
            )
        except ContributionError as exc:
            raise CrossDimensionOverlapValidationError(
                "contribution_not_closed", str(exc)
            ) from exc
        selected_impact = contributions[0]
        expected_total = float(candidate["total_impact_bp"]) / 10000
        expected_adverse = float(candidate["adverse_impact_bp"]) / 10000
        if not math.isclose(
            selected_impact.total_impact,
            expected_total,
            rel_tol=0.0,
            abs_tol=tolerance,
        ) or not math.isclose(
            selected_impact.adverse_impact,
            expected_adverse,
            rel_tol=0.0,
            abs_tol=tolerance,
        ):
            raise CrossDimensionOverlapValidationError(
                "contribution_not_closed",
                f"overlap marginal impact does not rehook: {candidate['candidate_id']}",
            )

    @classmethod
    def _contribution_row(
        cls, row: dict[str, Any], quadrant: str, totals: dict[str, int]
    ) -> dict[str, Any]:
        return {
            "dimension_value": quadrant,
            "dimension_label": quadrant,
            "bucket_kind": "quadrant",
            **{field: row[field] for field in cls.COUNT_FIELDS},
            **{f"overall_{field}": totals[field] for field in cls.COUNT_FIELDS},
        }

    @classmethod
    def _aggregate_contribution_row(
        cls, identity: str, counts: dict[str, int], totals: dict[str, int]
    ) -> dict[str, Any]:
        return {
            "dimension_value": identity,
            "dimension_label": identity,
            "bucket_kind": identity,
            **counts,
            **{f"overall_{field}": totals[field] for field in cls.COUNT_FIELDS},
        }

    @staticmethod
    def _normalize_rows(
        raw_result: dict[str, Any], expected_columns: list[str]
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_result, dict) or raw_result.get("columns") != (
            expected_columns
        ):
            raise CrossDimensionOverlapValidationError(
                "schema_invalid", "raw result columns must match overlap QuerySpec"
            )
        rows = raw_result.get("rows")
        if not isinstance(rows, list):
            raise CrossDimensionOverlapValidationError(
                "schema_invalid", "raw overlap rows must be an array"
            )
        result = []
        for index, row in enumerate(rows):
            if isinstance(row, list) and len(row) == len(expected_columns):
                result.append(dict(zip(expected_columns, row, strict=True)))
            elif isinstance(row, dict) and set(row) == set(expected_columns):
                result.append({name: row[name] for name in expected_columns})
            else:
                raise CrossDimensionOverlapValidationError(
                    "schema_invalid", f"row {index} does not match overlap QuerySpec"
                )
        return result

    @staticmethod
    def _integer(row: dict[str, Any], index: int, field: str) -> int:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CrossDimensionOverlapValidationError(
                "quality_gate_failed",
                f"row {index} {field} must be a non-negative integer",
            )
        return value
