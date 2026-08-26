import hashlib
import tempfile
import unittest
from pathlib import Path

from runtime.query_builder import QueryBuildError
from runtime.runner import AttributionRunner, RunnerError


ROOT = Path(__file__).resolve().parents[1]
TRIAGE_PATH = ROOT / "references/sql-fast-triage.md"
GAME_QUERY_PATH = ROOT / "references/queries/download-game-attribution.yaml"


class ErrorRepairRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        self.runner.init_run(
            run_id="repair-run",
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            analysis_date="2026-08-22",
        )
        self.initial_ticket = self.runner.next_action("repair-run")

    def _semantic_error(self, ticket, message="column is not in GROUP BY"):
        return {
            "event": "query_error",
            "step_id": ticket["step_id"],
            "attempt_no": ticket["attempt_no"],
            "submitted_sql_sha256": ticket["rendered_sql_sha256"],
            "query_id": f"error-query-{ticket['attempt_no']}",
            "error_class": "semantic_analysis",
            "error_code": "ODPS-0130071",
            "error_message": message,
        }

    def _submit_semantic_error(self, ticket=None):
        ticket = ticket or self.initial_ticket
        return self.runner.record("repair-run", self._semantic_error(ticket))

    def _repair_event(self, ticket, repaired_sql):
        return {
            "event": "repair_submitted",
            "step_id": ticket["step_id"],
            "repair_attempt": ticket["repair_attempt"],
            "repair_reason": "原始错误指出 CTE 别名作用域不一致",
            "error_evidence": "ODPS-0130071 points to a missing scoped alias",
            "repaired_sql": repaired_sql,
        }

    def test_semantic_analysis_locks_cursor_and_injects_full_triage(self):
        result = self._submit_semantic_error()
        self.assertEqual(0, result["cursor"])
        self.assertEqual("repair_required", result["current_step"]["status"])
        with self.assertRaisesRegex(RunnerError, "cannot export"):
            self.runner.export("repair-run")

        repair_ticket = self.runner.next_action("repair-run")
        self.assertEqual("repair_query", repair_ticket["action"])
        self.assertTrue(repair_ticket["cursor_locked"])
        self.assertEqual(1, repair_ticket["repair_attempt"])
        self.assertEqual(
            TRIAGE_PATH.read_text(encoding="utf-8"), repair_ticket["triage_text"]
        )
        self.assertEqual("semantic_analysis", repair_ticket["raw_error"]["class"])
        self.assertIn("original_sql", repair_ticket)

    def test_two_repairs_then_failure_advances_only_the_current_family(self):
        self._submit_semantic_error()
        repair_one = self.runner.next_action("repair-run")
        repaired_one = repair_one["original_sql"].replace(
            "scoped_rows", "scoped_rows_repaired"
        )
        self.runner.record(
            "repair-run", self._repair_event(repair_one, repaired_one)
        )
        attempt_one = self.runner.next_action("repair-run")
        self.assertEqual(1, attempt_one["attempt_no"])

        self._submit_semantic_error(attempt_one)
        repair_two = self.runner.next_action("repair-run")
        self.assertEqual(2, repair_two["repair_attempt"])
        repaired_two = repair_two["original_sql"].replace(
            "bucket_aggregates", "bucket_aggregates_repaired"
        )
        self.runner.record(
            "repair-run", self._repair_event(repair_two, repaired_two)
        )
        attempt_two = self.runner.next_action("repair-run")
        self.assertEqual(2, attempt_two["attempt_no"])

        final_error = self.runner.record(
            "repair-run", self._semantic_error(attempt_two, "still invalid")
        )
        self.assertEqual(1, final_error["cursor"])
        self.assertEqual("pending", final_error["current_step"]["status"])
        next_family = self.runner.next_action("repair-run")
        self.assertEqual("is_reserve_auto_download", next_family["step_id"])
        state = self.runner.load_state("repair-run")
        self.assertEqual("failed", state["steps"][0]["status"])
        self.assertIn("two evidence-based repairs", state["steps"][0]["reason"])

    def test_repair_requires_reason_evidence_and_exact_attempt_number(self):
        self._submit_semantic_error()
        ticket = self.runner.next_action("repair-run")
        repaired = ticket["original_sql"].replace("scoped_rows", "scoped_rows_fixed")
        event = self._repair_event(ticket, repaired)
        event.pop("repair_reason")
        with self.assertRaisesRegex(RunnerError, "repair_reason"):
            self.runner.record("repair-run", event)
        event = self._repair_event(ticket, repaired)
        event["repair_attempt"] = 2
        with self.assertRaisesRegex(RunnerError, "must equal 1"):
            self.runner.record("repair-run", event)

    def test_semantic_error_requires_complete_raw_error(self):
        for missing_field in ("error_class", "error_code", "error_message"):
            with self.subTest(missing_field=missing_field):
                event = self._semantic_error(self.initial_ticket)
                event.pop(missing_field)
                with self.assertRaisesRegex(RunnerError, missing_field):
                    self.runner.record("repair-run", event)

    def test_repair_cannot_change_source_date_scope_or_metric_tokens(self):
        self._submit_semantic_error()
        ticket = self.runner.next_action("repair-run")
        illegal_repairs = (
            ticket["original_sql"].replace(
                "tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d",
                "tap_dw.some_other_table",
            ),
            ticket["original_sql"].replace("WHERE dt BETWEEN", "WHERE dt ="),
            ticket["original_sql"].replace(
                "is_download_complete", "is_explicit_failed"
            ),
            ticket["original_sql"] + "\nLIMIT 1",
        )
        for repaired_sql in illegal_repairs:
            with self.subTest(repaired_sql=repaired_sql[-80:]):
                with self.assertRaises(QueryBuildError):
                    self.runner.record(
                        "repair-run", self._repair_event(ticket, repaired_sql)
                    )

    def test_repair_never_modifies_the_source_query_asset(self):
        before = hashlib.sha256(GAME_QUERY_PATH.read_bytes()).hexdigest()
        self._submit_semantic_error()
        ticket = self.runner.next_action("repair-run")
        repaired = ticket["original_sql"].replace("scoped_rows", "scoped_rows_fixed")
        self.runner.record("repair-run", self._repair_event(ticket, repaired))
        after = hashlib.sha256(GAME_QUERY_PATH.read_bytes()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
