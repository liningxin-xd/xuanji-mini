from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime.contracts import RepositoryContracts
from runtime.final_validator import FinalEvidenceValidator, FinalValidationError
from runtime.runner import AttributionRunner, RunnerError
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


class PostPrimaryCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _complete(
        self,
        run_id: str,
        *,
        profile: str = "primary_v2",
        game_mode: str = "dominant",
    ) -> tuple[AttributionRunner, int]:
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile=profile,
        )
        runner.init_run(
            run_id=run_id,
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        query_count = 0
        while True:
            ticket = runner.next_action(run_id)
            if ticket["action"] == "queue_complete":
                break
            query_count += 1
            if ticket["step_id"] == "game_id":
                raw_result = raw_result_for_ticket(
                    runner,
                    run_id,
                    ticket,
                    candidate=game_mode != "none",
                )
                if game_mode != "none":
                    self._set_game_scenario(raw_result, game_mode)
            elif ticket["step_id"] not in {"secondary", "game_background"}:
                raw_result = raw_result_for_ticket(runner, run_id, ticket)
                if game_mode != "none":
                    current_num, baseline_num, current_den, baseline_den = {
                        "dominant": (750, 800, 1000, 1000),
                        "trigger_not_met": (750, 800, 1000, 1000),
                        "non_positive_denominator": (700, 800, 1000, 1000),
                    }[game_mode]
                    self._set_non_candidate_root(
                        raw_result,
                        current_num=current_num,
                        baseline_num=baseline_num,
                        current_den=current_den,
                        baseline_den=baseline_den,
                    )
            else:
                raw_result = raw_result_for_ticket(runner, run_id, ticket)
            runner.record(
                run_id,
                self_reported_result_event(
                    ticket,
                    raw_result,
                    f"{run_id}-{ticket['step_id']}",
                ),
            )
        return runner, query_count

    @staticmethod
    def _set_game_scenario(raw_result: dict, mode: str) -> None:
        scenarios = {
            "dominant": ((500, 350), (500, 400), (500, 400), (500, 400)),
            "trigger_not_met": (
                (500, 390),
                (500, 400),
                (500, 360),
                (500, 400),
            ),
            "non_positive_denominator": (
                (100, 70),
                (700, 560),
                (0, 0),
                (0, 0),
            ),
        }
        current_game, baseline_game, current_residual, baseline_residual = scenarios[
            mode
        ]
        rows = raw_result["rows"]
        values = (
            (rows[0], current_game, baseline_game),
            (rows[1], current_residual, baseline_residual),
        )
        for row, current, baseline in values:
            row["current_denominator"], row["current_numerator"] = current
            row["baseline_denominator"], row["baseline_numerator"] = baseline
        current_den = sum(item[1][0] for item in values)
        current_num = sum(item[1][1] for item in values)
        baseline_den = sum(item[2][0] for item in values)
        baseline_num = sum(item[2][1] for item in values)
        for row in rows:
            row["overall_current_denominator"] = current_den
            row["overall_current_numerator"] = current_num
            row["overall_baseline_denominator"] = baseline_den
            row["overall_baseline_numerator"] = baseline_num

    @staticmethod
    def _set_non_candidate_root(
        raw_result: dict,
        *,
        current_num: int,
        baseline_num: int,
        current_den: int,
        baseline_den: int,
    ) -> None:
        rows = raw_result["rows"]
        rows[0]["current_denominator"] = 1
        rows[0]["baseline_denominator"] = 1
        rows[0]["current_numerator"] = 0
        rows[0]["baseline_numerator"] = 0
        rows[1]["current_denominator"] = current_den - 1
        rows[1]["baseline_denominator"] = baseline_den - 1
        rows[1]["current_numerator"] = current_num
        rows[1]["baseline_numerator"] = baseline_num
        for row in rows:
            row["overall_current_denominator"] = current_den
            row["overall_current_numerator"] = current_num
            row["overall_baseline_denominator"] = baseline_den
            row["overall_baseline_numerator"] = baseline_num
            if "overall_current_dimension_matched_denominator" in row:
                row["overall_current_dimension_matched_denominator"] = current_den
                row["overall_baseline_dimension_matched_denominator"] = baseline_den
                row["overall_current_dimension_match_rate"] = 1.0
                row["overall_baseline_dimension_match_rate"] = 1.0

    @staticmethod
    def _context() -> dict:
        return {
            "source": "dataworks_dqc",
            "project": "tap_dw",
            "table": "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d",
            "partition": "dt=2026-08-22",
            "investigation": {
                "rule_indexes": [0],
                "metric_hint": "下载完成率",
                "alert_partition": "dt=2026-08-22",
                "alert_rules": [{"rule_name": "下载完成率低于阈值"}],
            },
        }

    @staticmethod
    def _patch(pack: dict) -> dict:
        return {
            "summary": "下载完成率相对基线下降，异常集中在已登记游戏范围。",
            "finding_texts": {
                item["candidate_id"]: f"核查游戏 {item['label']} 的下载链路。"
                for item in pack["candidates"]
            },
            "evidence_limits": [],
            "recommended_action": "复核候选游戏的下载配置和链路变化。",
        }

    def test_profile_contract_freezes_v1_and_budgets_v2(self):
        contracts = RepositoryContracts(ROOT)
        self.assertEqual("primary_v1", contracts.default_analysis_profile)
        self.assertIsNone(
            contracts.analysis_profile("primary_v1")["post_primary_plan"]
        )
        profile = contracts.analysis_profile("primary_v2")
        self.assertEqual(
            ["counterfactual", "secondary", "game_background"],
            profile["enabled_post_primary_steps"],
        )
        plan = contracts.post_primary_plan(profile["post_primary_plan"])
        self.assertEqual(5, plan["max_additional_queries"])
        self.assertEqual(
            ["counterfactual", "secondary", "game_background", "error_code"],
            [step["id"] for step in plan["steps"]],
        )

    def test_primary_v1_export_and_writer_pack_remain_unchanged(self):
        runner, query_count = self._complete(
            "v1-compat", profile="primary_v1", game_mode="dominant"
        )
        execution = runner.export("v1-compat")
        pack = runner.build_writer_pack("v1-compat")
        self.assertEqual(7, query_count)
        self.assertEqual("primary_v1", pack["analysis_profile"])
        self.assertNotIn("analysis_profile", execution)
        self.assertNotIn("post_primary", execution)
        self.assertNotIn("counterfactual", pack)
        self.assertIsNone(runner.load_state("v1-compat")["post_primary"])

    def test_pre_profile_primary_v1_state_remains_resumable(self):
        runner, _ = self._complete(
            "v1-old-state", profile="primary_v1", game_mode="dominant"
        )
        state = runner.load_state("v1-old-state")
        for field in (
            "analysis_profile",
            "analysis_profile_sha256",
            "post_primary_plan_sha256",
            "post_primary",
        ):
            state.pop(field, None)
        for step in state["steps"]:
            for field in (
                "root_current_numerator",
                "root_current_denominator",
                "root_baseline_numerator",
                "root_baseline_denominator",
                "family_adverse_impact_bp",
            ):
                step.pop(field, None)
            for candidate in step["candidates"]:
                candidate.pop("private_counts", None)
        runner._write_state(state)
        self.assertEqual(
            "full_queue", runner.export("v1-old-state")["mode"]
        )

    def test_dominant_counterfactual_is_machine_computed_without_queries(self):
        runner, query_count = self._complete("v2-dominant")
        pack = runner.build_writer_pack("v2-dominant")
        state = runner.load_state("v2-dominant")
        self.assertEqual(9, query_count)
        self.assertEqual("primary_v2", pack["analysis_profile"])
        self.assertEqual(0.0, pack["counterfactual"]["removal_delta_bp"])
        self.assertEqual(1.0, pack["counterfactual"]["restoration_ratio"])
        self.assertTrue(pack["counterfactual"]["dominant"])
        self.assertEqual("completed", state["post_primary"]["status"])
        self.assertEqual(
            ["succeeded", "succeeded", "succeeded", "skipped_by_policy"],
            [step["status"] for step in state["post_primary"]["steps"]],
        )
        encoded = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded.encode("utf-8")), 12 * 1024)
        self.assertNotIn("private_counts", encoded)
        self.assertNotIn("current_numerator", encoded)

        analysis = runner.assemble_final(
            "v2-dominant", self._patch(pack), self._context()
        )
        self.assertEqual(
            pack["counterfactual"]["finding"],
            analysis["investigations"][0]["counterfactual"]["finding"],
        )
        self.assertEqual(
            "valid",
            FinalEvidenceValidator().validate(state, analysis, 0)["status"],
        )
        tampered = copy.deepcopy(analysis)
        tampered["investigations"][0]["counterfactual"][
            "removal_delta_bp"
        ] = 1.0
        with self.assertRaisesRegex(FinalValidationError, "machine evidence"):
            FinalEvidenceValidator().validate(state, tampered, 0)

    def test_no_candidate_skips_counterfactual_and_stays_no_dominant_slice(self):
        runner, _ = self._complete("v2-no-candidate", game_mode="none")
        pack = runner.build_writer_pack("v2-no-candidate")
        self.assertEqual("no_dominant_slice", pack["result_status_hint"])
        self.assertNotIn("counterfactual", pack)
        step = runner.load_state("v2-no-candidate")["post_primary"]["steps"][0]
        self.assertEqual("skipped_by_policy", step["status"])
        self.assertEqual("no_legal_game_candidate", step["reason"])

    def test_trigger_not_met_omits_counterfactual_without_deleting_candidate(self):
        runner, _ = self._complete(
            "v2-trigger-not-met", game_mode="trigger_not_met"
        )
        pack = runner.build_writer_pack("v2-trigger-not-met")
        self.assertEqual("completed", pack["result_status_hint"])
        self.assertTrue(pack["candidates"])
        self.assertNotIn("counterfactual", pack)
        step = runner.load_state("v2-trigger-not-met")["post_primary"]["steps"][0]
        self.assertEqual("counterfactual_trigger_not_met", step["reason"])

    def test_non_positive_remaining_denominator_keeps_a_machine_limit(self):
        runner, _ = self._complete(
            "v2-denominator", game_mode="non_positive_denominator"
        )
        pack = runner.build_writer_pack("v2-denominator")
        self.assertEqual("completed", pack["result_status_hint"])
        self.assertNotIn("counterfactual", pack)
        self.assertIn(
            "counterfactual:non_positive_remaining_denominator",
            pack["evidence_limits"],
        )

    def test_same_primary_evidence_produces_identical_calibration(self):
        first, _ = self._complete("v2-repeat-a")
        second, _ = self._complete("v2-repeat-b")
        first.build_writer_pack("v2-repeat-a")
        second.build_writer_pack("v2-repeat-b")
        first_post = first.load_state("v2-repeat-a")["post_primary"]
        second_post = second.load_state("v2-repeat-b")["post_primary"]
        for post_primary in (first_post, second_post):
            secondary = post_primary["steps"][1]
            for attempt in secondary.get("attempts", []):
                attempt["query_id"] = "stable-query-id"
                attempt["event_path"] = "stable-event-path"
            background = post_primary["steps"][2]
            for item in background.get("items", []):
                for attempt in item.get("attempts", []):
                    attempt["query_id"] = "stable-background-query-id"
                    attempt["event_path"] = "stable-background-event-path"
        self.assertEqual(first_post, second_post)

    def test_executing_post_primary_plan_resumes_deterministically(self):
        runner, _ = self._complete("v2-calibration-resume")
        state = runner.load_state("v2-calibration-resume")
        state["post_primary"] = runner.calibration_runner.create_plan(state)
        state["ready_for_final_validation"] = False
        runner._write_state(state)
        planned = runner.load_state("v2-calibration-resume")["post_primary"]
        self.assertEqual("executing", planned["status"])
        self.assertEqual("planned", planned["steps"][0]["status"])
        ticket = runner.next_action("v2-calibration-resume")
        self.assertEqual("secondary", ticket["step_id"])
        runner.record(
            "v2-calibration-resume",
            self_reported_result_event(
                ticket,
                raw_result_for_ticket(
                    runner, "v2-calibration-resume", ticket
                ),
                "v2-calibration-resume-secondary-resumed",
            ),
        )
        background_ticket = runner.next_action("v2-calibration-resume")
        self.assertEqual("game_background", background_ticket["step_id"])
        runner.record(
            "v2-calibration-resume",
            self_reported_result_event(
                background_ticket,
                raw_result_for_ticket(
                    runner, "v2-calibration-resume", background_ticket
                ),
                "v2-calibration-resume-background-resumed",
            ),
        )
        self.assertEqual(
            "queue_complete",
            runner.next_action("v2-calibration-resume")["action"],
        )
        runner.build_writer_pack("v2-calibration-resume")
        resumed = runner.load_state("v2-calibration-resume")["post_primary"]
        self.assertEqual("completed", resumed["status"])
        self.assertEqual("succeeded", resumed["steps"][0]["status"])
        self.assertEqual("succeeded", resumed["steps"][1]["status"])

    def test_profile_is_immutable_across_run_resume(self):
        runner, _ = self._complete("v2-profile")
        runner.build_writer_pack("v2-profile")
        primary_v1 = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v1",
        )
        with self.assertRaisesRegex(RunnerError, "analysis profile"):
            primary_v1.init_run(
                run_id="v2-profile",
                chain="download",
                game_type="app",
                metric="下载完成率",
                alert_date="2026-08-22",
                receipt_mode="self_reported",
                resume=True,
            )


if __name__ == "__main__":
    unittest.main()
