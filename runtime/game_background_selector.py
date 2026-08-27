from __future__ import annotations

import math
import re
from typing import Any

from .contracts import RepositoryContracts


class GameBackgroundSelectionError(ValueError):
    pass


class GameBackgroundSelector:
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
        ):
            return self._skip("no_legal_game_candidate")

        legal_candidates = []
        for candidate in game_step["candidates"]:
            if not isinstance(candidate, dict):
                raise GameBackgroundSelectionError(
                    "game background candidate must be an object"
                )
            value = candidate.get("value")
            if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
                continue
            label = candidate.get("label") or value
            adverse_impact = candidate.get("adverse_impact_bp")
            if (
                not isinstance(label, str)
                or not label.strip()
                or isinstance(adverse_impact, bool)
                or not isinstance(adverse_impact, (int, float))
                or not math.isfinite(float(adverse_impact))
                or float(adverse_impact) < 0
            ):
                raise GameBackgroundSelectionError(
                    "game background candidate evidence is invalid"
                )
            legal_candidates.append(
                {
                    "candidate_id": f"game_id:{value}",
                    "game_id": value,
                    "label": label,
                    "adverse_impact_bp": float(adverse_impact),
                }
            )
        if not legal_candidates:
            return self._skip("no_legal_game_candidate")

        legal_candidates.sort(
            key=lambda item: (-item["adverse_impact_bp"], int(item["game_id"]))
        )
        dominant = self._dominant_counterfactual(post_primary)
        if dominant is not None:
            selected = next(
                (
                    candidate
                    for candidate in legal_candidates
                    if candidate["candidate_id"] == dominant.get("candidate_id")
                    and candidate["game_id"] == dominant.get("value")
                    and candidate["label"] == dominant.get("label")
                ),
                None,
            )
            if selected is None:
                return self._skip("no_legal_game_candidate")
            policy = self.contracts.game_background_policy()
            if policy["dominant_counterfactual_max_games"] != 1:
                raise GameBackgroundSelectionError(
                    "dominant game background policy must select one game"
                )
            return self._planned([selected], "dominant_counterfactual")

        root_adverse_bp = self._root_adverse_bp(state, game_step)
        if root_adverse_bp <= 0:
            return self._skip("non_positive_root_adverse_delta")
        policy = self.contracts.game_background_policy()
        max_games = policy["max_games"]
        ratio = policy["cumulative_root_adverse_ratio"]
        threshold = root_adverse_bp * ratio
        selected = []
        cumulative = 0.0
        for candidate in legal_candidates[:max_games]:
            selected.append(candidate)
            cumulative += candidate["adverse_impact_bp"]
            if cumulative + 1e-9 >= threshold:
                return self._planned(
                    selected, "cumulative_root_adverse_ratio"
                )
        return self._skip("game_candidates_do_not_reach_background_threshold")

    def _root_adverse_bp(
        self, state: dict[str, Any], game_step: dict[str, Any]
    ) -> float:
        canonical = state.get("canonical_root_metric")
        root_delta = (
            canonical.get("delta")
            if isinstance(canonical, dict)
            else game_step.get("root_delta")
        )
        if (
            isinstance(root_delta, bool)
            or not isinstance(root_delta, (int, float))
            or not math.isfinite(float(root_delta))
        ):
            raise GameBackgroundSelectionError(
                "game background root adverse delta is invalid"
            )
        direction = self.contracts.metric_definition(state["metric"])["direction"]
        adverse = -float(root_delta) if direction == "higher_is_better" else float(
            root_delta
        )
        return max(adverse * 10000, 0.0)

    @staticmethod
    def _dominant_counterfactual(
        post_primary: dict[str, Any],
    ) -> dict[str, Any] | None:
        counterfactual = next(
            (
                step
                for step in post_primary.get("steps", [])
                if step.get("id") == "counterfactual"
            ),
            None,
        )
        if (
            isinstance(counterfactual, dict)
            and counterfactual.get("status") == "succeeded"
            and isinstance(counterfactual.get("result"), dict)
            and counterfactual["result"].get("dominant") is True
        ):
            return counterfactual["result"]
        return None

    @staticmethod
    def _skip(reason: str) -> dict[str, Any]:
        return {"status": "skipped_by_policy", "reason": reason}

    @staticmethod
    def _planned(
        candidates: list[dict[str, Any]], selection_reason: str
    ) -> dict[str, Any]:
        games = [
            {
                "candidate_id": candidate["candidate_id"],
                "game_id": candidate["game_id"],
                "label": candidate["label"],
                "selection_reason": selection_reason,
            }
            for candidate in candidates
        ]
        return {
            "status": "planned",
            "cursor": 0,
            "selected_games": games,
            "items": [
                {
                    "id": "game_background",
                    "item_index": index,
                    **game,
                    "status": "planned",
                    "binding": None,
                    "binding_sha256": None,
                    "attempts": [],
                    "facts": [],
                    "limit_codes": [],
                    "failure_code": None,
                    "reason": None,
                }
                for index, game in enumerate(games)
            ],
        }
