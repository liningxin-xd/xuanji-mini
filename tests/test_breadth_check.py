from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from runtime.breadth_selector import BreadthSelector
from runtime.contracts import RepositoryContracts
from runtime.runner import AttributionRunner, RunnerError
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


class BreadthSelectorTest(unittest.TestCase):
    def setUp(self):
        self.selector = BreadthSelector(RepositoryContracts(ROOT))

    def test_two_non_candidate_buckets_can_reduce_focus_specificity(self):
        state = self._state(
            metric="下载完成率",
            rates=((0.75, 0.80), (0.77, 0.80), (0.775, 0.80)),
        )

        result = self.selector.select(state)

        self.assertEqual("succeeded", result["status"])
        self.assertEqual(0, result["query_count"])
        self.assertEqual(1, len(result["calibrations"]))
        calibration = result["calibrations"][0]
        self.assertEqual("device_brand:brand-a", calibration["candidate_id"])
        self.assertEqual("broad_change", calibration["specificity_status"])
        self.assertEqual(2, calibration["supporting_bucket_count"])
        self.assertEqual(
            ["brand-b", "brand-c"],
            [item["value"] for item in calibration["supporting_buckets"]],
        )

    def test_wrong_direction_or_below_half_does_not_trigger(self):
        state = self._state(
            metric="下载完成率",
            rates=((0.75, 0.80), (0.78, 0.80), (0.82, 0.80)),
        )
        result = self.selector.select(state)
        self.assertEqual(
            {"status": "skipped_by_policy", "reason": "no_broad_primary_family"},
            result,
        )

    def test_lower_is_better_direction_is_normalized(self):
        state = self._state(
            metric="下载失败率",
            rates=((0.10, 0.05), (0.08, 0.05), (0.075, 0.05)),
        )
        result = self.selector.select(state)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(
            "device_brand:brand-a", result["calibrations"][0]["candidate_id"]
        )

    @staticmethod
    def _state(*, metric: str, rates: tuple[tuple[float, float], ...]) -> dict:
        values = ("brand-a", "brand-b", "brand-c")
        buckets = [
            {
                "value": value,
                "label": value.title(),
                "current_rate": current,
                "baseline_rate": baseline,
            }
            for value, (current, baseline) in zip(values, rates, strict=True)
        ]
        return {
            "metric": metric,
            "steps": [
                {
                    "id": "device_brand",
                    "status": "succeeded",
                    "candidate_count": 1,
                    "candidates": [
                        {
                            "value": "brand-a",
                            "label": "Brand-A",
                            "adverse_impact_bp": 100.0,
                        }
                    ],
                    "breadth_buckets": buckets,
                }
            ],
        }


class BreadthRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_breadth_check_reuses_primary_rows_without_an_extra_query(self):
        runner = AttributionRunner(
            ROOT,
            runs_root=self.temp_dir.name,
            analysis_profile="primary_v2",
        )
        runner.init_run(
            run_id="breadth-runtime",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            receipt_mode="self_reported",
        )
        query_count = 0
        while True:
            ticket = runner.next_action("breadth-runtime")
            if ticket["action"] == "queue_complete":
                break
            query_count += 1
            raw = raw_result_for_ticket(
                runner, "breadth-runtime", ticket, candidate=False
            )
            if ticket["step_id"] == "device_brand":
                self._set_broad_brand_rows(raw)
            runner.record(
                "breadth-runtime",
                self_reported_result_event(
                    ticket,
                    raw,
                    f"breadth-runtime-{ticket['step_id']}",
                ),
            )

        self.assertEqual(7, query_count)
        state = runner.load_state("breadth-runtime")
        breadth = next(
            step
            for step in state["post_primary"]["steps"]
            if step["id"] == "breadth_check"
        )
        self.assertEqual("succeeded", breadth["status"])
        self.assertEqual(0, breadth["query_count"])
        self.assertEqual(
            0,
            state["post_primary"]["enhancement_plan"]["query_module_count"],
        )

        pack = runner.build_writer_pack("breadth-runtime")
        brands = [
            candidate
            for candidate in pack["candidates"]
            if candidate["dimension"] == "device_brand"
        ]
        self.assertEqual(3, len(brands))
        self.assertTrue(
            all(
                candidate["breadth_calibration"]["specificity_status"]
                == "broad_change"
                for candidate in brands
            )
        )
        self.assertIn(
            {"step": "breadth_check", "status": "succeeded"},
            pack["post_primary_steps"],
        )

        state["post_primary"]["steps"][3]["calibrations"] = []
        runner._write_state(state)
        with self.assertRaisesRegex(RunnerError, "breadth check"):
            runner.next_action("breadth-runtime")

    @staticmethod
    def _set_broad_brand_rows(raw: dict) -> None:
        business_template = raw["rows"][0]
        residual_template = raw["rows"][1]
        buckets = (
            ("brand-a", "Brand A", 200, 200, 150, 160),
            ("brand-b", "Brand B", 200, 200, 152, 160),
            ("brand-c", "Brand C", 200, 200, 154, 160),
        )
        rows = []
        for value, label, current_den, baseline_den, current_num, baseline_num in (
            buckets
        ):
            row = copy.deepcopy(business_template)
            row.update(
                {
                    "dimension_value": value,
                    "dimension_label": label,
                    "current_denominator": current_den,
                    "baseline_denominator": baseline_den,
                    "current_numerator": current_num,
                    "baseline_numerator": baseline_num,
                }
            )
            rows.append(row)
        residual = copy.deepcopy(residual_template)
        residual.update(
            {
                "current_denominator": 400,
                "baseline_denominator": 400,
                "current_numerator": 334,
                "baseline_numerator": 320,
            }
        )
        rows.append(residual)
        for row in rows:
            row.update(
                {
                    "overall_current_denominator": 1000,
                    "overall_baseline_denominator": 1000,
                    "overall_current_numerator": 790,
                    "overall_baseline_numerator": 800,
                    "collapsed_source_bucket_count": 1,
                    "source_bucket_count": 4,
                    "overall_current_dimension_matched_denominator": 1000,
                    "overall_baseline_dimension_matched_denominator": 1000,
                }
            )
        raw["rows"] = rows


if __name__ == "__main__":
    unittest.main()
