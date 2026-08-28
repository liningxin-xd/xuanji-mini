from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .contracts import RepositoryContracts
from .error_code_dictionary import ErrorCodeDictionary, ErrorCodeDictionaryError


class ErrorCodeValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ErrorCodeOutcome:
    facts: tuple[dict[str, Any], ...]
    limit_codes: tuple[str, ...]


class ErrorCodeResultValidator:
    BUCKET_IDENTITIES = {
        "unmatched_code": "unmatched_code",
        "residual": "__other_below_threshold__",
        "source_contract_mismatch": "__source_contract_mismatch__",
    }

    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts
        self.dictionary = ErrorCodeDictionary(contracts.root)

    def validate(
        self,
        *,
        raw_result: dict[str, Any],
        binding: Any,
        analysis_date: str,
        frozen_scopes: list[dict[str, Any]],
    ) -> ErrorCodeOutcome:
        expected_columns, quality = self.contracts.query_spec_result_contract(
            binding
        )
        rows = self._normalize_rows(raw_result, list(expected_columns))
        max_rows = quality.get("max_rows")
        row_limit = quality.get("row_limit_exclusive")
        if max_rows != 206 or row_limit != 250:
            raise ErrorCodeValidationError(
                "schema_invalid", "error-code result budget changed"
            )
        if not rows:
            raise ErrorCodeValidationError(
                "result_incomplete", "error-code query returned no rows"
            )
        if len(rows) > max_rows or len(rows) >= row_limit:
            raise ErrorCodeValidationError(
                "result_limit_exceeded",
                f"error-code query returned {len(rows)} rows",
            )

        expected_by_scope = self._expected_scopes(frozen_scopes)
        rows_by_scope: dict[tuple[str, int], list[dict[str, Any]]] = {}
        seen: set[tuple[Any, ...]] = set()
        for index, row in enumerate(rows):
            if row.get("analysis_date") != analysis_date:
                raise ErrorCodeValidationError(
                    "identity_mismatch",
                    f"row {index} analysis_date changed",
                )
            scope = self._text(row, index, "scope")
            focus_game_id = self._integer(row, index, "focus_game_id", minimum=0)
            scope_key = (scope, focus_game_id)
            expected = expected_by_scope.get(scope_key)
            if expected is None:
                raise ErrorCodeValidationError(
                    "identity_mismatch", f"row {index} scope is not frozen"
                )
            bucket_kind = self._text(row, index, "bucket_kind")
            error_code = self._text(row, index, "error_code")
            identity = (analysis_date, scope, focus_game_id, bucket_kind, error_code)
            if identity in seen:
                raise ErrorCodeValidationError(
                    "result_incomplete", "error-code bucket identity is duplicated"
                )
            seen.add(identity)
            if bucket_kind == "error_code":
                if re.fullmatch(r"[0-9]{4}", error_code) is None:
                    raise ErrorCodeValidationError(
                        "quality_gate_failed",
                        f"row {index} download error code is not four digits",
                    )
            elif self.BUCKET_IDENTITIES.get(bucket_kind) != error_code:
                raise ErrorCodeValidationError(
                    "quality_gate_failed",
                    f"row {index} error-code quality bucket is invalid",
                )
            if bucket_kind == "source_contract_mismatch":
                raise ErrorCodeValidationError(
                    "quality_gate_failed",
                    "failure behavior/action or affected entity key is inconsistent",
                )

            counts = {
                field: self._integer(row, index, field, minimum=0)
                for field in (
                    "current_error_events",
                    "baseline_error_events",
                    "current_affected_entities",
                    "baseline_affected_entities",
                    "overall_current_error_events",
                    "overall_baseline_error_events",
                    "overall_current_affected_entities",
                    "overall_baseline_affected_entities",
                )
            }
            collapsed = self._integer(
                row, index, "collapsed_source_bucket_count", minimum=1
            )
            source_count = self._integer(
                row, index, "source_bucket_count", minimum=1
            )
            if collapsed > source_count:
                raise ErrorCodeValidationError(
                    "contribution_not_closed",
                    f"row {index} collapsed source bucket count is invalid",
                )
            if row.get("baseline_day_count") != 7:
                raise ErrorCodeValidationError(
                    "result_incomplete", f"row {index} baseline day count changed"
                )
            for field in (
                "current_business_denominator",
                "baseline_business_denominator",
            ):
                actual = self._integer(row, index, field, minimum=1)
                if actual != expected[field]:
                    raise ErrorCodeValidationError(
                        "identity_mismatch", f"row {index} {field} changed"
                    )
            for field in (
                "overall_current_affected_entities",
                "overall_baseline_affected_entities",
            ):
                expected_field = field.removeprefix("overall_")
                if counts[field] != expected[expected_field]:
                    raise ErrorCodeValidationError(
                        "result_incomplete", f"row {index} {field} does not rehook"
                    )
            if counts["current_error_events"] < counts[
                "current_affected_entities"
            ] or counts["baseline_error_events"] < counts[
                "baseline_affected_entities"
            ]:
                raise ErrorCodeValidationError(
                    "quality_gate_failed",
                    f"row {index} error events are below affected entities",
                )
            self._validate_ratio(
                row,
                index,
                "current_entity_rate",
                counts["current_affected_entities"],
                expected["current_business_denominator"],
            )
            self._validate_ratio(
                row,
                index,
                "baseline_entity_rate",
                counts["baseline_affected_entities"],
                expected["baseline_business_denominator"],
            )
            self._validate_optional_ratio(
                row,
                index,
                "current_repeats_per_entity",
                counts["current_error_events"],
                counts["current_affected_entities"],
            )
            self._validate_optional_ratio(
                row,
                index,
                "baseline_repeats_per_entity",
                counts["baseline_error_events"],
                counts["baseline_affected_entities"],
            )
            rows_by_scope.setdefault(scope_key, []).append(
                {**row, **counts, "collapsed_source_bucket_count": collapsed}
            )

        if set(rows_by_scope) != set(expected_by_scope):
            raise ErrorCodeValidationError(
                "result_incomplete", "error-code result omitted a frozen scope"
            )
        for scope_key, scope_rows in rows_by_scope.items():
            self._validate_scope_closure(scope_key, scope_rows)

        code_rows = [row for row in rows if row["bucket_kind"] == "error_code"]
        frozen_codes = tuple(sorted({row["error_code"] for row in code_rows}))
        try:
            annotations, dictionary_limits = self.dictionary.annotate_frozen_codes(
                frozen_codes
            )
        except ErrorCodeDictionaryError as exc:
            raise ErrorCodeValidationError("quality_gate_failed", str(exc)) from exc

        facts = []
        for row in code_rows:
            current_repeats = row["current_repeats_per_entity"]
            baseline_repeats = row["baseline_repeats_per_entity"]
            repeat_change_ratio = (
                float(current_repeats) / float(baseline_repeats)
                if isinstance(current_repeats, (int, float))
                and not isinstance(current_repeats, bool)
                and isinstance(baseline_repeats, (int, float))
                and not isinstance(baseline_repeats, bool)
                and float(baseline_repeats) > 0
                else None
            )
            fact = {
                "scope": row["scope"],
                "code": row["error_code"],
                "meaning_status": annotations[row["error_code"]][
                    "meaning_status"
                ],
                "current_affected_entities": row[
                    "current_affected_entities"
                ],
                "current_entity_rate": row["current_entity_rate"],
                "baseline_entity_rate": row["baseline_entity_rate"],
                "entity_rate_delta_bp": (
                    float(row["current_entity_rate"])
                    - float(row["baseline_entity_rate"])
                )
                * 10000,
                "current_repeats_per_entity": current_repeats,
                "baseline_repeats_per_entity": baseline_repeats,
                "repeat_change_ratio": repeat_change_ratio,
            }
            if row["scope"] == "focus_game":
                fact["focus_game_id"] = row["focus_game_id"]
            facts.append(fact)
        facts.sort(
            key=lambda fact: (
                -fact["entity_rate_delta_bp"],
                -fact["current_affected_entities"],
                0 if fact["scope"] == "overall" else 1,
                fact["code"],
            )
        )
        overall_facts = [fact for fact in facts if fact["scope"] == "overall"]
        focus_facts = [
            fact for fact in facts if fact["scope"] == "focus_game"
        ]
        selected_facts = overall_facts[:3] + focus_facts[:2]

        limit_codes = list(dictionary_limits)
        if not code_rows:
            limit_codes.append("no_code_above_threshold")
        if any(row["bucket_kind"] == "unmatched_code" for row in rows):
            limit_codes.append("unmatched_code_present")
        if any(row["bucket_kind"] == "residual" for row in rows):
            limit_codes.append("below_threshold_codes_collapsed")
        return ErrorCodeOutcome(
            tuple(selected_facts), tuple(dict.fromkeys(limit_codes))
        )

    @staticmethod
    def _expected_scopes(
        frozen_scopes: list[dict[str, Any]],
    ) -> dict[tuple[str, int], dict[str, Any]]:
        if (
            not isinstance(frozen_scopes, list)
            or not 1 <= len(frozen_scopes) <= 2
        ):
            raise ErrorCodeValidationError(
                "schema_invalid", "frozen error-code scopes are invalid"
            )
        required_fields = {
            "scope",
            "focus_game_id",
            "current_affected_entities",
            "baseline_affected_entities",
            "current_business_denominator",
            "baseline_business_denominator",
        }
        result: dict[tuple[str, int], dict[str, Any]] = {}
        for scope in frozen_scopes:
            if not isinstance(scope, dict) or set(scope) != required_fields:
                raise ErrorCodeValidationError(
                    "schema_invalid", "frozen error-code scope is invalid"
                )
            scope_name = scope["scope"]
            focus_game_id = scope["focus_game_id"]
            if scope_name not in {"overall", "focus_game"} or (
                isinstance(focus_game_id, bool)
                or not isinstance(focus_game_id, int)
                or (scope_name == "overall" and focus_game_id != 0)
                or (scope_name == "focus_game" and focus_game_id <= 0)
            ):
                raise ErrorCodeValidationError(
                    "schema_invalid", "frozen error-code scope identity is invalid"
                )
            for field in required_fields - {"scope", "focus_game_id"}:
                value = scope[field]
                minimum = 1 if field.endswith("business_denominator") else 0
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < minimum
                ):
                    raise ErrorCodeValidationError(
                        "schema_invalid", f"frozen error-code {field} is invalid"
                    )
            if (
                scope["current_affected_entities"]
                > scope["current_business_denominator"]
                or scope["baseline_affected_entities"]
                > scope["baseline_business_denominator"]
            ):
                raise ErrorCodeValidationError(
                    "schema_invalid",
                    "frozen affected entities exceed the business denominator",
                )
            key = (scope_name, focus_game_id)
            if key in result:
                raise ErrorCodeValidationError(
                    "schema_invalid", "frozen error-code scope is duplicated"
                )
            result[key] = scope
        if ("overall", 0) not in result or (
            len(result) == 2
            and not any(scope == "focus_game" for scope, _ in result)
        ):
            raise ErrorCodeValidationError(
                "schema_invalid", "frozen error-code scopes lack overall identity"
            )
        return result

    @staticmethod
    def _normalize_rows(
        raw_result: dict[str, Any], expected_columns: list[str]
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_result, dict) or raw_result.get("columns") != (
            expected_columns
        ):
            raise ErrorCodeValidationError(
                "schema_invalid",
                "raw result columns must match the error-code QuerySpec",
            )
        rows = raw_result.get("rows")
        if not isinstance(rows, list):
            raise ErrorCodeValidationError(
                "schema_invalid", "raw result rows must be an array"
            )
        normalized = []
        for index, row in enumerate(rows):
            if isinstance(row, list) and len(row) == len(expected_columns):
                normalized.append(dict(zip(expected_columns, row, strict=True)))
            elif isinstance(row, dict) and set(row) == set(expected_columns):
                normalized.append({name: row[name] for name in expected_columns})
            else:
                raise ErrorCodeValidationError(
                    "schema_invalid", f"row {index} does not match QuerySpec"
                )
        return normalized

    @staticmethod
    def _text(row: dict[str, Any], index: int, field: str) -> str:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ErrorCodeValidationError(
                "schema_invalid", f"row {index} {field} must be non-empty"
            )
        return value

    @staticmethod
    def _integer(
        row: dict[str, Any], index: int, field: str, *, minimum: int
    ) -> int:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ErrorCodeValidationError(
                "quality_gate_failed",
                f"row {index} {field} must be an integer >= {minimum}",
            )
        return value

    @staticmethod
    def _validate_ratio(
        row: dict[str, Any], index: int, field: str, numerator: int, denominator: int
    ) -> None:
        value = row.get(field)
        expected = numerator / denominator
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not math.isclose(float(value), expected, rel_tol=0.0, abs_tol=1e-9)
        ):
            raise ErrorCodeValidationError(
                "contribution_not_closed", f"row {index} {field} is inconsistent"
            )

    @staticmethod
    def _validate_optional_ratio(
        row: dict[str, Any], index: int, field: str, numerator: int, denominator: int
    ) -> None:
        value = row.get(field)
        if denominator == 0:
            if value is not None:
                raise ErrorCodeValidationError(
                    "contribution_not_closed",
                    f"row {index} {field} must be null",
                )
            return
        ErrorCodeResultValidator._validate_ratio(
            row, index, field, numerator, denominator
        )

    @staticmethod
    def _validate_scope_closure(
        scope_key: tuple[str, int], rows: list[dict[str, Any]]
    ) -> None:
        repeated_fields = (
            "source_bucket_count",
            "overall_current_error_events",
            "overall_baseline_error_events",
            "overall_current_affected_entities",
            "overall_baseline_affected_entities",
        )
        for field in repeated_fields:
            values = {row[field] for row in rows}
            if len(values) != 1:
                raise ErrorCodeValidationError(
                    "result_incomplete", f"{scope_key} {field} is inconsistent"
                )
        first = rows[0]
        if sum(row["collapsed_source_bucket_count"] for row in rows) != first[
            "source_bucket_count"
        ]:
            raise ErrorCodeValidationError(
                "contribution_not_closed", f"{scope_key} source buckets do not close"
            )
        if sum(row["current_error_events"] for row in rows) != first[
            "overall_current_error_events"
        ] or sum(row["baseline_error_events"] for row in rows) != first[
            "overall_baseline_error_events"
        ]:
            raise ErrorCodeValidationError(
                "contribution_not_closed", f"{scope_key} error events do not close"
            )
        if first["overall_current_error_events"] < first[
            "overall_current_affected_entities"
        ] or first["overall_baseline_error_events"] < first[
            "overall_baseline_affected_entities"
        ]:
            raise ErrorCodeValidationError(
                "quality_gate_failed",
                f"{scope_key} total error events are below affected entities",
            )
