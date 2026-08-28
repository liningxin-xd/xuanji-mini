from __future__ import annotations

import math
from typing import Any

from .contracts import RepositoryContracts


class BreadthSelectionError(ValueError):
    pass


class BreadthSelector:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def select(self, state: dict[str, Any]) -> dict[str, Any]:
        policy = self.contracts.breadth_check_policy()
        direction = self.contracts.metric_definition(state["metric"])["direction"]
        steps = state.get("steps")
        if not isinstance(steps, list):
            raise BreadthSelectionError("breadth check primary steps are invalid")

        calibrations = []
        for dimension in policy["eligible_dimensions"]:
            step = next(
                (
                    item
                    for item in steps
                    if isinstance(item, dict) and item.get("id") == dimension
                ),
                None,
            )
            if not isinstance(step, dict) or step.get("status") != "succeeded":
                continue
            candidates = step.get("candidates")
            buckets = step.get("breadth_buckets")
            if (
                not isinstance(candidates, list)
                or len(candidates) != step.get("candidate_count")
                or not isinstance(buckets, list)
            ):
                raise BreadthSelectionError(
                    f"breadth check evidence is invalid: {dimension}"
                )
            normalized_buckets = [
                self._bucket(item, direction=direction, dimension=dimension)
                for item in buckets
            ]
            normalized_buckets.sort(
                key=lambda item: (-item["adverse_rate_change_bp"], item["value"])
            )
            focus_candidates = sorted(
                candidates,
                key=lambda item: (-float(item["adverse_impact_bp"]), item["value"]),
            )[: policy["max_focus_candidates_per_family"]]
            for focus in focus_candidates:
                focus_bucket = next(
                    (
                        item
                        for item in normalized_buckets
                        if item["value"] == focus.get("value")
                        and item["label"] == focus.get("label")
                    ),
                    None,
                )
                if focus_bucket is None:
                    raise BreadthSelectionError(
                        f"breadth focus no longer matches its bucket: {dimension}"
                    )
                focus_change = focus_bucket["adverse_rate_change_bp"]
                if focus_change <= 0:
                    continue
                minimum_change = (
                    focus_change * policy["minimum_relative_rate_change"]
                )
                supporting = [
                    item
                    for item in normalized_buckets
                    if item["value"] != focus_bucket["value"]
                    and item["adverse_rate_change_bp"] > 0
                    and item["adverse_rate_change_bp"] + 1e-9 >= minimum_change
                ]
                if len(supporting) < policy["minimum_other_buckets"]:
                    continue
                calibrations.append(
                    {
                        "candidate_id": f"{dimension}:{focus_bucket['value']}",
                        "dimension": dimension,
                        "value": focus_bucket["value"],
                        "label": focus_bucket["label"],
                        "specificity_status": "broad_change",
                        "focus_adverse_rate_change_bp": focus_change,
                        "minimum_relative_rate_change": policy[
                            "minimum_relative_rate_change"
                        ],
                        "supporting_bucket_count": len(supporting),
                        "supporting_buckets": supporting[
                            : policy["max_supporting_buckets"]
                        ],
                    }
                )

        if not calibrations:
            return {
                "status": "skipped_by_policy",
                "reason": "no_broad_primary_family",
            }
        return {
            "status": "succeeded",
            "query_count": 0,
            "calibrations": calibrations,
        }

    def _bucket(
        self, value: Any, *, direction: str, dimension: str
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "value",
            "label",
            "current_rate",
            "baseline_rate",
        }:
            raise BreadthSelectionError(
                f"breadth bucket shape is invalid: {dimension}"
            )
        bucket_value = value["value"]
        label = value["label"]
        current = value["current_rate"]
        baseline = value["baseline_rate"]
        if (
            not isinstance(bucket_value, str)
            or not bucket_value.strip()
            or not isinstance(label, str)
            or not label.strip()
            or any(
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(float(rate))
                or not 0 <= float(rate) <= 1
                for rate in (current, baseline)
            )
        ):
            raise BreadthSelectionError(
                f"breadth bucket value or rate is invalid: {dimension}"
            )
        if direction == "higher_is_better":
            adverse_change = float(baseline) - float(current)
        elif direction == "lower_is_better":
            adverse_change = float(current) - float(baseline)
        else:  # pragma: no cover - validated metric contract
            raise BreadthSelectionError(
                f"breadth metric direction is invalid: {direction}"
            )
        return {
            "value": bucket_value,
            "label": label,
            "adverse_rate_change_bp": max(adverse_change * 10000, 0.0),
        }
