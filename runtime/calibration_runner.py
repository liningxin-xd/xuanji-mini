from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import RepositoryContracts, canonical_sha256
from .counterfactual import CounterfactualCalculator, CounterfactualError
from .post_primary_planner import PostPrimaryPlanError, PostPrimaryPlanner


class CalibrationError(ValueError):
    pass


class CalibrationRunner:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts
        self.planner = PostPrimaryPlanner(contracts)
        self.counterfactual = CounterfactualCalculator(contracts)

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
        if post_primary["status"] == "completed":
            self._validate_completed(state, post_primary)
            return deepcopy(post_primary)

        result = deepcopy(post_primary)
        for step in result["steps"]:
            if step["status"] != "planned":
                continue
            if step["id"] != "counterfactual":
                raise CalibrationError(
                    f"enabled post-primary step is not implemented: {step['id']}"
                )
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
        result["status"] = "completed"
        self._validate_completed(state, result)
        return result

    def _validate_completed(
        self, state: dict[str, Any], post_primary: dict[str, Any]
    ) -> None:
        self.planner.validate_identity(state, post_primary)
        planned = self.planner.create(state)
        if planned is None:
            raise CalibrationError("primary_v1 cannot complete post-primary analysis")
        expected = deepcopy(planned)
        for step in expected["steps"]:
            if step["status"] != "planned":
                continue
            try:
                outcome = self.counterfactual.calculate(state)
            except CounterfactualError as exc:
                step.update({"status": "failed", "failure_code": exc.code})
            else:
                step.update(outcome)
        expected["status"] = "completed"
        if canonical_sha256(post_primary) != canonical_sha256(expected):
            raise CalibrationError("post-primary result does not match primary evidence")
