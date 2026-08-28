from __future__ import annotations

import copy
import unittest
from pathlib import Path

from runtime.contracts import RepositoryContracts, canonical_sha256
from runtime.enhancement_planner import EnhancementPlanError, EnhancementPlanner


ROOT = Path(__file__).resolve().parents[1]
FROZEN_EVIDENCE_SHA256 = "a" * 64


class _PolicyContracts:
    def __init__(self, plan: dict):
        self.plan = plan

    @staticmethod
    def post_primary_plan(plan_id: str) -> dict:
        if plan_id != "post_primary_v1":
            raise ValueError(plan_id)
        return {"enhancement_priority_plan": "direction_enhancement_v1"}

    def enhancement_priority_plan(self, plan_id: str) -> dict:
        if plan_id != "direction_enhancement_v1":
            raise ValueError(plan_id)
        return copy.deepcopy(self.plan)

    def enhancement_priority_plan_contract_sha256(self, plan_id: str) -> str:
        return canonical_sha256(
            {"plan_id": plan_id, "contract": self.enhancement_priority_plan(plan_id)}
        )


class EnhancementPlannerTest(unittest.TestCase):
    def setUp(self):
        self.contracts = RepositoryContracts(ROOT)
        self.planner = EnhancementPlanner(self.contracts)
        self.post_primary = {
            "plan_id": "post_primary_v1",
            "primary_evidence_sha256": FROZEN_EVIDENCE_SHA256,
        }

    def test_contract_freezes_priority_budget_and_evidence_policy(self):
        plan = self.contracts.enhancement_priority_plan(
            "direction_enhancement_v1"
        )
        self.assertEqual(2, plan["max_query_modules"])
        self.assertEqual(
            [
                "install_strict_funnel",
                "error_code",
                "cross_dimension_overlap",
                "same_day_version_quasi_experiment",
                "peer_negative_control",
            ],
            [module["id"] for module in plan["modules"]],
        )
        self.assertEqual(
            {
                "allowed_source": "frozen_root_and_attribution_evidence",
                "module_source_scan_before_trigger": False,
                "model_selection_allowed": False,
            },
            plan["evidence_policy"],
        )
        self.assertEqual(
            ["error_code"],
            [
                module["id"]
                for module in plan["modules"]
                if module["runtime_status"] == "enabled"
            ],
        )

    def test_fixed_priority_selects_only_two_triggered_query_modules(self):
        plan = self.contracts.enhancement_priority_plan(
            "direction_enhancement_v1"
        )
        for module in plan["modules"]:
            module["runtime_status"] = "enabled"
            module["selector"] = f"{module['id']}_v1"
            module.pop("reason", None)
        planner = EnhancementPlanner(_PolicyContracts(plan))
        decisions = {
            module["id"]: planner.selector_decision(
                module_id=module["id"],
                triggered=True,
                reason=None,
                frozen_evidence_sha256=FROZEN_EVIDENCE_SHA256,
            )
            for module in plan["modules"]
        }

        result = planner.create(self.post_primary, decisions)

        self.assertEqual(
            ["install_strict_funnel", "error_code"], result["selected_modules"]
        )
        self.assertEqual(2, result["query_module_count"])
        skipped = [
            module
            for module in result["modules"]
            if module["status"] == "skipped_by_budget"
        ]
        self.assertEqual(
            [
                "cross_dimension_overlap",
                "same_day_version_quasi_experiment",
                "peer_negative_control",
            ],
            [module["id"] for module in skipped],
        )
        self.assertEqual(
            [module["limit_code"] for module in skipped],
            result["evidence_limits"],
        )

    def test_decision_requires_frozen_evidence_without_scan_or_model_input(self):
        decision = self.planner.selector_decision(
            module_id="error_code",
            triggered=True,
            reason=None,
            frozen_evidence_sha256=FROZEN_EVIDENCE_SHA256,
        )
        scanned = copy.deepcopy(decision)
        scanned["module_source_scan_performed"] = True
        with self.assertRaisesRegex(EnhancementPlanError, "not frozen"):
            self.planner.create(self.post_primary, {"error_code": scanned})

        model_selected = copy.deepcopy(decision)
        model_selected["selected_by_model"] = True
        with self.assertRaisesRegex(EnhancementPlanError, "decision is invalid"):
            self.planner.create(
                self.post_primary, {"error_code": model_selected}
            )

    def test_not_triggered_module_consumes_no_query_budget(self):
        decision = self.planner.selector_decision(
            module_id="error_code",
            triggered=False,
            reason="root_adverse_delta_below_threshold",
            frozen_evidence_sha256=FROZEN_EVIDENCE_SHA256,
        )
        result = self.planner.create(
            self.post_primary, {"error_code": decision}
        )
        self.assertEqual([], result["selected_modules"])
        self.assertEqual(0, result["query_module_count"])
        self.assertEqual([], result["evidence_limits"])
        self.assertEqual(
            "not_triggered",
            self.planner.module(result, "error_code")["status"],
        )


if __name__ == "__main__":
    unittest.main()
