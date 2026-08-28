from __future__ import annotations

import re
from typing import Any

from .contracts import RepositoryContracts, canonical_sha256


class EnhancementPlanError(ValueError):
    pass


class EnhancementPlanner:
    DECISION_FIELDS = {
        "module_id",
        "status",
        "reason",
        "evidence_source",
        "frozen_evidence_sha256",
        "module_source_scan_performed",
    }

    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts

    def selector_decision(
        self,
        *,
        module_id: str,
        triggered: bool,
        reason: str | None,
        frozen_evidence_sha256: str,
    ) -> dict[str, Any]:
        return {
            "module_id": module_id,
            "status": "triggered" if triggered else "not_triggered",
            "reason": None if triggered else reason,
            "evidence_source": "frozen_root_and_attribution_evidence",
            "frozen_evidence_sha256": frozen_evidence_sha256,
            "module_source_scan_performed": False,
        }

    def create(
        self,
        post_primary: dict[str, Any],
        selector_decisions: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        post_plan_id = post_primary.get("plan_id")
        post_plan = self.contracts.post_primary_plan(post_plan_id)
        plan_id = post_plan.get("enhancement_priority_plan")
        plan = self.contracts.enhancement_priority_plan(plan_id)
        frozen_evidence_sha256 = post_primary.get("primary_evidence_sha256")
        if (
            not isinstance(frozen_evidence_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", frozen_evidence_sha256) is None
        ):
            raise EnhancementPlanError(
                "enhancement planner requires frozen primary evidence"
            )
        if not isinstance(selector_decisions, dict):
            raise EnhancementPlanError(
                "enhancement selector decisions must be an object"
            )

        modules = plan["modules"]
        known_ids = {module["id"] for module in modules}
        unknown_ids = set(selector_decisions) - known_ids
        if unknown_ids:
            raise EnhancementPlanError(
                f"unknown enhancement selector decisions: {sorted(unknown_ids)}"
            )

        planned_modules = []
        triggered_modules = []
        for module in modules:
            module_id = module["id"]
            planned = {
                "id": module_id,
                "priority": module["priority"],
                "query_cost": module["query_cost"],
            }
            if module["runtime_status"] == "disabled":
                if module_id in selector_decisions:
                    raise EnhancementPlanError(
                        f"disabled enhancement module received a selector decision: "
                        f"{module_id}"
                    )
                planned.update(
                    {
                        "status": "skipped_by_policy",
                        "reason": module["reason"],
                    }
                )
                planned_modules.append(planned)
                continue

            decision = selector_decisions.get(module_id)
            self._validate_selector_decision(
                decision,
                module_id=module_id,
                frozen_evidence_sha256=frozen_evidence_sha256,
                allowed_source=plan["evidence_policy"]["allowed_source"],
            )
            if decision["status"] == "not_triggered":
                planned.update(
                    {"status": "not_triggered", "reason": decision["reason"]}
                )
                planned_modules.append(planned)
                continue
            planned["status"] = "triggered"
            planned_modules.append(planned)
            triggered_modules.append(planned)

        selected_modules = []
        evidence_limits = []
        query_module_count = 0
        max_query_modules = plan["max_query_modules"]
        for module in triggered_modules:
            if query_module_count < max_query_modules:
                module["status"] = "selected"
                selected_modules.append(module["id"])
                query_module_count += 1
                continue
            limit_code = f"enhancement_budget_exhausted:{module['id']}"
            module.update(
                {
                    "status": "skipped_by_budget",
                    "reason": "enhancement_query_module_budget_exhausted",
                    "limit_code": limit_code,
                }
            )
            evidence_limits.append(limit_code)

        return {
            "plan_id": plan_id,
            "plan_contract_sha256": (
                self.contracts.enhancement_priority_plan_contract_sha256(plan_id)
            ),
            "frozen_evidence_sha256": frozen_evidence_sha256,
            "max_query_modules": max_query_modules,
            "query_module_count": query_module_count,
            "selected_modules": selected_modules,
            "modules": planned_modules,
            "evidence_limits": evidence_limits,
        }

    def validate(
        self,
        post_primary: dict[str, Any],
        selector_decisions: dict[str, dict[str, Any]],
    ) -> None:
        actual = post_primary.get("enhancement_plan")
        expected = self.create(post_primary, selector_decisions)
        if canonical_sha256(actual) != canonical_sha256(expected):
            raise EnhancementPlanError(
                "enhancement plan no longer matches frozen selector evidence"
            )

    @staticmethod
    def module(plan: dict[str, Any], module_id: str) -> dict[str, Any]:
        value = next(
            (
                item
                for item in plan.get("modules", [])
                if isinstance(item, dict) and item.get("id") == module_id
            ),
            None,
        )
        if not isinstance(value, dict):
            raise EnhancementPlanError(
                f"enhancement plan lacks registered module: {module_id}"
            )
        return value

    def _validate_selector_decision(
        self,
        decision: Any,
        *,
        module_id: str,
        frozen_evidence_sha256: str,
        allowed_source: str,
    ) -> None:
        if not isinstance(decision, dict) or set(decision) != self.DECISION_FIELDS:
            raise EnhancementPlanError(
                f"enhancement selector decision is invalid: {module_id}"
            )
        if (
            decision["module_id"] != module_id
            or decision["status"] not in {"triggered", "not_triggered"}
            or decision["evidence_source"] != allowed_source
            or decision["frozen_evidence_sha256"] != frozen_evidence_sha256
            or decision["module_source_scan_performed"] is not False
        ):
            raise EnhancementPlanError(
                f"enhancement selector decision is not frozen: {module_id}"
            )
        reason = decision["reason"]
        if decision["status"] == "triggered":
            if reason is not None:
                raise EnhancementPlanError(
                    f"triggered enhancement cannot retain a skip reason: {module_id}"
                )
        elif not isinstance(reason, str) or not reason.strip():
            raise EnhancementPlanError(
                f"non-triggered enhancement requires a reason: {module_id}"
            )
