from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import RepositoryContracts, canonical_sha256
from .models import TERMINAL_STEP_STATUSES


class PostPrimaryPlanError(ValueError):
    pass


class PostPrimaryPlanner:
    STEP_STATUSES = {
        "planned",
        "in_progress",
        "repair_required",
        "succeeded",
        "failed",
        "skipped_by_policy",
    }

    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def create(self, state: dict[str, Any]) -> dict[str, Any] | None:
        profile_name = self.profile_name(state)
        profile = self.contracts.analysis_profile(profile_name)
        plan_id = profile["post_primary_plan"]
        if plan_id is None:
            return None
        primary_evidence_sha256 = canonical_sha256(self.primary_evidence(state))
        enabled = set(profile["enabled_post_primary_steps"])
        plan = self.contracts.post_primary_plan(plan_id)
        steps = []
        for item in plan["steps"]:
            step = {"id": item["id"], "status": "planned"}
            if item["id"] not in enabled:
                step.update(
                    {
                        "status": "skipped_by_policy",
                        "reason": "profile_step_disabled",
                    }
                )
            steps.append(step)
        return {
            "profile": profile_name,
            "plan_id": plan_id,
            "primary_evidence_sha256": primary_evidence_sha256,
            "enhancement_plan": None,
            "status": "executing",
            "steps": steps,
        }

    def validate_identity(
        self, state: dict[str, Any], post_primary: dict[str, Any]
    ) -> None:
        expected = self.create(state)
        if expected is None:
            raise PostPrimaryPlanError("primary_v1 cannot contain post-primary state")
        for field in ("profile", "plan_id", "primary_evidence_sha256"):
            if post_primary.get(field) != expected[field]:
                raise PostPrimaryPlanError(f"post-primary {field} changed")
        if post_primary.get("status") not in {"executing", "completed"}:
            raise PostPrimaryPlanError("post-primary status is invalid")
        steps = post_primary.get("steps")
        expected_steps = expected["steps"]
        if not isinstance(steps, list) or len(steps) != len(expected_steps):
            raise PostPrimaryPlanError("post-primary step count changed")
        for actual, planned in zip(steps, expected_steps, strict=True):
            if not isinstance(actual, dict) or actual.get("id") != planned["id"]:
                raise PostPrimaryPlanError("post-primary steps changed order")
            if actual.get("status") not in self.STEP_STATUSES:
                raise PostPrimaryPlanError("post-primary step status is invalid")
            if planned["status"] == "skipped_by_policy" and actual != planned:
                raise PostPrimaryPlanError("disabled post-primary step changed")
        if "enhancement_plan" not in post_primary:
            raise PostPrimaryPlanError("post-primary state lacks enhancement plan state")
        enhancement_plan = post_primary["enhancement_plan"]
        if enhancement_plan is not None and not isinstance(enhancement_plan, dict):
            raise PostPrimaryPlanError("post-primary enhancement plan is invalid")
        error_code = next(step for step in steps if step["id"] == "error_code")
        if error_code["status"] != "planned" and enhancement_plan is None:
            raise PostPrimaryPlanError(
                "scheduled error-code step lacks its enhancement plan"
            )
        if enhancement_plan is not None and any(
            step["status"] in {"planned", "in_progress", "repair_required"}
            for step in steps
            if step["id"] in {"counterfactual", "secondary", "game_background"}
        ):
            raise PostPrimaryPlanError(
                "enhancement plan was frozen before prerequisite evidence"
            )
        if post_primary["status"] == "completed" and any(
            step["status"] in {"planned", "in_progress", "repair_required"}
            for step in steps
        ):
            raise PostPrimaryPlanError(
                "completed post-primary plan has a non-terminal step"
            )

    def primary_evidence(self, state: dict[str, Any]) -> dict[str, Any]:
        steps = state.get("steps")
        if not isinstance(steps, list) or state.get("cursor") != len(steps):
            raise PostPrimaryPlanError("post-primary requires a complete primary queue")
        if any(step.get("status") not in TERMINAL_STEP_STATUSES for step in steps):
            raise PostPrimaryPlanError("post-primary requires terminal primary steps")
        return {
            "schema_version": 1,
            "plan_id": state["plan_id"],
            "plan_contract_sha256": state["plan_contract_sha256"],
            "secondary_relations_sha256": state.get(
                "secondary_relations_sha256"
            ),
            "error_code_capabilities_sha256": state.get(
                "error_code_capabilities_sha256"
            ),
            "error_code_triggers_sha256": state.get(
                "error_code_triggers_sha256"
            ),
            "enhancement_priority_sha256": state.get(
                "enhancement_priority_sha256"
            ),
            "chain": state["chain"],
            "game_type": state["game_type"],
            "metric": state["metric"],
            "analysis_date": state["analysis_date"],
            "canonical_root_metric": deepcopy(state.get("canonical_root_metric")),
            "steps": [
                {
                    key: deepcopy(step.get(key))
                    for key in (
                        "id",
                        "kind",
                        "produces_candidates",
                        "status",
                        "candidate_count",
                        "candidates",
                        "root_current_value",
                        "root_baseline_value",
                        "root_delta",
                        "root_current_numerator",
                        "root_current_denominator",
                        "root_baseline_numerator",
                        "root_baseline_denominator",
                        "family_adverse_impact_bp",
                        "failure_code",
                        "reason",
                        "warning_codes",
                    )
                }
                for step in steps
            ],
        }

    def profile_name(self, state: dict[str, Any]) -> str:
        profile = state.get("analysis_profile", "primary_v1")
        if not isinstance(profile, str):
            raise PostPrimaryPlanError("analysis profile is invalid")
        self.contracts.analysis_profile(profile)
        return profile
