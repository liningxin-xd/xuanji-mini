from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


class ContributionError(ValueError):
    pass


@dataclass(frozen=True)
class BucketContribution:
    dimension_value: str
    dimension_label: str
    bucket_kind: str
    lifecycle: str
    current_rate: float
    baseline_rate: float
    current_share: float
    baseline_share: float
    composition_impact: float
    performance_impact: float
    total_impact: float
    adverse_impact: float

    def as_candidate(self, dimension: str) -> dict[str, Any]:
        return {
            "dimension": dimension,
            "value": self.dimension_value,
            "label": self.dimension_label,
            "bucket_kind": self.bucket_kind,
            "lifecycle": self.lifecycle,
            "current_rate": self.current_rate,
            "baseline_rate": self.baseline_rate,
            "current_share": self.current_share,
            "baseline_share": self.baseline_share,
            "composition_impact_bp": self.composition_impact * 10000,
            "performance_impact_bp": self.performance_impact * 10000,
            "total_impact_bp": self.total_impact * 10000,
            "adverse_impact_bp": self.adverse_impact * 10000,
        }


def calculate_contributions(
    rows: list[dict[str, Any]],
    *,
    direction: str,
    tolerance: float,
) -> tuple[list[BucketContribution], float]:
    if not rows:
        raise ContributionError("contribution input is empty")
    first = rows[0]
    current_total_denominator = _positive_number(
        first, "overall_current_denominator"
    )
    baseline_total_denominator = _positive_number(
        first, "overall_baseline_denominator"
    )
    current_total_numerator = _non_negative_number(
        first, "overall_current_numerator"
    )
    baseline_total_numerator = _non_negative_number(
        first, "overall_baseline_numerator"
    )
    current_overall_rate = current_total_numerator / current_total_denominator
    baseline_overall_rate = baseline_total_numerator / baseline_total_denominator
    midpoint = (current_overall_rate + baseline_overall_rate) / 2

    contributions: list[BucketContribution] = []
    for row in rows:
        current_denominator = _non_negative_number(row, "current_denominator")
        baseline_denominator = _non_negative_number(row, "baseline_denominator")
        current_numerator = _non_negative_number(row, "current_numerator")
        baseline_numerator = _non_negative_number(row, "baseline_numerator")
        if current_denominator > 0 and baseline_denominator > 0:
            lifecycle = "common"
            current_rate = current_numerator / current_denominator
            baseline_rate = baseline_numerator / baseline_denominator
        elif current_denominator > 0:
            lifecycle = "entrant"
            current_rate = current_numerator / current_denominator
            baseline_rate = current_rate
        elif baseline_denominator > 0:
            lifecycle = "exit"
            baseline_rate = baseline_numerator / baseline_denominator
            current_rate = baseline_rate
        else:
            lifecycle = "empty"
            current_rate = midpoint
            baseline_rate = midpoint

        current_share = current_denominator / current_total_denominator
        baseline_share = baseline_denominator / baseline_total_denominator
        composition = (current_share - baseline_share) * (
            (current_rate + baseline_rate) / 2 - midpoint
        )
        performance = (current_rate - baseline_rate) * (
            current_share + baseline_share
        ) / 2
        total = composition + performance
        if direction == "higher_is_better":
            adverse = max(-total, 0.0)
        elif direction == "lower_is_better":
            adverse = max(total, 0.0)
        else:
            raise ContributionError(f"unknown metric direction: {direction}")
        contributions.append(
            BucketContribution(
                dimension_value=str(row["dimension_value"]),
                dimension_label=str(row["dimension_label"]),
                bucket_kind=str(row["bucket_kind"]),
                lifecycle=lifecycle,
                current_rate=current_rate,
                baseline_rate=baseline_rate,
                current_share=current_share,
                baseline_share=baseline_share,
                composition_impact=composition,
                performance_impact=performance,
                total_impact=total,
                adverse_impact=adverse,
            )
        )

    root_delta = current_overall_rate - baseline_overall_rate
    contribution_sum = sum(item.total_impact for item in contributions)
    if not math.isclose(contribution_sum, root_delta, abs_tol=tolerance):
        raise ContributionError(
            "bucket contributions do not close to the overall rate change: "
            f"sum={contribution_sum:.12f}, root={root_delta:.12f}"
        )
    return contributions, root_delta


def _positive_number(row: dict[str, Any], key: str) -> float:
    value = _non_negative_number(row, key)
    if value <= 0:
        raise ContributionError(f"{key} must be positive")
    return value


def _non_negative_number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContributionError(f"{key} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ContributionError(f"{key} must be finite and non-negative")
    return numeric
