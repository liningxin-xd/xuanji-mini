from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime.contracts import ContractError, RepositoryContracts
from runtime.final_validator import FinalEvidenceValidator, FinalValidationError
from runtime.runner import AttributionRunner, RunnerError
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_error_event,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


class SecondaryAttributionRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _new_runner(
        self,
        run_id: str,
        *,
        chain: str = "download",
        game_type: str = "app",
        metric: str = "下载完成率",
    ) -> AttributionRunner:
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        runner.init_run(
            run_id=run_id,
            chain=chain,
            game_type=game_type,
            metric=metric,
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        return runner

    def _complete(
        self,
        run_id: str,
        *,
        chain: str = "download",
        game_type: str = "app",
        metric: str = "下载完成率",
        game_candidate: bool = True,
        non_dominant: bool = False,
        secondary_mutator=None,
    ) -> tuple[AttributionRunner, int]:
        runner = self._new_runner(
            run_id, chain=chain, game_type=game_type, metric=metric
        )
        query_count = 0
        while True:
            ticket = runner.next_action(run_id)
            if ticket["action"] == "queue_complete":
                break
            query_count += 1
            raw = raw_result_for_ticket(
                runner,
                run_id,
                ticket,
                candidate=ticket["step_id"] == "game_id" and game_candidate,
            )
            if non_dominant and ticket["step_id"] == "game_id":
                self._set_game_counts(
                    raw,
                    current_game=(500, 350),
                    baseline_game=(500, 400),
                    current_outside=(500, 380),
                    baseline_outside=(500, 400),
                )
            elif non_dominant and ticket["step_id"] not in {
                "secondary",
                "game_background",
            } and (
                ticket["step_id"] != "install_stage"
            ):
                self._set_root_counts(
                    raw,
                    current_numerator=730,
                    baseline_numerator=800,
                    current_denominator=1000,
                    baseline_denominator=1000,
                )
            if ticket["step_id"] == "secondary" and secondary_mutator:
                secondary_mutator(raw)
            runner.record(
                run_id,
                self_reported_result_event(
                    ticket, raw, f"{run_id}-{ticket['step_id']}-{query_count}"
                ),
            )
        return runner, query_count

    @staticmethod
    def _set_game_counts(
        raw: dict,
        *,
        current_game: tuple[int, int],
        baseline_game: tuple[int, int],
        current_outside: tuple[int, int],
        baseline_outside: tuple[int, int],
    ) -> None:
        pairs = (
            (raw["rows"][0], current_game, baseline_game),
            (raw["rows"][1], current_outside, baseline_outside),
        )
        for row, current, baseline in pairs:
            row["current_denominator"], row["current_numerator"] = current
            row["baseline_denominator"], row["baseline_numerator"] = baseline
        for row in raw["rows"]:
            row["overall_current_denominator"] = sum(item[1][0] for item in pairs)
            row["overall_current_numerator"] = sum(item[1][1] for item in pairs)
            row["overall_baseline_denominator"] = sum(item[2][0] for item in pairs)
            row["overall_baseline_numerator"] = sum(item[2][1] for item in pairs)

    @staticmethod
    def _set_root_counts(
        raw: dict,
        *,
        current_numerator: int,
        baseline_numerator: int,
        current_denominator: int,
        baseline_denominator: int,
    ) -> None:
        rows = raw["rows"]
        rows[0]["current_denominator"] = 1
        rows[0]["baseline_denominator"] = 1
        rows[0]["current_numerator"] = 0
        rows[0]["baseline_numerator"] = 0
        rows[1]["current_denominator"] = current_denominator - 1
        rows[1]["baseline_denominator"] = baseline_denominator - 1
        rows[1]["current_numerator"] = current_numerator
        rows[1]["baseline_numerator"] = baseline_numerator
        for row in rows:
            row["overall_current_denominator"] = current_denominator
            row["overall_current_numerator"] = current_numerator
            row["overall_baseline_denominator"] = baseline_denominator
            row["overall_baseline_numerator"] = baseline_numerator
            if "overall_current_dimension_matched_denominator" in row:
                row["overall_current_dimension_matched_denominator"] = (
                    current_denominator
                )
                row["overall_baseline_dimension_matched_denominator"] = (
                    baseline_denominator
                )
                row["overall_current_dimension_match_rate"] = 1.0
                row["overall_baseline_dimension_match_rate"] = 1.0

    @staticmethod
    def _context(metric: str, alert_date: str = "2026-08-22") -> dict:
        return {
            "source": "dataworks_dqc",
            "project": "tap_dw",
            "table": "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d",
            "partition": f"dt={alert_date}",
            "investigation": {
                "rule_indexes": [0],
                "metric_hint": metric,
                "alert_partition": f"dt={alert_date}",
                "alert_rules": [{"rule_name": f"{metric}告警"}],
            },
        }

    @staticmethod
    def _patch(pack: dict) -> dict:
        return {
            "summary": "已完成固定一级队列和一次有界二级归因。",
            "finding_texts": {
                item["candidate_id"]: f"候选 {item['label']} 达到机器门槛。"
                for item in pack["candidates"]
            },
            "evidence_limits": [],
            "recommended_action": "复核父游戏范围内的二级候选链路。",
        }

    def test_dominant_game_runs_one_download_secondary_query(self):
        runner, query_count = self._complete("secondary-download")
        self.assertEqual(9, query_count)
        state = runner.load_state("secondary-download")
        secondary = state["post_primary"]["steps"][1]
        self.assertEqual("succeeded", secondary["status"])
        self.assertEqual(
            "dominant_counterfactual_game",
            secondary["parent_selection_reason"],
        )
        self.assertEqual("device_brand", secondary["child_dimension"])
        self.assertEqual(1, len(secondary["attempts"]))
        self.assertGreater(secondary["candidate_count"], 0)

        execution = runner.export("secondary-download")
        self.assertEqual(1, len(execution["secondary_steps"]))
        pack = runner.build_writer_pack("secondary-download")
        encoded = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded.encode("utf-8")), 12 * 1024)
        for forbidden in ("rendered_sql", "raw_result", "query_id", "receipt"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(3, len(pack["post_primary_steps"]))

    def test_non_dominant_head_game_still_selects_secondary_parent(self):
        runner, query_count = self._complete(
            "secondary-non-dominant", non_dominant=True
        )
        self.assertEqual(9, query_count)
        state = runner.load_state("secondary-non-dominant")
        counterfactual = state["post_primary"]["steps"][0]["result"]
        secondary = state["post_primary"]["steps"][1]
        self.assertFalse(counterfactual["dominant"])
        self.assertEqual(
            "largest_legal_game_candidate",
            secondary["parent_selection_reason"],
        )
        self.assertEqual("succeeded", secondary["status"])

    def test_no_game_candidate_skips_secondary_without_a_query(self):
        runner, query_count = self._complete(
            "secondary-no-game", game_candidate=False
        )
        self.assertEqual(7, query_count)
        secondary = runner.load_state("secondary-no-game")["post_primary"][
            "steps"
        ][1]
        self.assertEqual("skipped_by_policy", secondary["status"])
        self.assertEqual("no_legal_game_candidate", secondary["reason"])

    def test_missing_outside_parent_fails_only_secondary(self):
        def remove_outside(raw):
            raw["rows"] = [
                row for row in raw["rows"] if row["bucket_kind"] != "outside_parent"
            ]

        runner, _ = self._complete(
            "secondary-missing-outside", secondary_mutator=remove_outside
        )
        state = runner.load_state("secondary-missing-outside")
        secondary = state["post_primary"]["steps"][1]
        self.assertEqual("failed", secondary["status"])
        self.assertEqual("result_incomplete", secondary["failure_code"])
        self.assertTrue(state["steps"][0]["candidates"])
        self.assertEqual(
            "completed",
            runner.build_writer_pack("secondary-missing-outside")[
                "result_status_hint"
            ],
        )

    def test_negative_row_audit_cannot_hide_behind_root_closure(self):
        def offset_row_counts(raw):
            total = raw["rows"][0]["overall_current_row_count"]
            raw["rows"][0]["current_row_count"] = -1
            raw["rows"][1]["current_row_count"] = total + 1

        runner, _ = self._complete(
            "secondary-negative-row-audit",
            secondary_mutator=offset_row_counts,
        )
        secondary = runner.load_state("secondary-negative-row-audit")[
            "post_primary"
        ]["steps"][1]
        self.assertEqual("failed", secondary["status"])
        self.assertEqual("quality_gate_failed", secondary["failure_code"])
        self.assertIn("cannot be negative", secondary["reason"])

    def test_secondary_semantic_error_allows_only_two_repairs(self):
        runner = self._new_runner("secondary-repair")
        while True:
            ticket = runner.next_action("secondary-repair")
            if ticket.get("step_id") == "secondary":
                break
            raw = raw_result_for_ticket(
                runner,
                "secondary-repair",
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            runner.record(
                "secondary-repair",
                self_reported_result_event(
                    ticket, raw, f"primary-{ticket['step_id']}"
                ),
            )

        for repair_index, token in enumerate(
            ("scoped_rows", "bucket_aggregates"), start=1
        ):
            runner.record(
                "secondary-repair",
                self_reported_error_event(
                    ticket,
                    query_id=f"secondary-error-{repair_index}",
                    error_class="semantic_analysis",
                ),
            )
            repair = runner.next_action("secondary-repair")
            self.assertEqual(repair_index, repair["repair_attempt"])
            repaired_sql = repair["original_sql"].replace(
                token, f"{token}_repair_{repair_index}"
            )
            runner.record(
                "secondary-repair",
                {
                    "event": "repair_submitted",
                    "step_id": "secondary",
                    "repair_attempt": repair_index,
                    "repair_reason": "修正已知别名作用域错误",
                    "error_evidence": "ODPS-0130071 semantic analysis",
                    "repaired_sql": repaired_sql,
                },
            )
            ticket = runner.next_action("secondary-repair")

        runner.record(
            "secondary-repair",
            self_reported_error_event(
                ticket,
                query_id="secondary-error-final",
                error_class="semantic_analysis",
            ),
        )
        background_ticket = runner.next_action("secondary-repair")
        self.assertEqual("game_background", background_ticket["step_id"])
        runner.record(
            "secondary-repair",
            self_reported_result_event(
                background_ticket,
                raw_result_for_ticket(
                    runner, "secondary-repair", background_ticket
                ),
                "background-after-secondary-repair",
            ),
        )
        self.assertEqual(
            "queue_complete", runner.next_action("secondary-repair")["action"]
        )
        secondary = runner.load_state("secondary-repair")["post_primary"][
            "steps"
        ][1]
        self.assertEqual("failed", secondary["status"])
        self.assertEqual(3, len(secondary["attempts"]))
        self.assertIn("two evidence-based repairs", secondary["reason"])

    def test_secondary_query_failure_reason_stays_out_of_writer_pack(self):
        runner = self._new_runner("secondary-redaction")
        while True:
            ticket = runner.next_action("secondary-redaction")
            if ticket.get("step_id") == "secondary":
                break
            raw = raw_result_for_ticket(
                runner,
                "secondary-redaction",
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            runner.record(
                "secondary-redaction",
                self_reported_result_event(
                    ticket, raw, f"primary-{ticket['step_id']}"
                ),
            )

        private_message = (
            "SELECT secret FROM tap_dw.private_table "
            "query_id=private-secondary-query"
        )
        runner.record(
            "secondary-redaction",
            {
                "event": "query_error",
                "step_id": "secondary",
                "attempt_no": ticket["attempt_no"],
                "receipt_type": "self_reported_receipt",
                "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                "query_id": "private-secondary-query",
                "error_class": "execution",
                "error_code": "ODPS-PRIVATE",
                "error_message": private_message,
            },
        )
        background_ticket = runner.next_action("secondary-redaction")
        runner.record(
            "secondary-redaction",
            self_reported_result_event(
                background_ticket,
                raw_result_for_ticket(
                    runner, "secondary-redaction", background_ticket
                ),
                "background-after-secondary-failure",
            ),
        )
        pack = runner.build_writer_pack("secondary-redaction")
        encoded = json.dumps(pack, ensure_ascii=False)
        self.assertNotIn(private_message, encoded)
        self.assertNotIn("tap_dw.private_table", encoded)
        self.assertNotIn("private-secondary-query", encoded)
        self.assertEqual(
            "query_failed",
            pack["post_primary_steps"][1]["failure_code"],
        )
        self.assertIn(
            private_message,
            runner.load_state("secondary-redaction")["post_primary"]["steps"][1][
                "reason"
            ],
        )

    def test_apk_install_runs_six_primary_plus_one_secondary_query(self):
        runner, query_count = self._complete(
            "secondary-install",
            chain="install",
            game_type="app",
            metric="下载安装完成率",
        )
        self.assertEqual(8, query_count)
        state = runner.load_state("secondary-install")
        self.assertEqual("2026-08-20", state["analysis_date"])
        self.assertEqual("succeeded", state["post_primary"]["steps"][1]["status"])
        pack = runner.build_writer_pack("secondary-install")
        analysis = runner.assemble_final(
            "secondary-install",
            self._patch(pack),
            self._context("下载安装完成率"),
        )
        self.assertEqual(
            "valid",
            FinalEvidenceValidator().validate(state, analysis, 0)["status"],
        )

    def test_apk_install_rejects_an_immature_secondary_bucket(self):
        def change_bucket_window(raw):
            raw["rows"][0]["current_observation_days_min"] = 2

        runner, query_count = self._complete(
            "secondary-install-immature",
            chain="install",
            game_type="app",
            metric="下载安装完成率",
            secondary_mutator=change_bucket_window,
        )
        self.assertEqual(8, query_count)
        secondary = runner.load_state("secondary-install-immature")[
            "post_primary"
        ]["steps"][1]
        self.assertEqual("failed", secondary["status"])
        self.assertEqual("quality_gate_failed", secondary["failure_code"])
        self.assertIn("bucket observation window", secondary["reason"])

    def test_lower_is_better_counterfactual_and_secondary_are_supported(self):
        runner, query_count = self._complete(
            "secondary-lower-is-better", metric="下载失败率"
        )
        self.assertEqual(9, query_count)
        state = runner.load_state("secondary-lower-is-better")
        self.assertTrue(state["post_primary"]["steps"][0]["result"]["dominant"])
        self.assertEqual("succeeded", state["post_primary"]["steps"][1]["status"])

    def test_final_validator_rejects_forged_parent_and_child_candidate(self):
        runner, _ = self._complete("secondary-final")
        pack = runner.build_writer_pack("secondary-final")
        state = runner.load_state("secondary-final")
        analysis = runner.assemble_final(
            "secondary-final", self._patch(pack), self._context("下载完成率")
        )
        finding = next(
            item
            for item in analysis["investigations"][0]["top_findings"]
            if item["attribution_level"] == "secondary"
        )
        forged_parent = copy.deepcopy(analysis)
        forged = next(
            item
            for item in forged_parent["investigations"][0]["top_findings"]
            if item["attribution_level"] == "secondary"
        )
        forged["parent_value"] = "forged-game"
        with self.assertRaisesRegex(FinalValidationError, "parent_value"):
            FinalEvidenceValidator().validate(state, forged_parent, 0)

        forged_child = copy.deepcopy(analysis)
        forged = next(
            item
            for item in forged_child["investigations"][0]["top_findings"]
            if item["attribution_level"] == "secondary"
        )
        forged["value"] = "not-a-secondary-candidate"
        with self.assertRaisesRegex(FinalValidationError, "validated candidate"):
            FinalEvidenceValidator().validate(state, forged_child, 0)
        self.assertTrue(finding["parent_value"])

    def test_second_secondary_step_and_non_game_parent_are_rejected(self):
        runner, _ = self._complete("secondary-depth")
        pack = runner.build_writer_pack("secondary-depth")
        state = runner.load_state("secondary-depth")
        analysis = runner.assemble_final(
            "secondary-depth", self._patch(pack), self._context("下载完成率")
        )
        analysis["investigations"][0]["attribution_execution"][
            "secondary_steps"
        ].append(
            copy.deepcopy(
                analysis["investigations"][0]["attribution_execution"][
                    "secondary_steps"
                ][0]
            )
        )
        with self.assertRaises(FinalValidationError):
            FinalEvidenceValidator().validate(state, analysis, 0)
        with self.assertRaises(ContractError):
            RepositoryContracts(ROOT).secondary_binding(
                chain="download",
                metric="下载完成率",
                parent_dimension="device_brand",
                parent_value="Brand A",
                child_dimension="os_major_version",
            )

    def test_secondary_candidate_state_tampering_is_revalidated_from_raw_result(self):
        runner, _ = self._complete("secondary-tamper")
        state = runner.load_state("secondary-tamper")
        game_candidate = state["steps"][0]["candidates"][0]
        original_denominator = game_candidate["private_counts"][
            "current_denominator"
        ]
        game_candidate["private_counts"]["current_denominator"] = -1
        runner._write_state(state)
        with self.assertRaisesRegex(RunnerError, "private counts"):
            runner.load_state("secondary-tamper")

        game_candidate["private_counts"][
            "current_denominator"
        ] = original_denominator
        runner._write_state(state)
        secondary = state["post_primary"]["steps"][1]
        secondary["candidates"][0]["adverse_impact_bp"] += 1
        runner._write_state(state)
        with self.assertRaisesRegex(RunnerError, "raw evidence"):
            runner.load_state("secondary-tamper")


if __name__ == "__main__":
    unittest.main()
