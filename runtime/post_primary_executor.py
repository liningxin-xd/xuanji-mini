from __future__ import annotations

from typing import Any

from .contracts import RepositoryContracts, canonical_sha256
from .cross_dimension_overlap_result_validator import (
    CrossDimensionOverlapResultValidator,
    CrossDimensionOverlapValidationError,
)
from .error_code_result_validator import (
    ErrorCodeResultValidator,
    ErrorCodeValidationError,
)
from .game_background_validator import (
    GameBackgroundValidationError,
    GameBackgroundValidator,
)
from .models import BuiltQuery, QueryBinding, StepStatus
from .query_builder import QueryBuilder
from .result_validator import ResultValidationError
from .secondary_query_builder import SecondaryQueryBuilder
from .secondary_result_validator import SecondaryResultValidator


class PostPrimaryExecutionError(ValueError):
    pass


class PostPrimaryExecutor:
    def __init__(
        self,
        contracts: RepositoryContracts,
        *,
        query_builder: QueryBuilder,
        secondary_query_builder: SecondaryQueryBuilder,
        secondary_result_validator: SecondaryResultValidator,
        game_background_validator: GameBackgroundValidator,
        error_code_result_validator: ErrorCodeResultValidator,
        cross_dimension_overlap_result_validator: (
            CrossDimensionOverlapResultValidator
        ),
    ):
        self.contracts = contracts
        self.query_builder = query_builder
        self.secondary_query_builder = secondary_query_builder
        self.secondary_result_validator = secondary_result_validator
        self.game_background_validator = game_background_validator
        self.error_code_result_validator = error_code_result_validator
        self.cross_dimension_overlap_result_validator = (
            cross_dimension_overlap_result_validator
        )

    def current_query(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        post_primary = state.get("post_primary")
        if not isinstance(post_primary, dict) or post_primary.get("status") != (
            "executing"
        ):
            return None
        for step in post_primary.get("steps", []):
            if not isinstance(step, dict):
                continue
            if step.get("id") == "secondary" and step.get("status") in {
                "planned",
                "in_progress",
                "repair_required",
            }:
                return step, step
            if step.get("id") == "error_code" and step.get("status") in {
                "planned",
                "in_progress",
                "repair_required",
            }:
                return step, step
            if step.get("id") == "cross_dimension_overlap" and step.get(
                "status"
            ) in {
                "planned",
                "in_progress",
                "repair_required",
            }:
                return step, step
            if step.get("id") != "game_background" or step.get("status") not in {
                "planned",
                "in_progress",
            }:
                continue
            items = step.get("items")
            cursor = step.get("cursor")
            if (
                isinstance(items, list)
                and isinstance(cursor, int)
                and 0 <= cursor < len(items)
                and isinstance(items[cursor], dict)
                and items[cursor].get("status")
                in {"planned", "in_progress", "repair_required"}
            ):
                return step, items[cursor]
        return None

    def active_query(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        current = self.current_query(state)
        if current is None or current[1].get("status") not in {
            "in_progress",
            "repair_required",
        }:
            return None
        return current

    def prepare_query(
        self,
        state: dict[str, Any],
        post_step: dict[str, Any],
        query_item: dict[str, Any],
    ) -> BuiltQuery:
        if post_step["id"] == "secondary":
            built = self.secondary_query_builder.build(
                chain=state["chain"],
                metric=state["metric"],
                business_date=state["analysis_date"],
                game_type=state["game_type"],
                parent_dimension=query_item["parent_dimension"],
                parent_value=query_item["parent_value"],
                child_dimension=query_item["child_dimension"],
            )
        elif post_step["id"] == "game_background":
            binding = self.contracts.game_background_binding(
                game_id=int(query_item["game_id"])
            )
            built = self.query_builder.build(
                binding,
                {
                    "business_date": state["analysis_date"],
                    "game_id": int(query_item["game_id"]),
                },
            )
        elif post_step["id"] == "error_code":
            binding = self.error_code_binding(query_item)
            built = self.query_builder.build(
                binding,
                {
                    "business_date": state["analysis_date"],
                    **binding.dimension_config["query_parameters"],
                },
            )
        elif post_step["id"] == "cross_dimension_overlap":
            binding = self.cross_dimension_overlap_binding(query_item, state)
            built = self.query_builder.build(
                binding,
                {
                    "business_date": state["analysis_date"],
                    "game_type": state["game_type"],
                    **binding.dimension_config["query_parameters"],
                },
            )
        else:  # pragma: no cover - guarded by the plan contract
            raise PostPrimaryExecutionError(
                f"unsupported post-primary query step: {post_step['id']}"
            )

        binding_snapshot = self.binding_snapshot(built.binding)
        if post_step["id"] == "secondary":
            query_item.update(
                {
                    "binding": binding_snapshot,
                    "binding_sha256": canonical_sha256(binding_snapshot),
                    "attempts": [],
                    "candidate_count": None,
                    "candidates": [],
                    "root_current_value": None,
                    "root_baseline_value": None,
                    "root_delta": None,
                    "root_current_numerator": None,
                    "root_current_denominator": None,
                    "root_baseline_numerator": None,
                    "root_baseline_denominator": None,
                    "family_adverse_impact_bp": None,
                    "failure_code": None,
                    "reason": None,
                    "warning_codes": [],
                }
            )
        else:
            query_item["binding"] = binding_snapshot
            query_item["binding_sha256"] = canonical_sha256(binding_snapshot)
        return built

    @staticmethod
    def issue_query(
        post_step: dict[str, Any],
        query_item: dict[str, Any],
        built: BuiltQuery,
        sql_path: str,
    ) -> None:
        query_item["attempts"].append(
            {
                "attempt_no": 0,
                "status": "issued",
                "sql_sha256": built.sha256,
                "sql_path": sql_path,
                "query_id": None,
                "error": None,
                "event_path": None,
                "raw_result_sha256": None,
                "validation": None,
            }
        )
        query_item["status"] = StepStatus.IN_PROGRESS.value
        post_step["status"] = StepStatus.IN_PROGRESS.value

    def record_returned(
        self,
        *,
        state: dict[str, Any],
        post_step: dict[str, Any],
        query_item: dict[str, Any],
        attempt: dict[str, Any],
        binding: QueryBinding,
        raw_result: dict[str, Any],
    ) -> None:
        try:
            outcome = self._validate_result(
                state=state,
                post_step=post_step,
                query_item=query_item,
                binding=binding,
                raw_result=raw_result,
            )
        except (
            ResultValidationError,
            GameBackgroundValidationError,
            ErrorCodeValidationError,
            CrossDimensionOverlapValidationError,
        ) as exc:
            attempt["status"] = "failed"
            attempt["validation"] = {
                "status": "failed",
                "failure_code": exc.code,
                "reason": str(exc),
            }
            query_item["status"] = StepStatus.FAILED.value
            query_item["failure_code"] = exc.code
            query_item["reason"] = f"{exc.code}: {exc}"
            return

        attempt["status"] = "succeeded"
        query_item["status"] = StepStatus.SUCCEEDED.value
        query_item["failure_code"] = None
        query_item["reason"] = None
        if post_step["id"] in {
            "game_background",
            "error_code",
            "cross_dimension_overlap",
        }:
            attempt["validation"] = {
                "status": "succeeded",
                "fact_count": len(outcome.facts),
                "limit_codes": list(outcome.limit_codes),
            }
            query_item["facts"] = list(outcome.facts)
            query_item["limit_codes"] = list(outcome.limit_codes)
            return

        attempt["validation"] = {
            "status": "succeeded",
            "candidate_count": outcome.candidate_count,
            "warning_codes": list(outcome.warning_codes),
            "root_current_value": outcome.root_current_value,
            "root_baseline_value": outcome.root_baseline_value,
            "root_delta": outcome.root_delta,
        }
        query_item["candidate_count"] = outcome.candidate_count
        query_item["candidates"] = list(outcome.candidates)
        query_item["root_current_value"] = outcome.root_current_value
        query_item["root_baseline_value"] = outcome.root_baseline_value
        query_item["root_delta"] = outcome.root_delta
        query_item["root_current_numerator"] = outcome.root_current_numerator
        query_item["root_current_denominator"] = outcome.root_current_denominator
        query_item["root_baseline_numerator"] = outcome.root_baseline_numerator
        query_item["root_baseline_denominator"] = outcome.root_baseline_denominator
        query_item["family_adverse_impact_bp"] = outcome.family_adverse_impact_bp
        query_item["warning_codes"] = list(outcome.warning_codes)

    def record_error(
        self,
        *,
        query_item: dict[str, Any],
        attempt: dict[str, Any],
        attempt_no: int,
        raw_error: dict[str, str],
    ) -> bool:
        attempt["status"] = "error"
        attempt["error"] = raw_error
        if raw_error["class"] == "semantic_analysis" and attempt_no < 2:
            query_item["status"] = StepStatus.REPAIR_REQUIRED.value
            return False

        failure_code = self.query_failure_code(raw_error)
        reason = self.query_failure_reason(raw_error, attempt_no)
        query_item["status"] = StepStatus.FAILED.value
        query_item["failure_code"] = failure_code
        query_item["reason"] = reason
        attempt["validation"] = {
            "status": "failed",
            "failure_code": failure_code,
            "reason": reason,
        }
        return True

    @staticmethod
    def advance_background(
        post_step: dict[str, Any], query_item: dict[str, Any]
    ) -> None:
        items = post_step.get("items")
        cursor = post_step.get("cursor")
        if (
            post_step.get("id") != "game_background"
            or not isinstance(items, list)
            or not isinstance(cursor, int)
            or cursor >= len(items)
            or items[cursor] is not query_item
            or query_item.get("status") not in {"succeeded", "failed"}
        ):
            raise PostPrimaryExecutionError("game background cursor cannot advance")
        post_step["cursor"] += 1
        if post_step["cursor"] < len(items):
            post_step["status"] = "in_progress"
            return
        if any(item.get("status") == "succeeded" for item in items):
            post_step["status"] = "succeeded"
            post_step.pop("failure_code", None)
            post_step.pop("reason", None)
        else:
            post_step["status"] = "failed"
            post_step["failure_code"] = "game_background_unavailable"
            post_step["reason"] = "all selected game background queries failed"

    def error_code_binding(self, step: dict[str, Any]) -> QueryBinding:
        try:
            scopes = self.error_code_result_validator._expected_scopes(
                step.get("frozen_scopes")
            )
        except ErrorCodeValidationError as exc:
            raise PostPrimaryExecutionError(str(exc)) from exc
        overall = scopes[("overall", 0)]
        focus_entry = next(
            (
                (key, value)
                for key, value in scopes.items()
                if key[0] == "focus_game"
            ),
            None,
        )
        focus_game_id = focus_entry[0][1] if focus_entry is not None else 0
        focus = focus_entry[1] if focus_entry is not None else None
        return self.contracts.error_code_binding(
            focus_game_id=focus_game_id,
            overall_current_business_denominator=overall[
                "current_business_denominator"
            ],
            overall_baseline_business_denominator=overall[
                "baseline_business_denominator"
            ],
            focus_current_business_denominator=(
                focus["current_business_denominator"] if focus is not None else 0
            ),
            focus_baseline_business_denominator=(
                focus["baseline_business_denominator"] if focus is not None else 0
            ),
        )

    def cross_dimension_overlap_binding(
        self, step: dict[str, Any], state: dict[str, Any]
    ) -> QueryBinding:
        candidates = step.get("frozen_candidates")
        if not isinstance(candidates, list) or len(candidates) != 2:
            raise PostPrimaryExecutionError(
                "overlap step lacks two frozen candidates"
            )
        left, right = candidates
        try:
            return self.contracts.cross_dimension_overlap_binding(
                metric=state["metric"],
                left_game_id=int(left["value"]),
                right_reserve_value=int(right["value"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PostPrimaryExecutionError(
                "overlap frozen candidate identity is invalid"
            ) from exc

    @staticmethod
    def binding_snapshot(binding: QueryBinding) -> dict[str, Any]:
        return {
            "asset_path": binding.asset_path,
            "asset_sha256": binding.asset_sha256,
            "asset_kind": binding.asset_kind,
            "data_sources": list(binding.data_sources),
            "protected_tokens": list(binding.protected_tokens),
            "required_predicates": list(binding.required_predicates),
            "result_schema_id": binding.result_schema_id,
            "dimension": binding.dimension,
            "dimension_config": binding.dimension_config,
        }

    @staticmethod
    def path_id(step: dict[str, Any]) -> str:
        item_index = step.get("item_index")
        if isinstance(item_index, int):
            return f"{step['id']}-{item_index:02d}"
        return step["id"]

    @staticmethod
    def query_failure_code(raw_error: dict[str, str]) -> str:
        combined = " ".join(raw_error.values()).lower()
        if any(
            marker in combined
            for marker in ("permission", "access denied", "unauthorized", "forbidden")
        ):
            return "query_blocked"
        return "query_failed"

    @staticmethod
    def query_failure_reason(raw_error: dict[str, str], attempt_no: int) -> str:
        repair_suffix = (
            " after two evidence-based repairs"
            if raw_error["class"] == "semantic_analysis" and attempt_no == 2
            else ""
        )
        return (
            f"{raw_error['class']} {raw_error['code']}{repair_suffix}: "
            f"{raw_error['message']}"
        )

    def secondary_parent_counts(
        self, state: dict[str, Any], secondary: dict[str, Any]
    ) -> dict[str, Any]:
        game_step = next(step for step in state["steps"] if step["id"] == "game_id")
        candidate = next(
            (
                item
                for item in game_step["candidates"]
                if item.get("value") == secondary["parent_value"]
                and item.get("label") == secondary["parent_label"]
            ),
            None,
        )
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("private_counts"), dict
        ):
            raise PostPrimaryExecutionError(
                "secondary parent no longer rehooks its game candidate"
            )
        return dict(candidate["private_counts"])

    @staticmethod
    def secondary_root_counts(state: dict[str, Any]) -> dict[str, Any]:
        game_step = next(step for step in state["steps"] if step["id"] == "game_id")
        return {
            "current_numerator": game_step["root_current_numerator"],
            "current_denominator": game_step["root_current_denominator"],
            "baseline_numerator": game_step["root_baseline_numerator"],
            "baseline_denominator": game_step["root_baseline_denominator"],
        }

    def _validate_result(
        self,
        *,
        state: dict[str, Any],
        post_step: dict[str, Any],
        query_item: dict[str, Any],
        binding: QueryBinding,
        raw_result: dict[str, Any],
    ) -> Any:
        if post_step["id"] == "secondary":
            return self.secondary_result_validator.validate(
                raw_result=raw_result,
                binding=binding,
                chain=state["chain"],
                metric=state["metric"],
                analysis_date=state["analysis_date"],
                game_type=state["game_type"],
                parent_value=query_item["parent_value"],
                parent_counts=self.secondary_parent_counts(state, query_item),
                root_counts=self.secondary_root_counts(state),
            )
        if post_step["id"] == "game_background":
            return self.game_background_validator.validate(
                raw_result=raw_result,
                binding=binding,
                analysis_date=state["analysis_date"],
                game_id=int(query_item["game_id"]),
            )
        if post_step["id"] == "error_code":
            return self.error_code_result_validator.validate(
                raw_result=raw_result,
                binding=binding,
                analysis_date=state["analysis_date"],
                frozen_scopes=query_item["frozen_scopes"],
            )
        if post_step["id"] == "cross_dimension_overlap":
            return self.cross_dimension_overlap_result_validator.validate(
                raw_result=raw_result,
                binding=binding,
                metric=state["metric"],
                analysis_date=state["analysis_date"],
                game_type=state["game_type"],
                frozen_candidates=query_item["frozen_candidates"],
                frozen_root_counts=query_item["frozen_root_counts"],
            )
        raise PostPrimaryExecutionError(
            f"unsupported post-primary query step: {post_step['id']}"
        )
