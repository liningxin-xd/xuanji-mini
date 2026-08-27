from __future__ import annotations

import math
from typing import Any

from .contracts import RepositoryContracts


class CounterfactualError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CounterfactualCalculator:
    TRIGGER_SHARE = 0.50
    DOMINANCE_RESTORATION = 0.50
    ROOT_TOLERANCE = 0.0005

    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def calculate(self, state: dict[str, Any]) -> dict[str, Any]:
        game_step = next(
            (step for step in state["steps"] if step.get("id") == "game_id"),
            None,
        )
        if (
            not isinstance(game_step, dict)
            or game_step.get("status") != "succeeded"
            or not game_step.get("candidates")
        ):
            return {
                "status": "skipped_by_policy",
                "reason": "no_legal_game_candidate",
            }

        candidate = game_step["candidates"][0]
        candidate_adverse_bp = self._finite_non_negative(
            candidate.get("adverse_impact_bp"), "candidate adverse impact"
        )
        family_adverse_bp = self._finite_non_negative(
            game_step.get("family_adverse_impact_bp"), "family adverse impact"
        )
        if family_adverse_bp <= 0:
            raise CounterfactualError(
                "invalid_family_adverse_impact",
                "game family adverse impact must be positive when a candidate exists",
            )
        family_share = candidate_adverse_bp / family_adverse_bp
        root_delta = self._finite(game_step.get("root_delta"), "root delta")
        direction = self.contracts.metric_definition(state["metric"])["direction"]
        root_adverse_bp = max(
            (-root_delta if direction == "higher_is_better" else root_delta) * 10000,
            0.0,
        )
        trigger_reasons = []
        if family_share + 1e-12 >= self.TRIGGER_SHARE:
            trigger_reasons.append("family_adverse_share_at_least_50pct")
        if candidate_adverse_bp + 1e-9 >= root_adverse_bp:
            trigger_reasons.append("impact_at_least_root_net_adverse_change")
        if not trigger_reasons:
            return {
                "status": "skipped_by_policy",
                "reason": "counterfactual_trigger_not_met",
            }

        counts = candidate.get("private_counts")
        if not isinstance(counts, dict):
            raise CounterfactualError(
                "missing_candidate_counts",
                "game candidate lacks frozen numerator and denominator counts",
            )
        root_current_numerator = self._finite_non_negative(
            game_step.get("root_current_numerator"), "root current numerator"
        )
        root_current_denominator = self._finite_non_negative(
            game_step.get("root_current_denominator"), "root current denominator"
        )
        root_baseline_numerator = self._finite_non_negative(
            game_step.get("root_baseline_numerator"), "root baseline numerator"
        )
        root_baseline_denominator = self._finite_non_negative(
            game_step.get("root_baseline_denominator"), "root baseline denominator"
        )
        slice_current_numerator = self._finite_non_negative(
            counts.get("current_numerator"), "slice current numerator"
        )
        slice_current_denominator = self._finite_non_negative(
            counts.get("current_denominator"), "slice current denominator"
        )
        slice_baseline_numerator = self._finite_non_negative(
            counts.get("baseline_numerator"), "slice baseline numerator"
        )
        slice_baseline_denominator = self._finite_non_negative(
            counts.get("baseline_denominator"), "slice baseline denominator"
        )

        current_denominator_without = (
            root_current_denominator - slice_current_denominator
        )
        baseline_denominator_without = (
            root_baseline_denominator - slice_baseline_denominator
        )
        if current_denominator_without <= 0 or baseline_denominator_without <= 0:
            return {
                "status": "skipped_by_policy",
                "reason": "non_positive_remaining_denominator",
                "limit_code": "counterfactual:non_positive_remaining_denominator",
            }
        current_numerator_without = root_current_numerator - slice_current_numerator
        baseline_numerator_without = root_baseline_numerator - slice_baseline_numerator
        if current_numerator_without < 0 or baseline_numerator_without < 0:
            raise CounterfactualError(
                "negative_remaining_numerator",
                "candidate numerator exceeds the frozen root numerator",
            )
        if math.isclose(root_delta, 0.0, rel_tol=0.0, abs_tol=1e-12):
            return {
                "status": "skipped_by_policy",
                "reason": "zero_root_delta",
                "limit_code": "counterfactual:zero_root_delta",
            }

        current_without = current_numerator_without / current_denominator_without
        baseline_without = baseline_numerator_without / baseline_denominator_without
        removal_delta = current_without - baseline_without
        restoration_ratio = 1 - abs(removal_delta) / abs(root_delta)
        dominance_reasons = []
        if restoration_ratio + 1e-12 >= self.DOMINANCE_RESTORATION:
            dominance_reasons.append("absolute_anomaly_reduced_at_least_50pct")
        if abs(removal_delta) <= self.ROOT_TOLERANCE + 1e-12:
            dominance_reasons.append("within_5bp_tolerance")
        if root_delta * removal_delta < 0:
            dominance_reasons.append("direction_reversed")
        dominant = bool(dominance_reasons)
        label = candidate.get("label") or candidate.get("value")
        if not isinstance(label, str) or not label.strip():
            raise CounterfactualError(
                "missing_candidate_identity", "game candidate lacks its label/value"
            )
        removal_delta_bp = removal_delta * 10000
        if dominant:
            finding = (
                f"剔除游戏 {label} 后，当前相对基线变化为 "
                f"{removal_delta_bp:+.2f}bp，反事实恢复比例为 "
                f"{restoration_ratio:.1%}；该结果只说明异常范围的算术解释力，"
                "不代表已确认根因。"
            )
        else:
            finding = (
                f"剔除游戏 {label} 后，当前相对基线变化为 "
                f"{removal_delta_bp:+.2f}bp，反事实恢复比例为 "
                f"{restoration_ratio:.1%}，未达到主导范围门槛；该结果不代表因果结论。"
            )
        return {
            "status": "succeeded",
            "result": {
                "candidate_id": f"game_id:{candidate['value']}",
                "dimension": "game_id",
                "value": candidate["value"],
                "label": candidate["label"],
                "current_without": current_without,
                "baseline_without": baseline_without,
                "removal_delta_bp": removal_delta_bp,
                "restoration_ratio": restoration_ratio,
                "family_adverse_share": family_share,
                "trigger_reasons": trigger_reasons,
                "dominant": dominant,
                "dominance_reasons": dominance_reasons,
                "finding": finding,
            },
        }

    @staticmethod
    def _finite(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CounterfactualError("invalid_numeric_evidence", f"{label} is invalid")
        result = float(value)
        if not math.isfinite(result):
            raise CounterfactualError("invalid_numeric_evidence", f"{label} is invalid")
        return result

    def _finite_non_negative(self, value: Any, label: str) -> float:
        result = self._finite(value, label)
        if result < 0:
            raise CounterfactualError(
                "invalid_numeric_evidence", f"{label} must be non-negative"
            )
        return result
