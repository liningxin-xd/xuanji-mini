from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .contracts import RepositoryContracts


class GameBackgroundValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GameBackgroundOutcome:
    facts: tuple[dict[str, Any], ...]
    limit_codes: tuple[str, ...]


class GameBackgroundValidator:
    LIFECYCLE_KINDS = {
        "download_open",
        "reservation_open",
        "playable_open",
    }
    OPERATION_KINDS = {"incident", "update"}
    TRANSITION_EVIDENCE = {
        "operation_event",
        "observed_state_transition",
        "registered_lifecycle_date_only",
    }

    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def validate(
        self,
        *,
        raw_result: dict[str, Any],
        binding: Any,
        analysis_date: str,
        game_id: int,
    ) -> GameBackgroundOutcome:
        expected_columns, quality = self.contracts.query_spec_result_contract(
            binding
        )
        rows = self._normalize_rows(raw_result, list(expected_columns))
        max_rows = quality.get("max_rows")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int):
            raise GameBackgroundValidationError(
                "schema_invalid", "game background max_rows is invalid"
            )
        if len(rows) > max_rows:
            raise GameBackgroundValidationError(
                "result_limit_exceeded",
                f"game background returned {len(rows)} rows; max_rows={max_rows}",
            )
        target_date = self._date(analysis_date, "analysis_date")
        if not rows:
            return GameBackgroundOutcome((), ("no_registered_event",))

        normalized: list[dict[str, Any]] = []
        limit_codes: list[str] = []
        for index, row in enumerate(rows):
            if row.get("analysis_date") != analysis_date:
                raise GameBackgroundValidationError(
                    "identity_mismatch",
                    f"row {index} analysis_date does not match the investigation",
                )
            app_id = row.get("app_id")
            if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id != game_id:
                raise GameBackgroundValidationError(
                    "identity_mismatch",
                    f"row {index} app_id does not match the frozen game",
                )
            event_kind = self._text(row, index, "event_kind")
            event_title = self._text(row, index, "event_title")
            source = self._text(row, index, "source")
            transition = self._text(row, index, "transition_evidence")
            if transition not in self.TRANSITION_EVIDENCE:
                raise GameBackgroundValidationError(
                    "quality_gate_failed",
                    f"row {index} transition_evidence is not registered",
                )
            if transition == "operation_event":
                if event_kind not in self.OPERATION_KINDS:
                    raise GameBackgroundValidationError(
                        "quality_gate_failed",
                        f"row {index} operation event kind is invalid",
                    )
            elif event_kind not in self.LIFECYCLE_KINDS:
                raise GameBackgroundValidationError(
                    "quality_gate_failed",
                    f"row {index} lifecycle event kind is invalid",
                )
            elif source != "game_detail_lifecycle":
                raise GameBackgroundValidationError(
                    "quality_gate_failed",
                    f"row {index} lifecycle source is invalid",
                )

            event_date0 = self._date(row.get("event_date0"), "event_date0")
            event_date1 = self._date(row.get("event_date1"), "event_date1")
            if event_date1 < event_date0 or event_date0 > target_date:
                raise GameBackgroundValidationError(
                    "temporal_evidence_invalid",
                    f"row {index} contains a future or reversed event interval",
                )
            baseline_start = target_date - timedelta(days=7)
            if event_date1 < baseline_start:
                raise GameBackgroundValidationError(
                    "temporal_evidence_invalid",
                    f"row {index} does not overlap the comparison window",
                )
            if transition != "operation_event" and event_date0 < baseline_start:
                raise GameBackgroundValidationError(
                    "temporal_evidence_invalid",
                    f"row {index} lifecycle event starts before the comparison window",
                )
            self._non_negative_integer(row, index, "days_before_analysis")
            expected_days = (target_date - event_date0).days
            if row.get("days_before_analysis") != expected_days:
                raise GameBackgroundValidationError(
                    "temporal_evidence_invalid",
                    f"row {index} days_before_analysis is inconsistent",
                )
            expected_relation = self._temporal_relation(
                event_date0, target_date, baseline_start
            )
            if row.get("temporal_relation") != expected_relation:
                raise GameBackgroundValidationError(
                    "temporal_evidence_invalid",
                    f"row {index} temporal_relation is inconsistent",
                )
            self._non_negative_integer(row, index, "event_priority")
            self._non_negative_integer(row, index, "event_type")
            self._non_negative_integer(row, index, "impact_score1")
            if not 1 <= row["event_priority"] <= 4:
                raise GameBackgroundValidationError(
                    "quality_gate_failed",
                    f"row {index} event_priority is outside 1..4",
                )

            snapshot = row.get("source_snapshot_dt")
            if snapshot is None:
                limit_codes.append("snapshot_missing")
                continue
            self._date(snapshot, "source_snapshot_dt")
            normalized.append(
                {
                    **row,
                    "event_date0": event_date0.isoformat(),
                    "event_date1": event_date1.isoformat(),
                }
            )

        normalized = self._deduplicate_registered_events(normalized)
        normalized = self._prefer_observed_lifecycle(normalized)
        normalized = self._suppress_derived_playable_duplicate(normalized)
        if any(
            row["transition_evidence"] == "registered_lifecycle_date_only"
            for row in normalized
        ):
            limit_codes.append("state_transition_not_directly_observed")
        normalized.sort(
            key=lambda row: (
                row["event_priority"],
                -date.fromisoformat(row["event_date0"]).toordinal(),
                -row["impact_score1"],
                row["event_title"],
                row["source"],
            )
        )
        facts = []
        for row in normalized:
            fact = {
                "event_kind": row["event_kind"],
                "event_date": row["event_date0"],
                "temporal_relation": row["temporal_relation"],
            }
            if row["transition_evidence"] != "operation_event":
                fact["transition_evidence"] = row["transition_evidence"]
            if fact not in facts:
                facts.append(fact)
            if len(facts) == 4:
                break
        return GameBackgroundOutcome(
            tuple(facts), tuple(dict.fromkeys(limit_codes))
        )

    @staticmethod
    def _normalize_rows(
        raw_result: dict[str, Any], expected_columns: list[str]
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_result, dict):
            raise GameBackgroundValidationError(
                "schema_invalid", "raw_result must be an object"
            )
        if raw_result.get("columns") != expected_columns:
            raise GameBackgroundValidationError(
                "schema_invalid",
                "raw_result columns must exactly match the game background QuerySpec",
            )
        rows = raw_result.get("rows")
        if not isinstance(rows, list):
            raise GameBackgroundValidationError(
                "schema_invalid", "raw_result.rows must be an array"
            )
        normalized = []
        for index, row in enumerate(rows):
            if isinstance(row, list):
                if len(row) != len(expected_columns):
                    raise GameBackgroundValidationError(
                        "schema_invalid",
                        f"row {index} does not match the registered column count",
                    )
                normalized.append(dict(zip(expected_columns, row, strict=True)))
            elif isinstance(row, dict) and set(row) == set(expected_columns):
                normalized.append({name: row[name] for name in expected_columns})
            else:
                raise GameBackgroundValidationError(
                    "schema_invalid",
                    f"row {index} does not match the registered columns",
                )
        return normalized

    @staticmethod
    def _date(value: Any, field: str) -> date:
        if not isinstance(value, str):
            raise GameBackgroundValidationError(
                "schema_invalid", f"{field} must be YYYY-MM-DD"
            )
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise GameBackgroundValidationError(
                "schema_invalid", f"{field} must be YYYY-MM-DD"
            ) from exc
        if parsed.isoformat() != value:
            raise GameBackgroundValidationError(
                "schema_invalid", f"{field} must be YYYY-MM-DD"
            )
        return parsed

    @staticmethod
    def _text(row: dict[str, Any], index: int, field: str) -> str:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise GameBackgroundValidationError(
                "schema_invalid", f"row {index} {field} must be non-empty"
            )
        return value

    @staticmethod
    def _non_negative_integer(
        row: dict[str, Any], index: int, field: str
    ) -> None:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GameBackgroundValidationError(
                "quality_gate_failed",
                f"row {index} {field} must be a non-negative integer",
            )

    @staticmethod
    def _temporal_relation(
        event_date: date, target_date: date, baseline_start: date
    ) -> str:
        if event_date < baseline_start:
            return "active_from_before_baseline"
        if event_date == target_date:
            return "same_day"
        if event_date == target_date - timedelta(days=1):
            return "one_day_before"
        return "within_baseline"

    @staticmethod
    def _deduplicate_registered_events(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["analysis_date"],
                row["app_id"],
                row["event_kind"],
                row["event_title"],
                row["event_date0"],
            )
            if row["transition_evidence"] != "operation_event":
                key += (row["transition_evidence"],)
            deduplicated.setdefault(key, row)
        return list(deduplicated.values())

    @staticmethod
    def _prefer_observed_lifecycle(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        observed = {
            (row["app_id"], row["event_kind"], row["event_date0"])
            for row in rows
            if row["transition_evidence"] == "observed_state_transition"
        }
        return [
            row
            for row in rows
            if row["transition_evidence"] != "registered_lifecycle_date_only"
            or (row["app_id"], row["event_kind"], row["event_date0"])
            not in observed
        ]

    @staticmethod
    def _suppress_derived_playable_duplicate(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        download_days = {
            (row["app_id"], row["event_date0"])
            for row in rows
            if row["event_kind"] == "download_open"
        }
        return [
            row
            for row in rows
            if not (
                row["event_kind"] == "playable_open"
                and (row["app_id"], row["event_date0"]) in download_days
                and row.get("is_android_download_enable") == 1
            )
        ]
