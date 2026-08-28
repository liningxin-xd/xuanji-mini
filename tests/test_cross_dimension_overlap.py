from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import yaml

from runtime.contracts import RepositoryContracts
from runtime.cross_dimension_overlap_selector import CrossDimensionOverlapSelector
from runtime.runner import AttributionRunner, RunnerError
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


class CrossDimensionOverlapSelectorTest(unittest.TestCase):
    def setUp(self):
        self.selector = CrossDimensionOverlapSelector(RepositoryContracts(ROOT))

    def test_required_game_and_reserve_candidates_trigger_from_frozen_evidence(self):
        result = self.selector.select(self._state(left_bp=30.0, right_bp=25.0))

        self.assertEqual("planned", result["status"])
        self.assertEqual(
            ["game_id:12345", "is_reserve_auto_download:1"],
            [item["candidate_id"] for item in result["frozen_candidates"]],
        )
        self.assertEqual(25.0, result["minimum_candidate_adverse_impact_bp"])
        self.assertEqual(
            {
                "current_numerator": 790,
                "current_denominator": 1000,
                "baseline_numerator": 800,
                "baseline_denominator": 1000,
            },
            result["frozen_root_counts"],
        )

    def test_weaker_candidate_must_reach_root_relative_threshold(self):
        result = self.selector.select(self._state(left_bp=30.0, right_bp=24.9))
        self.assertEqual(
            {
                "status": "skipped_by_policy",
                "reason": "weaker_candidate_below_overlap_threshold",
            },
            result,
        )

    @staticmethod
    def _state(*, left_bp: float, right_bp: float) -> dict:
        root = {
            "current_numerator": 790,
            "current_denominator": 1000,
            "baseline_numerator": 800,
            "baseline_denominator": 1000,
        }

        def step(dimension: str, value: str, impact: float) -> dict:
            return {
                "id": dimension,
                "status": "succeeded",
                "candidate_count": 1,
                "candidates": [
                    {
                        "dimension": dimension,
                        "value": value,
                        "label": value,
                        "total_impact_bp": -impact,
                        "adverse_impact_bp": impact,
                        "private_counts": {
                            "current_numerator": 350,
                            "current_denominator": 500,
                            "baseline_numerator": 560,
                            "baseline_denominator": 700,
                        },
                    }
                ],
                **{f"root_{field}": item for field, item in root.items()},
            }

        return {
            "chain": "download",
            "metric": "下载完成率",
            "canonical_root_metric": {
                "current_value": 0.79,
                "baseline_value": 0.80,
                "delta": -0.01,
            },
            "steps": [
                step("game_id", "12345", left_bp),
                step("is_reserve_auto_download", "1", right_bp),
            ],
        }


class CrossDimensionOverlapQueryContractTest(unittest.TestCase):
    def setUp(self):
        self.contracts = RepositoryContracts(ROOT)
        self.path = (
            ROOT / "references/queries/download-cross-dimension-overlap.yaml"
        )
        self.spec = yaml.safe_load(self.path.read_text(encoding="utf-8"))

    def test_query_is_bounded_to_four_complete_quadrants(self):
        sql = self.spec["sql"]
        self.assertEqual(
            ["BOTH", "LEFT_ONLY", "RIGHT_ONLY", "NEITHER"],
            self.spec["quality"]["required_quadrants"],
        )
        self.assertEqual(4, self.spec["quality"]["max_rows"])
        self.assertNotRegex(sql, r"(?i)\bLIMIT\b")
        self.assertNotRegex(
            sql,
            r"(?is)COUNT\s*\(\s*DISTINCT\s+[^()]+,[^()]+\)",
        )
        for quadrant in ("BOTH", "LEFT_ONLY", "RIGHT_ONLY", "NEITHER"):
            self.assertIn(f"'{quadrant}'", sql)

    def test_all_download_metrics_use_registered_projection_and_frozen_values(self):
        for metric in self.contracts.plans["download"].allowed_metrics:
            binding = self.contracts.cross_dimension_overlap_binding(
                metric=metric,
                left_game_id=12345,
                right_reserve_value=1,
            )
            built = AttributionRunner(ROOT).query_builder.build(
                binding,
                {
                    "business_date": "2026-08-22",
                    "game_type": "app",
                    "left_game_id": 12345,
                    "right_reserve_value": 1,
                },
            )
            projection = self.contracts.registry["download_metrics"][metric][
                "secondary_metric"
            ]
            self.assertIn(projection["denominator_source_field"], built.sql)
            self.assertIn(projection["numerator_source_field"], built.sql)
            self.assertRegex(built.sql, r"(?i)game_id\s*=\s*12345")
            self.assertRegex(
                built.sql,
                r"(?i)is_reserve_auto_download\s*=\s*1",
            )
            self.assertIsNone(
                re.search(r"__[A-Z][A-Z0-9_]*__", built.sql)
            )


class CrossDimensionOverlapRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _complete(
        self,
        run_id: str,
        *,
        metric: str = "下载完成率",
        overlap_mutator=None,
    ) -> tuple[AttributionRunner, int, list[dict]]:
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
        tickets = []
        while True:
            ticket = runner.next_action(run_id)
            if ticket["action"] == "queue_complete":
                break
            tickets.append(ticket)
            raw = raw_result_for_ticket(
                runner,
                run_id,
                ticket,
                candidate=ticket["step_id"]
                in {"game_id", "is_reserve_auto_download"},
            )
            if ticket["step_id"] == "is_reserve_auto_download":
                candidate = next(
                    row for row in raw["rows"] if row["bucket_kind"] == "dimension"
                )
                candidate["dimension_value"] = "1"
                candidate["dimension_label"] = "reserve_auto_download"
            if ticket["step_id"] == "cross_dimension_overlap" and overlap_mutator:
                overlap_mutator(raw)
            runner.record(
                run_id,
                self_reported_result_event(
                    ticket,
                    raw,
                    f"{run_id}-{ticket['step_id']}-{len(tickets)}",
                ),
            )
        return runner, len(tickets), tickets

    def test_overlap_adds_one_query_and_calibrates_existing_candidates_only(self):
        runner, query_count, tickets = self._complete("overlap-runtime")

        self.assertEqual(10, query_count)
        overlap_tickets = [
            ticket
            for ticket in tickets
            if ticket["step_id"] == "cross_dimension_overlap"
        ]
        self.assertEqual(1, len(overlap_tickets))
        self.assertEqual(12345, overlap_tickets[0]["parameters"]["left_game_id"])
        self.assertEqual(1, overlap_tickets[0]["parameters"]["right_reserve_value"])

        state = runner.load_state("overlap-runtime")
        plan = state["post_primary"]["enhancement_plan"]
        self.assertEqual(["cross_dimension_overlap"], plan["selected_modules"])
        self.assertEqual(1, plan["query_module_count"])
        overlap = state["post_primary"]["steps"][5]
        self.assertEqual("succeeded", overlap["status"])
        self.assertEqual(
            ["BOTH", "LEFT_ONLY", "RIGHT_ONLY", "NEITHER"],
            [fact["quadrant"] for fact in overlap["facts"]],
        )

        pack = runner.build_writer_pack("overlap-runtime")
        calibration = pack["cross_dimension_overlap_calibration"]
        self.assertEqual("game_id:12345", calibration["left_candidate_id"])
        self.assertEqual(
            "is_reserve_auto_download:1",
            calibration["right_candidate_id"],
        )
        self.assertFalse(
            any(
                candidate["dimension"] == "cross_dimension_overlap"
                for candidate in pack["candidates"]
            )
        )

        state["post_primary"]["steps"][5]["facts"][0][
            "adverse_impact_bp"
        ] += 1
        runner._write_state(state)
        with self.assertRaisesRegex(RunnerError, "overlap result"):
            runner.next_action("overlap-runtime")

    def test_error_code_and_overlap_consume_the_two_module_budget(self):
        runner, query_count, _ = self._complete(
            "overlap-with-error-code", metric="下载失败率"
        )
        state = runner.load_state("overlap-with-error-code")
        self.assertEqual(11, query_count)
        self.assertEqual(
            ["error_code", "cross_dimension_overlap"],
            state["post_primary"]["enhancement_plan"]["selected_modules"],
        )
        self.assertEqual(
            2,
            state["post_primary"]["enhancement_plan"]["query_module_count"],
        )

    def test_broken_quadrant_closure_fails_only_overlap(self):
        def break_closure(raw: dict) -> None:
            raw["rows"][0]["current_numerator"] += 1

        runner, _, _ = self._complete(
            "overlap-invalid", overlap_mutator=break_closure
        )
        state = runner.load_state("overlap-invalid")
        overlap = state["post_primary"]["steps"][5]
        self.assertEqual("failed", overlap["status"])
        self.assertEqual("contribution_not_closed", overlap["failure_code"])
        self.assertTrue(state["ready_for_final_validation"])
        pack = runner.build_writer_pack("overlap-invalid")
        self.assertNotIn("cross_dimension_overlap_calibration", pack)
        self.assertIn(
            "cross_dimension_overlap:contribution_not_closed",
            pack["evidence_limits"],
        )

    def test_resume_reuses_the_issued_overlap_query(self):
        run_id = "overlap-resume"
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        runner.init_run(
            run_id=run_id,
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        while True:
            ticket = runner.next_action(run_id)
            if ticket["step_id"] == "cross_dimension_overlap":
                break
            raw = raw_result_for_ticket(
                runner,
                run_id,
                ticket,
                candidate=ticket["step_id"]
                in {"game_id", "is_reserve_auto_download"},
            )
            if ticket["step_id"] == "is_reserve_auto_download":
                candidate = next(
                    row
                    for row in raw["rows"]
                    if row["bucket_kind"] == "dimension"
                )
                candidate["dimension_value"] = "1"
                candidate["dimension_label"] = "reserve_auto_download"
            runner.record(
                run_id,
                self_reported_result_event(
                    ticket, raw, f"{run_id}-{ticket['step_id']}"
                ),
            )

        restarted = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        resumed_ticket = restarted.next_action(run_id)
        self.assertEqual(ticket["attempt_no"], resumed_ticket["attempt_no"])
        self.assertEqual(
            ticket["rendered_sql_sha256"],
            resumed_ticket["rendered_sql_sha256"],
        )
        self.assertEqual(ticket["parameters"], resumed_ticket["parameters"])
        state = restarted.load_state(run_id)
        overlap = state["post_primary"]["steps"][5]
        self.assertEqual(1, len(overlap["attempts"]))

        raw = raw_result_for_ticket(restarted, run_id, resumed_ticket)
        restarted.record(
            run_id,
            self_reported_result_event(
                resumed_ticket, raw, "overlap-resume-query"
            ),
        )
        self.assertEqual("queue_complete", restarted.next_action(run_id)["action"])


if __name__ == "__main__":
    unittest.main()
