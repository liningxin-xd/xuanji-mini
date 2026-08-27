from __future__ import annotations

import math
from typing import Any

from .contracts import RepositoryContracts


class SecondarySelectionError(ValueError):
    pass


class SecondarySelector:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def select(
        self, state: dict[str, Any], post_primary: dict[str, Any]
    ) -> dict[str, Any]:
        game_step = next(
            (step for step in state["steps"] if step.get("id") == "game_id"),
            None,
        )
        if (
            not isinstance(game_step, dict)
            or game_step.get("status") != "succeeded"
            or not isinstance(game_step.get("candidates"), list)
            or not game_step["candidates"]
        ):
            return {
                "status": "skipped_by_policy",
                "reason": "no_legal_game_candidate",
            }

        counterfactual = next(
            (
                step
                for step in post_primary.get("steps", [])
                if step.get("id") == "counterfactual"
            ),
            None,
        )
        dominant_result = None
        if (
            isinstance(counterfactual, dict)
            and counterfactual.get("status") == "succeeded"
            and isinstance(counterfactual.get("result"), dict)
            and counterfactual["result"].get("dominant") is True
        ):
            dominant_result = counterfactual["result"]

        if dominant_result is not None:
            parent = next(
                (
                    candidate
                    for candidate in game_step["candidates"]
                    if candidate.get("value") == dominant_result.get("value")
                    and candidate.get("label") == dominant_result.get("label")
                ),
                None,
            )
            if parent is None:
                raise SecondarySelectionError(
                    "dominant counterfactual does not rehook a game candidate"
                )
            parent_reason = "dominant_counterfactual_game"
        else:
            parent = game_step["candidates"][0]
            parent_reason = "largest_legal_game_candidate"

        parent_value = parent.get("value")
        parent_label = parent.get("label") or parent_value
        if not isinstance(parent_value, str) or not parent_value.strip():
            raise SecondarySelectionError("secondary parent lacks a frozen value")
        if not isinstance(parent_label, str) or not parent_label.strip():
            raise SecondarySelectionError("secondary parent lacks a frozen label")

        children = self.contracts.secondary_relation_children(
            state["chain"], "game_id"
        )
        primary_by_id = {step["id"]: step for step in state["steps"]}
        eligible = [
            child
            for child in children
            if primary_by_id.get(child, {}).get("status") == "succeeded"
        ]
        if not eligible:
            return {
                "status": "skipped_by_policy",
                "reason": "no_eligible_secondary_child_dimension",
                "limit_code": "secondary:no_eligible_child_dimension",
            }

        with_candidates = []
        for child in eligible:
            candidates = primary_by_id[child].get("candidates")
            if not isinstance(candidates, list) or not candidates:
                continue
            adverse = candidates[0].get("adverse_impact_bp")
            if (
                isinstance(adverse, bool)
                or not isinstance(adverse, (int, float))
                or not math.isfinite(float(adverse))
            ):
                raise SecondarySelectionError(
                    f"secondary child evidence is invalid: {child}"
                )
            with_candidates.append((float(adverse), children.index(child), child))
        if with_candidates:
            child_dimension = min(
                with_candidates, key=lambda item: (-item[0], item[1])
            )[2]
            child_reason = "largest_primary_child_family_candidate"
        else:
            child_dimension = eligible[0]
            child_reason = "first_succeeded_registered_child_family"

        return {
            "status": "planned",
            "parent_dimension": "game_id",
            "parent_value": parent_value,
            "parent_label": parent_label,
            "parent_candidate_id": f"game_id:{parent_value}",
            "child_dimension": child_dimension,
            "parent_selection_reason": parent_reason,
            "child_selection_reason": child_reason,
        }
