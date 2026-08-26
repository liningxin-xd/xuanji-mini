import copy
import tempfile
import unittest
from pathlib import Path

from runtime.final_validator import FinalEvidenceValidator, FinalValidationError
from runtime.runner import AttributionRunner


ROOT = Path(__file__).resolve().parents[1]


class FinalEvidenceValidatorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        self.validator = FinalEvidenceValidator()

    def _complete_download(self, candidate_counts=None, failed_steps=None):
        candidate_counts = candidate_counts or {}
        failed_steps = set(failed_steps or ())
        self.runner.init_run(
            run_id="final-run",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            analysis_date="2026-08-22",
        )
        while True:
            ticket = self.runner.next_action("final-run")
            if ticket["action"] == "queue_complete":
                break
            if ticket["step_id"] in failed_steps:
                event = {
                    "event": "step_validation_failed",
                    "step_id": ticket["step_id"],
                    "attempt_no": ticket["attempt_no"],
                    "query_id": f"query-{ticket['step_id']}",
                    "reason": f"{ticket['step_id']} result did not close",
                    "warning_codes": ["result_incomplete"],
                }
            else:
                event = {
                    "event": "query_succeeded",
                    "step_id": ticket["step_id"],
                    "attempt_no": ticket["attempt_no"],
                    "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                    "query_id": f"query-{ticket['step_id']}",
                    "candidate_count": candidate_counts.get(ticket["step_id"], 0),
                    "warning_codes": [],
                    "raw_result": {"rows": []},
                }
            self.runner.record("final-run", event)
        execution = self.runner.export("final-run")
        return self.runner.load_state("final-run"), execution

    def _analysis(self, status, execution, findings=None):
        investigation = {
            "status": status,
            "attribution_execution": execution,
        }
        if findings is not None:
            investigation["top_findings"] = findings
        return {"investigations": [investigation]}

    def _game_finding(self):
        return {
            "dimension": "game_id",
            "value": "12345",
            "attribution_level": "primary",
            "adverse_impact_bp": 12.0,
            "finding": "Game 12345 is a validated adverse slice.",
        }

    def test_completed_is_allowed_after_full_queue_with_a_positive_candidate(self):
        state, execution = self._complete_download({"game_id": 1})
        analysis = self._analysis("completed", execution, [self._game_finding()])
        result = self.validator.validate(state, analysis, 0)
        self.assertEqual("valid", result["status"])
        self.assertEqual(7, result["validated_step_count"])

    def test_no_dominant_slice_requires_successes_and_all_zero_candidates(self):
        state, execution = self._complete_download()
        analysis = self._analysis("no_dominant_slice", execution)
        self.assertEqual("valid", self.validator.validate(state, analysis, 0)["status"])

        analysis["investigations"][0]["top_findings"] = [self._game_finding()]
        with self.assertRaises(FinalValidationError):
            self.validator.validate(state, analysis, 0)

    def test_missing_reordered_or_mismatched_steps_are_rejected(self):
        state, execution = self._complete_download({"game_id": 1})
        valid = self._analysis("completed", execution, [self._game_finding()])
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
        state, execution = self._complete_download(failed_steps={"device_brand"})
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
                    **self._game_finding(),
                    "dimension": "device_brand",
                    "value": "Brand A",
                }
            ],
        )
        with self.assertRaisesRegex(FinalValidationError, "positive candidate"):
            self.validator.validate(state, invalid_finding, 0)

    def test_finding_count_cannot_exceed_recorded_candidates(self):
        state, execution = self._complete_download({"game_id": 1})
        findings = [self._game_finding(), {**self._game_finding(), "value": "67890"}]
        analysis = self._analysis("completed", execution, findings)
        with self.assertRaisesRegex(FinalValidationError, "exceed"):
            self.validator.validate(state, analysis, 0)

    def test_unknown_query_id_is_rejected(self):
        state, execution = self._complete_download({"game_id": 1})
        analysis = self._analysis("completed", execution, [self._game_finding()])
        analysis["investigations"][0]["queries"] = [
            {"purpose": "untracked", "query_id": "unknown-query"}
        ]
        with self.assertRaisesRegex(FinalValidationError, "not recorded"):
            self.validator.validate(state, analysis, 0)

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
        )
        state = self.runner.load_state("pending-run")
        analysis = self._analysis(
            "no_dominant_slice",
            {"mode": "full_queue", "chain": "download", "game_type": "app", "steps": []},
        )
        with self.assertRaisesRegex(FinalValidationError, "not complete"):
            self.validator.validate(state, analysis, 0)


if __name__ == "__main__":
    unittest.main()
