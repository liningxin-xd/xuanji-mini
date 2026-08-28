from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

from .contracts import RepositoryContracts


class CrossDimensionOverlapSelectionError(ValueError):
    pass


class CrossDimensionOverlapSelector:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def select(self, state: dict[str, Any]) -> dict[str, Any]:
        policy = self.contracts.cross_dimension_overlap_policy()
        if state.get("chain") != "download":
            return self._skip("unsupported_chain")

        left_dimension = policy["left_dimension"]
        right_dimension = policy["right_dimension"]
        steps = state.get("steps")
        if not isinstance(steps, list):
            raise CrossDimensionOverlapSelectionError(
                "cross-dimension overlap primary steps are invalid"
            )
        by_id = {
            step.get("id"): step
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("id"), str)
        }
        left_step = by_id.get(left_dimension)
        right_step = by_id.get(right_dimension)
        if any(
            not isinstance(step, dict) or step.get("status") != "succeeded"
            for step in (left_step, right_step)
        ):
            return self._skip("required_primary_family_unavailable")

        left = self._top_candidate(left_step, left_dimension)
        right = self._top_candidate(right_step, right_dimension)
        if left is None or right is None:
            return self._skip("required_primary_candidate_missing")
        if re.fullmatch(r"[1-9][0-9]*", left["value"]) is None:
            raise CrossDimensionOverlapSelectionError(
                "overlap game candidate must be a positive integer"
            )
        if right["value"] not in {"0", "1"}:
            raise CrossDimensionOverlapSelectionError(
                "overlap reserve candidate must be binary"
            )

        root_counts = self._root_counts(left_step, right_step)
        root_adverse_delta_bp = self._root_adverse_delta_bp(state)
        minimum_bp = max(
            float(policy["minimum_candidate_adverse_impact"]) * 10000,
            root_adverse_delta_bp * float(policy["root_adverse_ratio"]),
        )
        weaker_bp = min(
            float(left["adverse_impact_bp"]),
            float(right["adverse_impact_bp"]),
        )
        if weaker_bp + 1e-9 < minimum_bp:
            return self._skip("weaker_candidate_below_overlap_threshold")

        frozen_candidates = [
            self._freeze_candidate(left, left_dimension),
            self._freeze_candidate(right, right_dimension),
        ]
        return {
            "status": "planned",
            "selection_id": "download_game_reserve_overlap_v1",
            "root_adverse_delta_bp": root_adverse_delta_bp,
            "minimum_candidate_adverse_impact_bp": minimum_bp,
            "frozen_root_counts": root_counts,
            "frozen_candidates": frozen_candidates,
            "binding": None,
            "binding_sha256": None,
            "attempts": [],
            "facts": [],
            "limit_codes": [],
            "failure_code": None,
            "reason": None,
        }

    def _top_candidate(
        self, step: dict[str, Any], dimension: str
    ) -> dict[str, Any] | None:
        candidates = step.get("candidates")
        count = step.get("candidate_count")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(candidates, list)
            or len(candidates) != count
        ):
            raise CrossDimensionOverlapSelectionError(
                f"overlap candidate evidence is invalid: {dimension}"
            )
        if not candidates:
            return None
        for candidate in candidates:
            self._validate_candidate(candidate, dimension)
        return min(
            candidates,
            key=lambda item: (-float(item["adverse_impact_bp"]), item["value"]),
        )

    @staticmethod
    def _validate_candidate(candidate: Any, dimension: str) -> None:
        if not isinstance(candidate, dict):
            raise CrossDimensionOverlapSelectionError(
                f"overlap candidate is invalid: {dimension}"
            )
        if (
            candidate.get("dimension") != dimension
            or not isinstance(candidate.get("value"), str)
            or not candidate["value"].strip()
            or not isinstance(candidate.get("label"), str)
            or not candidate["label"].strip()
        ):
            raise CrossDimensionOverlapSelectionError(
                f"overlap candidate identity is invalid: {dimension}"
            )
        for field in ("total_impact_bp", "adverse_impact_bp"):
            value = candidate.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or (field == "adverse_impact_bp" and float(value) < 0)
            ):
                raise CrossDimensionOverlapSelectionError(
                    f"overlap candidate impact is invalid: {dimension}"
                )
        counts = candidate.get("private_counts")
        if not isinstance(counts, dict) or set(counts) != {
            "current_numerator",
            "current_denominator",
            "baseline_numerator",
            "baseline_denominator",
        }:
            raise CrossDimensionOverlapSelectionError(
                f"overlap candidate counts are invalid: {dimension}"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in counts.values()
        ):
            raise CrossDimensionOverlapSelectionError(
                f"overlap candidate counts are invalid: {dimension}"
            )

    @staticmethod
    def _freeze_candidate(
        candidate: dict[str, Any], dimension: str
    ) -> dict[str, Any]:
        return {
            "candidate_id": f"{dimension}:{candidate['value']}",
            "dimension": dimension,
            "value": candidate["value"],
            "label": candidate["label"],
            "total_impact_bp": candidate["total_impact_bp"],
            "adverse_impact_bp": candidate["adverse_impact_bp"],
            "private_counts": deepcopy(candidate["private_counts"]),
        }

    def _root_counts(
        self, left_step: dict[str, Any], right_step: dict[str, Any]
    ) -> dict[str, int]:
        fields = (
            "current_numerator",
            "current_denominator",
            "baseline_numerator",
            "baseline_denominator",
        )
        values = []
        for step in (left_step, right_step):
            counts = {}
            for field in fields:
                value = step.get(f"root_{field}")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0
                    or not float(value).is_integer()
                ):
                    raise CrossDimensionOverlapSelectionError(
                        "overlap root counts are invalid"
                    )
                counts[field] = int(value)
            if counts["current_denominator"] <= 0 or counts[
                "baseline_denominator"
            ] <= 0:
                raise CrossDimensionOverlapSelectionError(
                    "overlap root denominators must be positive"
                )
            values.append(counts)
        if values[0] != values[1]:
            raise CrossDimensionOverlapSelectionError(
                "overlap root counts differ across primary families"
            )
        return values[0]

    def _root_adverse_delta_bp(self, state: dict[str, Any]) -> float:
        root = state.get("canonical_root_metric")
        delta = root.get("delta") if isinstance(root, dict) else None
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or not math.isfinite(float(delta))
        ):
            raise CrossDimensionOverlapSelectionError(
                "overlap canonical root is invalid"
            )
        direction = self.contracts.metric_definition(state["metric"])["direction"]
        adverse = -float(delta) if direction == "higher_is_better" else float(delta)
        return max(adverse * 10000, 0.0)

    @staticmethod
    def _skip(reason: str) -> dict[str, str]:
        return {"status": "skipped_by_policy", "reason": reason}
