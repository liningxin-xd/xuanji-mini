from __future__ import annotations

import math
import re
from typing import Any

from .contracts import RepositoryContracts


class ErrorCodeSelectionError(ValueError):
    pass


class ErrorCodeSelector:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def select(
        self, state: dict[str, Any], post_primary: dict[str, Any]
    ) -> dict[str, Any]:
        chain = state.get("chain")
        game_type = state.get("game_type")
        metric = state.get("metric")
        trigger = self.contracts.error_code_trigger(chain, game_type, metric)
        if trigger["status"] == "disabled":
            return self._skip(trigger["reason"])
        if trigger["status"] != "enabled":
            raise ErrorCodeSelectionError("error-code trigger status is invalid")

        candidate_steps = [
            step
            for step in state.get("steps", [])
            if isinstance(step, dict)
            and step.get("produces_candidates") is True
            and step.get("status") == "succeeded"
            and isinstance(step.get("candidate_count"), int)
            and step["candidate_count"] > 0
            and isinstance(step.get("candidates"), list)
            and len(step["candidates"]) == step["candidate_count"]
        ]
        if not candidate_steps:
            return self._skip("no_legal_primary_candidate")

        root_counts = self._root_counts(candidate_steps)
        requirements = trigger["requirements"]
        root_adverse_delta_bp = self._root_adverse_delta_bp(state)
        minimum_delta = requirements["root_adverse_delta_bp"]["value"]
        if root_adverse_delta_bp + 1e-9 < minimum_delta:
            return self._skip("root_adverse_delta_below_threshold")
        minimum_entities = requirements["current_affected_entity_count"]["value"]
        if root_counts["current_numerator"] < minimum_entities:
            return self._skip("current_affected_entity_count_below_threshold")

        focus_game = self._focus_game(state, post_primary)
        frozen_scopes = [
            {
                "scope": "overall",
                "focus_game_id": 0,
                "current_affected_entities": root_counts["current_numerator"],
                "baseline_affected_entities": root_counts["baseline_numerator"],
                "current_business_denominator": root_counts[
                    "current_denominator"
                ],
                "baseline_business_denominator": root_counts[
                    "baseline_denominator"
                ],
            }
        ]
        if focus_game is not None:
            counts = focus_game["private_counts"]
            frozen_scopes.append(
                {
                    "scope": "focus_game",
                    "focus_game_id": int(focus_game["game_id"]),
                    "current_affected_entities": counts["current_numerator"],
                    "baseline_affected_entities": counts["baseline_numerator"],
                    "current_business_denominator": counts[
                        "current_denominator"
                    ],
                    "baseline_business_denominator": counts[
                        "baseline_denominator"
                    ],
                }
            )

        return {
            "status": "planned",
            "trigger_id": trigger["trigger_id"],
            "root_adverse_delta_bp": root_adverse_delta_bp,
            "current_affected_entity_count": root_counts["current_numerator"],
            "focus_game": focus_game,
            "frozen_scopes": frozen_scopes,
            "binding": None,
            "binding_sha256": None,
            "attempts": [],
            "facts": [],
            "limit_codes": [],
            "failure_code": None,
            "reason": None,
        }

    def _root_counts(
        self, candidate_steps: list[dict[str, Any]]
    ) -> dict[str, int]:
        fields = (
            "current_numerator",
            "current_denominator",
            "baseline_numerator",
            "baseline_denominator",
        )
        expected = None
        for step in candidate_steps:
            counts = {
                field: self._non_negative_integer(
                    step.get(f"root_{field}"), f"root_{field}"
                )
                for field in fields
            }
            if counts["current_denominator"] <= 0 or counts[
                "baseline_denominator"
            ] <= 0:
                raise ErrorCodeSelectionError(
                    "error-code root denominators must be positive"
                )
            if expected is None:
                expected = counts
            elif counts != expected:
                raise ErrorCodeSelectionError(
                    "error-code root counts differ across primary families"
                )
        if expected is None:  # pragma: no cover - guarded by caller
            raise ErrorCodeSelectionError("error-code root counts are missing")
        return expected

    def _root_adverse_delta_bp(self, state: dict[str, Any]) -> float:
        canonical = state.get("canonical_root_metric")
        if not isinstance(canonical, dict):
            raise ErrorCodeSelectionError("error-code canonical root is missing")
        delta = canonical.get("delta")
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or not math.isfinite(float(delta))
        ):
            raise ErrorCodeSelectionError("error-code root delta is invalid")
        direction = self.contracts.metric_definition(state["metric"])["direction"]
        adverse = -float(delta) if direction == "higher_is_better" else float(delta)
        return max(adverse * 10000, 0.0)

    def _focus_game(
        self, state: dict[str, Any], post_primary: dict[str, Any]
    ) -> dict[str, Any] | None:
        game_step = next(
            (step for step in state["steps"] if step.get("id") == "game_id"),
            None,
        )
        if not isinstance(game_step, dict) or game_step.get("status") != "succeeded":
            return None
        candidates = []
        for candidate in game_step.get("candidates", []):
            if not isinstance(candidate, dict):
                raise ErrorCodeSelectionError("error-code game candidate is invalid")
            value = candidate.get("value")
            if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
                continue
            label = candidate.get("label")
            impact = candidate.get("adverse_impact_bp")
            counts = candidate.get("private_counts")
            if (
                not isinstance(label, str)
                or not label.strip()
                or isinstance(impact, bool)
                or not isinstance(impact, (int, float))
                or not math.isfinite(float(impact))
                or float(impact) < 0
                or not isinstance(counts, dict)
            ):
                raise ErrorCodeSelectionError(
                    "error-code game candidate evidence is invalid"
                )
            normalized_counts = {
                field: self._non_negative_integer(counts.get(field), field)
                for field in (
                    "current_numerator",
                    "current_denominator",
                    "baseline_numerator",
                    "baseline_denominator",
                )
            }
            if normalized_counts["current_denominator"] <= 0 or normalized_counts[
                "baseline_denominator"
            ] <= 0:
                raise ErrorCodeSelectionError(
                    "error-code focus game denominators must be positive"
                )
            candidates.append(
                {
                    "candidate_id": f"game_id:{value}",
                    "game_id": value,
                    "label": label,
                    "adverse_impact_bp": float(impact),
                    "private_counts": normalized_counts,
                }
            )
        if not candidates:
            return None

        preferred_id = None
        background = next(
            (
                step
                for step in post_primary.get("steps", [])
                if isinstance(step, dict) and step.get("id") == "game_background"
            ),
            None,
        )
        if isinstance(background, dict) and background.get("selected_games"):
            preferred_id = background["selected_games"][0].get("candidate_id")
        candidates.sort(
            key=lambda item: (-item["adverse_impact_bp"], int(item["game_id"]))
        )
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate["candidate_id"] == preferred_id
            ),
            candidates[0],
        )
        return selected

    @staticmethod
    def _non_negative_integer(value: Any, field: str) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            or not float(value).is_integer()
        ):
            raise ErrorCodeSelectionError(f"error-code {field} is invalid")
        return int(value)

    @staticmethod
    def _skip(reason: str) -> dict[str, Any]:
        return {"status": "skipped_by_policy", "reason": reason}
