from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime.runner import AttributionRunner, RunnerError
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


class ErrorCodeRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _new_runner(self, run_id: str, metric: str = "下载失败率") -> AttributionRunner:
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        runner.init_run(
            run_id=run_id,
            chain="download",
            game_type="app",
            metric=metric,
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        return runner

    def _complete(
        self,
        run_id: str,
        *,
        metric: str = "下载失败率",
        three_background_games: bool = False,
        error_code_mutator=None,
        error_code_failure: bool = False,
    ) -> tuple[AttributionRunner, int, list[dict]]:
        runner = self._new_runner(run_id, metric)
        query_count = 0
        tickets = []
        while True:
            ticket = runner.next_action(run_id)
            if ticket["action"] == "queue_complete":
                break
            query_count += 1
            tickets.append(ticket)
            if error_code_failure and ticket["step_id"] == "error_code":
                runner.record(
                    run_id,
                    {
                        "event": "query_error",
                        "step_id": "error_code",
                        "attempt_no": ticket["attempt_no"],
                        "receipt_type": "self_reported_receipt",
                        "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                        "query_id": f"private-error-code-{query_count}",
                        "error_class": "execution",
                        "error_code": "ODPS-PRIVATE",
                        "error_message": (
                            "SELECT secret FROM tap_dw.private_table "
                            "query_id=private-error-code /private/tmp/private.json"
                        ),
                    },
                )
                continue
            raw = raw_result_for_ticket(
                runner,
                run_id,
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            if three_background_games and ticket["step_id"] == "game_id":
                self._set_three_game_candidates(raw)
            elif three_background_games and ticket["step_id"] not in {
                "secondary",
                "game_background",
                "error_code",
            }:
                self._set_root_counts(raw, current_numerator=830)
            if ticket["step_id"] == "error_code" and error_code_mutator:
                error_code_mutator(raw)
            runner.record(
                run_id,
                self_reported_result_event(
                    ticket,
                    raw,
                    f"{run_id}-{ticket['step_id']}-{query_count}",
                ),
            )
        return runner, query_count, tickets

    @staticmethod
    def _set_root_counts(raw: dict, *, current_numerator: int) -> None:
        rows = raw["rows"]
        rows[0].update(
            {
                "current_denominator": 1,
                "current_numerator": 0,
                "baseline_denominator": 1,
                "baseline_numerator": 0,
            }
        )
        rows[1].update(
            {
                "current_denominator": 999,
                "current_numerator": current_numerator,
                "baseline_denominator": 999,
                "baseline_numerator": 800,
            }
        )
        for row in rows:
            row.update(
                {
                    "overall_current_denominator": 1000,
                    "overall_current_numerator": current_numerator,
                    "overall_baseline_denominator": 1000,
                    "overall_baseline_numerator": 800,
                }
            )
            if "overall_current_dimension_matched_denominator" in row:
                row.update(
                    {
                        "overall_current_dimension_matched_denominator": 1000,
                        "overall_baseline_dimension_matched_denominator": 1000,
                        "overall_current_dimension_match_rate": 1.0,
                        "overall_baseline_dimension_match_rate": 1.0,
                    }
                )

    @staticmethod
    def _set_three_game_candidates(raw: dict) -> None:
        template = raw["rows"][0]
        residual_template = raw["rows"][1]
        rows = []
        for game_id in ("12345", "23456", "34567"):
            row = copy.deepcopy(template)
            row.update(
                {
                    "dimension_value": game_id,
                    "dimension_label": f"Game {game_id}",
                    "current_denominator": 100,
                    "current_numerator": 85,
                    "baseline_denominator": 100,
                    "baseline_numerator": 80,
                }
            )
            rows.append(row)
        residual = copy.deepcopy(residual_template)
        residual.update(
            {
                "current_denominator": 700,
                "current_numerator": 575,
                "baseline_denominator": 700,
                "baseline_numerator": 560,
            }
        )
        rows.append(residual)
        for row in rows:
            row.update(
                {
                    "overall_current_denominator": 1000,
                    "overall_current_numerator": 830,
                    "overall_baseline_denominator": 1000,
                    "overall_baseline_numerator": 800,
                }
            )
        raw["rows"] = rows

    @staticmethod
    def _set_competing_error_code_facts(raw: dict) -> None:
        rows_by_scope = {}
        for row in raw["rows"]:
            rows_by_scope.setdefault(
                (row["scope"], row["focus_game_id"]), []
            ).append(row)

        expanded = []
        for (scope, _), rows in rows_by_scope.items():
            code_template = next(
                row for row in rows if row["bucket_kind"] == "error_code"
            )
            residual = copy.deepcopy(
                next(row for row in rows if row["bucket_kind"] == "residual")
            )
            code_count = 5 if scope == "overall" else 2
            current_entities = (
                min(
                    code_template["overall_current_affected_entities"],
                    max(1, code_template["current_business_denominator"] // 10),
                )
                if scope == "overall"
                else 1
            )
            scope_rows = []
            for offset in range(code_count):
                row = copy.deepcopy(code_template)
                row.update(
                    {
                        "error_code": str(1000 + offset),
                        "source_bucket_count": code_count + 1,
                        "current_error_events": max(100, current_entities),
                        "baseline_error_events": 0,
                        "current_affected_entities": current_entities,
                        "baseline_affected_entities": 0,
                        "current_entity_rate": current_entities
                        / row["current_business_denominator"],
                        "baseline_entity_rate": 0.0,
                        "current_repeats_per_entity": max(
                            100, current_entities
                        )
                        / current_entities,
                        "baseline_repeats_per_entity": None,
                    }
                )
                scope_rows.append(row)

            residual.update(
                {
                    "source_bucket_count": code_count + 1,
                    "current_error_events": residual[
                        "overall_current_affected_entities"
                    ],
                    "baseline_error_events": residual[
                        "overall_baseline_affected_entities"
                    ],
                    "current_affected_entities": residual[
                        "overall_current_affected_entities"
                    ],
                    "baseline_affected_entities": residual[
                        "overall_baseline_affected_entities"
                    ],
                    "current_entity_rate": residual[
                        "overall_current_affected_entities"
                    ]
                    / residual["current_business_denominator"],
                    "baseline_entity_rate": residual[
                        "overall_baseline_affected_entities"
                    ]
                    / residual["baseline_business_denominator"],
                    "current_repeats_per_entity": 1.0,
                    "baseline_repeats_per_entity": 1.0,
                }
            )
            scope_rows.append(residual)
            current_events = sum(row["current_error_events"] for row in scope_rows)
            baseline_events = sum(
                row["baseline_error_events"] for row in scope_rows
            )
            for row in scope_rows:
                row["overall_current_error_events"] = current_events
                row["overall_baseline_error_events"] = baseline_events
            expanded.extend(scope_rows)
        raw["rows"] = expanded

    def test_eligible_failure_rate_executes_one_calibration_query(self):
        runner, query_count, tickets = self._complete("error-code-eligible")
        self.assertEqual(10, query_count)
        error_tickets = [
            ticket for ticket in tickets if ticket["step_id"] == "error_code"
        ]
        self.assertEqual(1, len(error_tickets))
        self.assertEqual(12345, error_tickets[0]["parameters"]["focus_game_id"])

        state = runner.load_state("error-code-eligible")
        enhancement_plan = state["post_primary"]["enhancement_plan"]
        self.assertEqual(["error_code"], enhancement_plan["selected_modules"])
        self.assertEqual(1, enhancement_plan["query_module_count"])
        self.assertEqual([], enhancement_plan["evidence_limits"])
        error_code = state["post_primary"]["steps"][3]
        self.assertEqual("succeeded", error_code["status"])
        self.assertLessEqual(len(error_code["facts"]), 5)
        self.assertEqual(
            {"0200"}, {fact["code"] for fact in error_code["facts"]}
        )
        self.assertTrue(
            all(
                fact["meaning_status"] == "unconfirmed_version"
                for fact in error_code["facts"]
            )
        )

        pack = runner.build_writer_pack("error-code-eligible")
        self.assertEqual(
            {
                "plan_id": "direction_enhancement_v1",
                "max_query_modules": 2,
                "query_module_count": 1,
                "selected_modules": ["error_code"],
            },
            pack["enhancement_plan"],
        )
        self.assertEqual(error_code["facts"], pack["error_code_calibration"])
        encoded = json.dumps(pack, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded.encode("utf-8")), 12 * 1024)
        for forbidden in (
            "rendered_sql",
            "raw_result",
            "query_id",
            "receipt",
            "action_args",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_writer_pack_preserves_focus_facts_when_overall_has_five_stronger_codes(self):
        runner, _, _ = self._complete(
            "error-code-focus-budget",
            error_code_mutator=self._set_competing_error_code_facts,
        )
        facts = runner.load_state("error-code-focus-budget")["post_primary"][
            "steps"
        ][3]["facts"]
        overall = [fact for fact in facts if fact["scope"] == "overall"]
        focus = [fact for fact in facts if fact["scope"] == "focus_game"]
        self.assertEqual(3, len(overall))
        self.assertEqual(2, len(focus))
        self.assertGreater(
            min(fact["entity_rate_delta_bp"] for fact in overall),
            max(fact["entity_rate_delta_bp"] for fact in focus),
        )
        self.assertEqual(
            facts,
            runner.build_writer_pack("error-code-focus-budget")[
                "error_code_calibration"
            ],
        )

    def test_three_background_queries_reach_the_total_budget_of_twelve(self):
        runner, query_count, tickets = self._complete(
            "error-code-budget",
            three_background_games=True,
        )
        self.assertEqual(12, query_count)
        self.assertEqual(
            3,
            sum(ticket["step_id"] == "game_background" for ticket in tickets),
        )
        self.assertEqual(
            1, sum(ticket["step_id"] == "error_code" for ticket in tickets)
        )
        self.assertTrue(
            runner.load_state("error-code-budget")["ready_for_final_validation"]
        )

    def test_invalid_closure_fails_only_the_calibration_step(self):
        def break_closure(raw):
            raw["rows"][0]["source_bucket_count"] += 1

        runner, _, _ = self._complete(
            "error-code-invalid",
            error_code_mutator=break_closure,
        )
        state = runner.load_state("error-code-invalid")
        error_code = state["post_primary"]["steps"][3]
        self.assertEqual("failed", error_code["status"])
        self.assertEqual("result_incomplete", error_code["failure_code"])
        self.assertTrue(state["ready_for_final_validation"])
        pack = runner.build_writer_pack("error-code-invalid")
        self.assertIn("error_code:result_incomplete", pack["evidence_limits"])
        self.assertNotIn("error_code_calibration", pack)

    def test_trigger_thresholds_use_only_frozen_primary_evidence(self):
        runner, _, _ = self._complete("error-code-thresholds")
        state = runner.load_state("error-code-thresholds")
        selector = runner.calibration_runner.error_code_selector

        low_delta = copy.deepcopy(state)
        low_delta["canonical_root_metric"]["delta"] = 0.0004
        decision = selector.select(low_delta, low_delta["post_primary"])
        self.assertEqual("skipped_by_policy", decision["status"])
        self.assertEqual("root_adverse_delta_below_threshold", decision["reason"])

        low_entities = copy.deepcopy(state)
        for step in low_entities["steps"]:
            if step.get("candidate_count", 0) > 0:
                step["root_current_numerator"] = 99
        decision = selector.select(low_entities, low_entities["post_primary"])
        self.assertEqual("skipped_by_policy", decision["status"])
        self.assertEqual(
            "current_affected_entity_count_below_threshold", decision["reason"]
        )

        unsupported = copy.deepcopy(state)
        unsupported["metric"] = "下载完成率"
        decision = selector.select(unsupported, unsupported["post_primary"])
        self.assertEqual("skipped_by_policy", decision["status"])
        self.assertEqual("explicit_failed_signal_not_frozen", decision["reason"])

    def test_enhancement_plan_tampering_fails_closed_on_resume(self):
        runner, _, _ = self._complete("enhancement-plan-tamper")
        state = runner.load_state("enhancement-plan-tamper")
        state["post_primary"]["enhancement_plan"]["selected_modules"] = []
        runner._write_state(state)

        with self.assertRaisesRegex(RunnerError, "enhancement plan"):
            runner.next_action("enhancement-plan-tamper")

    def test_query_failure_details_stay_private(self):
        runner, _, _ = self._complete(
            "error-code-private-failure",
            error_code_failure=True,
        )
        state = runner.load_state("error-code-private-failure")
        private_reason = state["post_primary"]["steps"][3]["reason"]
        self.assertIn("SELECT secret", private_reason)
        pack = runner.build_writer_pack("error-code-private-failure")
        encoded = json.dumps(pack, ensure_ascii=False)
        self.assertIn("error_code:query_failed", pack["evidence_limits"])
        for forbidden in (
            "SELECT secret",
            "tap_dw.private_table",
            "private-error-code",
            "/private/tmp/private.json",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_resume_reuses_the_issued_error_code_query(self):
        runner = self._new_runner("error-code-resume")
        while True:
            ticket = runner.next_action("error-code-resume")
            if ticket["step_id"] == "error_code":
                break
            raw = raw_result_for_ticket(
                runner,
                "error-code-resume",
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            runner.record(
                "error-code-resume",
                self_reported_result_event(
                    ticket, raw, f"resume-{ticket['step_id']}"
                ),
            )

        resumed = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        resumed_ticket = resumed.next_action("error-code-resume")
        self.assertEqual(ticket["rendered_sql_sha256"], resumed_ticket["rendered_sql_sha256"])
        self.assertEqual(ticket["parameters"], resumed_ticket["parameters"])
        raw = raw_result_for_ticket(
            resumed, "error-code-resume", resumed_ticket
        )
        resumed.record(
            "error-code-resume",
            self_reported_result_event(resumed_ticket, raw, "resume-error-code"),
        )
        self.assertEqual(
            "queue_complete",
            resumed.next_action("error-code-resume")["action"],
        )

    def test_semantic_error_repair_preserves_the_frozen_query(self):
        runner = self._new_runner("error-code-repair")
        while True:
            ticket = runner.next_action("error-code-repair")
            if ticket["step_id"] == "error_code":
                break
            raw = raw_result_for_ticket(
                runner,
                "error-code-repair",
                ticket,
                candidate=ticket["step_id"] == "game_id",
            )
            runner.record(
                "error-code-repair",
                self_reported_result_event(
                    ticket, raw, f"repair-{ticket['step_id']}"
                ),
            )
        frozen_parameters = ticket["parameters"]
        runner.record(
            "error-code-repair",
            {
                "event": "query_error",
                "step_id": "error_code",
                "attempt_no": 0,
                "receipt_type": "self_reported_receipt",
                "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                "query_id": "repair-error-code-0",
                "error_class": "semantic_analysis",
                "error_code": "ODPS-0130071",
                "error_message": "column is not in GROUP BY",
            },
        )
        repair = runner.next_action("error-code-repair")
        self.assertEqual("repair_query", repair["action"])
        runner.record(
            "error-code-repair",
            {
                "event": "repair_submitted",
                "step_id": "error_code",
                "repair_attempt": 1,
                "repair_reason": "Normalize trailing statement whitespace.",
                "error_evidence": "The registered scope and columns are unchanged.",
                "repaired_sql": repair["original_sql"] + "\n",
            },
        )
        repaired_ticket = runner.next_action("error-code-repair")
        self.assertEqual(1, repaired_ticket["attempt_no"])
        self.assertEqual(frozen_parameters, repaired_ticket["parameters"])
        raw = raw_result_for_ticket(
            runner, "error-code-repair", repaired_ticket
        )
        runner.record(
            "error-code-repair",
            self_reported_result_event(
                repaired_ticket, raw, "repair-error-code-1"
            ),
        )
        state = runner.load_state("error-code-repair")
        attempts = state["post_primary"]["steps"][3]["attempts"]
        self.assertEqual(2, len(attempts))
        self.assertEqual("succeeded", state["post_primary"]["steps"][3]["status"])


if __name__ == "__main__":
    unittest.main()
