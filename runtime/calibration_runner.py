from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import RepositoryContracts, canonical_sha256
from .counterfactual import CounterfactualCalculator, CounterfactualError
from .game_background_selector import (
    GameBackgroundSelectionError,
    GameBackgroundSelector,
)
from .post_primary_planner import PostPrimaryPlanError, PostPrimaryPlanner
from .secondary_selector import SecondarySelectionError, SecondarySelector


class CalibrationError(ValueError):
    pass


class CalibrationRunner:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts
        self.planner = PostPrimaryPlanner(contracts)
        self.counterfactual = CounterfactualCalculator(contracts)
        self.secondary_selector = SecondarySelector(contracts)
        self.game_background_selector = GameBackgroundSelector(contracts)

    def create_plan(self, state: dict[str, Any]) -> dict[str, Any] | None:
        return self.planner.create(state)

    def execute(self, state: dict[str, Any]) -> dict[str, Any] | None:
        post_primary = state.get("post_primary")
        if post_primary is None:
            return self.create_plan(state)
        if not isinstance(post_primary, dict):
            raise CalibrationError("post-primary state must be an object")
        try:
            self.planner.validate_identity(state, post_primary)
        except PostPrimaryPlanError as exc:
            raise CalibrationError(str(exc)) from exc
        self._validate_counterfactual(state, post_primary)
        if post_primary["status"] == "completed":
            self._validate_completed(state, post_primary)
            return deepcopy(post_primary)

        result = deepcopy(post_primary)
        for step in result["steps"]:
            if step["status"] in {"in_progress", "repair_required"}:
                break
            if step["status"] != "planned":
                continue
            if step["id"] == "counterfactual":
                try:
                    outcome = self.counterfactual.calculate(state)
                except CounterfactualError as exc:
                    step.update(
                        {
                            "status": "failed",
                            "failure_code": exc.code,
                        }
                    )
                else:
                    step.update(outcome)
                continue
            if step["id"] == "secondary":
                try:
                    outcome = self.secondary_selector.select(state, result)
                except SecondarySelectionError as exc:
                    raise CalibrationError(str(exc)) from exc
                step.update(outcome)
                if step["status"] == "planned":
                    break
                continue
            if step["id"] == "game_background":
                try:
                    outcome = self.game_background_selector.select(state, result)
                except GameBackgroundSelectionError as exc:
                    raise CalibrationError(str(exc)) from exc
                step.update(outcome)
                if step["status"] == "planned":
                    break
                continue
            if step["status"] == "planned":
                raise CalibrationError(
                    f"enabled post-primary step is not implemented: {step['id']}"
                )
        if all(
            step["status"]
            in {"succeeded", "failed", "skipped_by_policy"}
            for step in result["steps"]
        ):
            result["status"] = "completed"
            self._validate_completed(state, result)
        return result

    def _validate_counterfactual(
        self, state: dict[str, Any], post_primary: dict[str, Any]
    ) -> None:
        actual = post_primary["steps"][0]
        if actual.get("status") == "planned":
            return
        expected = {"id": "counterfactual", "status": "planned"}
        try:
            outcome = self.counterfactual.calculate(state)
        except CounterfactualError as exc:
            expected.update({"status": "failed", "failure_code": exc.code})
        else:
            expected.update(outcome)
        if canonical_sha256(actual) != canonical_sha256(expected):
            raise CalibrationError(
                "counterfactual result does not match primary evidence"
            )

    def _validate_completed(
        self, state: dict[str, Any], post_primary: dict[str, Any]
    ) -> None:
        self.planner.validate_identity(state, post_primary)
        planned = self.planner.create(state)
        if planned is None:
            raise CalibrationError("primary_v1 cannot complete post-primary analysis")
        expected = deepcopy(planned)
        expected_counterfactual = expected["steps"][0]
        try:
            outcome = self.counterfactual.calculate(state)
        except CounterfactualError as exc:
            expected_counterfactual.update(
                {"status": "failed", "failure_code": exc.code}
            )
        else:
            expected_counterfactual.update(outcome)
        actual_counterfactual = post_primary["steps"][0]
        if canonical_sha256(actual_counterfactual) != canonical_sha256(
            expected_counterfactual
        ):
            raise CalibrationError(
                "counterfactual result does not match primary evidence"
            )

        expected_for_selection = deepcopy(post_primary)
        expected_for_selection["steps"][0] = expected_counterfactual
        try:
            secondary_selection = self.secondary_selector.select(
                state, expected_for_selection
            )
        except SecondarySelectionError as exc:
            raise CalibrationError(str(exc)) from exc
        actual_secondary = post_primary["steps"][1]
        selection_fields = {
            "parent_dimension",
            "parent_value",
            "parent_label",
            "parent_candidate_id",
            "child_dimension",
            "parent_selection_reason",
            "child_selection_reason",
        }
        if secondary_selection["status"] == "skipped_by_policy":
            expected_secondary = {
                "id": "secondary",
                **secondary_selection,
            }
            if canonical_sha256(actual_secondary) != canonical_sha256(
                expected_secondary
            ):
                raise CalibrationError(
                    "secondary skip does not match primary evidence"
                )
        else:
            if actual_secondary.get("status") not in {"succeeded", "failed"}:
                raise CalibrationError("completed secondary step is not terminal")
            for field in selection_fields:
                if actual_secondary.get(field) != secondary_selection.get(field):
                    raise CalibrationError(
                        f"secondary {field} does not match primary evidence"
                    )

        expected_for_background = deepcopy(post_primary)
        expected_for_background["steps"][0] = expected_counterfactual
        try:
            background_selection = self.game_background_selector.select(
                state, expected_for_background
            )
        except GameBackgroundSelectionError as exc:
            raise CalibrationError(str(exc)) from exc
        actual_background = post_primary["steps"][2]
        if background_selection["status"] == "skipped_by_policy":
            if actual_background != {
                "id": "game_background",
                **background_selection,
            }:
                raise CalibrationError(
                    "game background skip does not match primary evidence"
                )
        else:
            if actual_background.get("status") not in {"succeeded", "failed"}:
                raise CalibrationError(
                    "completed game background step is not terminal"
                )
            for field in ("selected_games",):
                if actual_background.get(field) != background_selection[field]:
                    raise CalibrationError(
                        f"game background {field} does not match primary evidence"
                    )
            items = actual_background.get("items")
            if (
                not isinstance(items, list)
                or len(items) != len(background_selection["items"])
                or actual_background.get("cursor") != len(items)
                or any(
                    not isinstance(item, dict)
                    or item.get("status") not in {"succeeded", "failed"}
                    for item in items
                )
            ):
                raise CalibrationError(
                    "completed game background items are not terminal"
                )
            has_success = any(item["status"] == "succeeded" for item in items)
            expected_status = "succeeded" if has_success else "failed"
            if actual_background["status"] != expected_status:
                raise CalibrationError(
                    "game background aggregate status is inconsistent"
                )

        if post_primary["steps"][3] != planned["steps"][3]:
            raise CalibrationError("disabled post-primary step changed")
