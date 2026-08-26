import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime.final_validator import FinalEvidenceValidator, FinalValidationError
from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]


class FinalEvidenceValidatorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        self.validator = FinalEvidenceValidator()

    def _complete_download(self, candidate_steps=None, failed_steps=None, warning_steps=None):
        candidate_steps = set(candidate_steps or ())
        failed_steps = set(failed_steps or ())
        warning_steps = set(warning_steps or ())
        self.runner.init_run(
            run_id="final-run",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            analysis_date="2026-08-22",
            receipt_mode="self_reported",
        )
        while True:
            ticket = self.runner.next_action("final-run")
            if ticket["action"] == "queue_complete":
                break
            raw_result = raw_result_for_ticket(
                self.runner,
                "final-run",
                ticket,
                candidate=ticket["step_id"] in candidate_steps,
            )
            if ticket["step_id"] in failed_steps:
                raw_result["rows"][0]["analysis_date"] = "2026-01-01"
            if ticket["step_id"] in warning_steps:
                for row in raw_result["rows"]:
                    if "overall_current_dimension_unmatched_denominator" in row:
                        total = row["overall_current_denominator"]
                        row["overall_current_dimension_matched_denominator"] = total - 1
                        row["overall_current_dimension_unmatched_denominator"] = 1
                        row["overall_current_dimension_match_rate"] = (
                            total - 1
                        ) / total
            event = self_reported_result_event(
                ticket, raw_result, f"query-{ticket['step_id']}"
            )
            self.runner.record("final-run", event)
        execution = self.runner.export("final-run")
        return self.runner.load_state("final-run"), execution

    def _analysis(self, status, execution, findings=None):
        investigation = {
            "status": status,
            "metric": "下载完成率",
            "analysis_date": "2026-08-22",
            "attribution_execution": execution,
        }
        if status in {"completed", "no_dominant_slice"}:
            state = self.runner.load_state("final-run")
            root_step = next(
                (
                    step
                    for step in state["steps"]
                    if step["status"] == "succeeded"
                    and step["produces_candidates"]
                ),
                None,
            )
            if root_step is not None:
                investigation.update(
                    {
                        "current_value": root_step["root_current_value"],
                        "baseline_value": root_step["root_baseline_value"],
                        "delta_bp": root_step["root_delta"] * 10000,
                    }
                )
            investigation.update(
                {
                    "summary": "The registered primary queue is complete.",
                    "evidence_limits": [],
                    "recommended_action": "Review the validated primary evidence.",
                }
            )
        elif status in self.validator.ALLOWED_FULL_QUEUE_STATUSES:
            investigation.update(
                {
                    "reason": "Every registered candidate family failed.",
                    "action": "Review the typed family failures.",
                }
            )
        if findings is not None:
            investigation["top_findings"] = findings
        return {"investigations": [investigation]}

    def _game_finding(self, state):
        candidate = state["steps"][0]["candidates"][0]
        return {
            "dimension": "game_id",
            "value": candidate["value"],
            "attribution_level": "primary",
            "adverse_impact_bp": candidate["adverse_impact_bp"],
            "finding": "The game slice is a validated adverse range.",
        }

    def test_completed_is_allowed_after_full_queue_with_a_positive_candidate(self):
        state, execution = self._complete_download({"game_id"})
        analysis = self._analysis("completed", execution, [self._game_finding(state)])
        result = self.validator.validate(state, analysis, 0)
        self.assertEqual("valid", result["status"])
        self.assertEqual(7, result["validated_step_count"])

    def test_no_dominant_slice_requires_successes_and_all_zero_candidates(self):
        state, execution = self._complete_download()
        analysis = self._analysis("no_dominant_slice", execution)
        self.assertEqual("valid", self.validator.validate(state, analysis, 0)["status"])

        analysis["investigations"][0]["top_findings"] = [
            {
                "dimension": "game_id",
                "value": "not-a-candidate",
                "attribution_level": "primary",
                "adverse_impact_bp": 5.0,
            }
        ]
        with self.assertRaises(FinalValidationError):
            self.validator.validate(state, analysis, 0)

    def test_missing_reordered_or_mismatched_steps_are_rejected(self):
        state, execution = self._complete_download({"game_id"})
        valid = self._analysis("completed", execution, [self._game_finding(state)])
        mutations = []
        missing = copy.deepcopy(valid)
        missing["investigations"][0]["attribution_execution"]["steps"].pop()
        mutations.append(missing)
        reordered = copy.deepcopy(valid)
        steps = reordered["investigations"][0]["attribution_execution"]["steps"]
        steps[0], steps[1] = steps[1], steps[0]
        mutations.append(reordered)
        mismatched = copy.deepcopy(valid)
        mismatched["investigations"][0]["attribution_execution"]["steps"][0][
            "candidate_count"
        ] = 0
        mutations.append(mismatched)
        for analysis in mutations:
            with self.subTest(analysis=analysis), self.assertRaises(FinalValidationError):
                self.validator.validate(state, analysis, 0)

    def test_failed_step_cannot_claim_zero_candidates_or_emit_a_finding(self):
        state, execution = self._complete_download(
            {"game_id"}, failed_steps={"device_brand"}
        )
        invalid_count = self._analysis("no_dominant_slice", copy.deepcopy(execution))
        invalid_count["investigations"][0]["attribution_execution"]["steps"][2][
            "candidate_count"
        ] = 0
        with self.assertRaisesRegex(FinalValidationError, "candidate_count"):
            self.validator.validate(state, invalid_count, 0)

        invalid_finding = self._analysis(
            "completed",
            execution,
            [
                {
                    **self._game_finding(state),
                    "dimension": "device_brand",
                    "value": "Brand A",
                }
            ],
        )
        with self.assertRaisesRegex(FinalValidationError, "positive candidate"):
            self.validator.validate(state, invalid_finding, 0)

    def test_finding_count_cannot_exceed_recorded_candidates(self):
        state, execution = self._complete_download({"game_id"})
        findings = [self._game_finding(state), self._game_finding(state)]
        analysis = self._analysis("completed", execution, findings)
        with self.assertRaisesRegex(FinalValidationError, "exceed"):
            self.validator.validate(state, analysis, 0)

    def test_unknown_query_id_is_rejected(self):
        state, execution = self._complete_download({"game_id"})
        analysis = self._analysis("completed", execution, [self._game_finding(state)])
        analysis["investigations"][0]["queries"] = [
            {"purpose": "untracked", "query_id": "unknown-query"}
        ]
        with self.assertRaisesRegex(FinalValidationError, "not recorded"):
            self.validator.validate(state, analysis, 0)

    def test_query_id_is_bound_to_its_exact_step(self):
        state, execution = self._complete_download({"game_id"})
        analysis = self._analysis("completed", execution, [self._game_finding(state)])
        steps = analysis["investigations"][0]["attribution_execution"]["steps"]
        steps[1]["query_id"] = steps[0]["query_id"]
        with self.assertRaisesRegex(FinalValidationError, "query_id mismatch"):
            self.validator.validate(state, analysis, 0)

    def test_warning_codes_cannot_be_omitted(self):
        state, execution = self._complete_download(warning_steps={"device_brand"})
        analysis = self._analysis("no_dominant_slice", execution)
        warned_step = analysis["investigations"][0]["attribution_execution"]["steps"][2]
        self.assertIn("warning_codes", warned_step)
        warned_step.pop("warning_codes")
        with self.assertRaisesRegex(FinalValidationError, "warning_codes"):
            self.validator.validate(state, analysis, 0)

    def test_unknown_status_and_mismatched_context_are_rejected(self):
        state, execution = self._complete_download()
        unknown = self._analysis("anything", execution)
        with self.assertRaisesRegex(FinalValidationError, "unknown"):
            self.validator.validate(state, unknown, 0)
        for field, value in (("metric", "下载失败率"), ("analysis_date", "2026-08-21")):
            analysis = self._analysis("no_dominant_slice", execution)
            analysis["investigations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(FinalValidationError):
                self.validator.validate(state, analysis, 0)

        wrong_mode = self._analysis("no_dominant_slice", copy.deepcopy(execution))
        wrong_mode["investigations"][0]["attribution_execution"][
            "execution_mode"
        ] = "trusted_host_adapter"
        with self.assertRaisesRegex(FinalValidationError, "execution_mode"):
            self.validator.validate(state, wrong_mode, 0)

    def test_export_does_not_finalize_and_validation_saves_hash_receipt(self):
        state, execution = self._complete_download()
        self.assertEqual("queue_complete", state["status"])
        analysis = self._analysis("no_dominant_slice", execution)
        path = Path(self.temp_dir.name) / "final-analysis.json"
        path.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
        receipt = self.runner.validate_final("final-run", path, 0)
        finalized = self.runner.load_state("final-run")
        self.assertEqual("finalized", finalized["status"])
        self.assertEqual(receipt, finalized["validation_receipt"])
        self.assertEqual(receipt["analysis_sha256"], finalized["final_analysis_sha256"])
        self.assertEqual("self_reported_development", receipt["execution_mode"])

    def test_all_candidate_families_failed_cannot_be_successful(self):
        failed = {
            "game_id",
            "is_reserve_auto_download",
            "device_brand",
            "channel_group",
            "app_major_version",
            "os_major_version",
            "apk_size_tier",
        }
        state, execution = self._complete_download(failed_steps=failed)
        for status in ("completed", "no_dominant_slice"):
            analysis = self._analysis(status, execution, [])
            with self.subTest(status=status), self.assertRaises(FinalValidationError):
                self.validator.validate(state, analysis, 0)

    def test_incomplete_queue_cannot_be_validated(self):
        self.runner.init_run(
            run_id="pending-run",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            analysis_date="2026-08-22",
            receipt_mode="self_reported",
        )
        state = self.runner.load_state("pending-run")
        analysis = {
            "investigations": [
                {
                    "status": "no_dominant_slice",
                    "metric": "下载完成率",
                    "analysis_date": "2026-08-22",
                    "attribution_execution": {
                        "mode": "full_queue",
                        "chain": "download",
                        "game_type": "app",
                        "steps": [],
                    },
                }
            ]
        }
        with self.assertRaisesRegex(FinalValidationError, "not complete"):
            self.validator.validate(state, analysis, 0)


if __name__ == "__main__":
    unittest.main()
