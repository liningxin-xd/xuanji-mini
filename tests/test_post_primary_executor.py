import unittest
from pathlib import Path

from runtime.runner import AttributionRunner


ROOT = Path(__file__).resolve().parents[1]


class PostPrimaryExecutorTest(unittest.TestCase):
    def setUp(self):
        self.executor = AttributionRunner(ROOT).post_primary_executor

    def test_current_and_active_query_follow_the_background_cursor(self):
        items = [
            {"id": "game_background", "status": "succeeded"},
            {"id": "game_background", "status": "planned"},
        ]
        background = {
            "id": "game_background",
            "status": "in_progress",
            "cursor": 1,
            "items": items,
        }
        state = {
            "post_primary": {
                "status": "executing",
                "steps": [
                    {"id": "secondary", "status": "succeeded"},
                    background,
                ],
            }
        }

        self.assertEqual(
            (background, items[1]), self.executor.current_query(state)
        )
        self.assertIsNone(self.executor.active_query(state))
        items[1]["status"] = "in_progress"
        self.assertEqual(
            (background, items[1]), self.executor.active_query(state)
        )

    def test_issue_query_freezes_the_shared_attempt_shape(self):
        binding = self.executor.contracts.game_background_binding(game_id=12345)
        built = self.executor.query_builder.build(
            binding,
            {"business_date": "2026-08-22", "game_id": 12345},
        )
        item = {"id": "game_background", "status": "planned", "attempts": []}
        post_step = {"id": "game_background", "status": "planned"}

        self.executor.issue_query(post_step, item, built, "sql/query.sql")

        self.assertEqual("in_progress", post_step["status"])
        self.assertEqual("in_progress", item["status"])
        self.assertEqual(
            [
                {
                    "attempt_no": 0,
                    "status": "issued",
                    "sql_sha256": built.sha256,
                    "sql_path": "sql/query.sql",
                    "query_id": None,
                    "error": None,
                    "event_path": None,
                    "raw_result_sha256": None,
                    "validation": None,
                }
            ],
            item["attempts"],
        )

    def test_semantic_errors_lock_for_two_repairs_then_become_terminal(self):
        raw_error = {
            "class": "semantic_analysis",
            "code": "ODPS-0130071",
            "message": "column is not in GROUP BY",
        }
        repair_item = {"status": "in_progress"}
        repair_attempt = self._attempt(0)
        advance = self.executor.record_error(
            query_item=repair_item,
            attempt=repair_attempt,
            attempt_no=0,
            raw_error=raw_error,
        )
        self.assertFalse(advance)
        self.assertEqual("repair_required", repair_item["status"])
        self.assertIsNone(repair_attempt["validation"])

        failed_item = {"status": "in_progress"}
        failed_attempt = self._attempt(2)
        advance = self.executor.record_error(
            query_item=failed_item,
            attempt=failed_attempt,
            attempt_no=2,
            raw_error=raw_error,
        )
        self.assertTrue(advance)
        self.assertEqual("failed", failed_item["status"])
        self.assertEqual("query_failed", failed_item["failure_code"])
        self.assertIn("after two evidence-based repairs", failed_item["reason"])
        self.assertEqual(failed_item["reason"], failed_attempt["validation"]["reason"])

    def test_permission_failure_and_background_aggregate_are_machine_classified(self):
        item = {"status": "in_progress"}
        attempt = self._attempt(0)
        advance = self.executor.record_error(
            query_item=item,
            attempt=attempt,
            attempt_no=0,
            raw_error={
                "class": "execution",
                "code": "ACCESS_DENIED",
                "message": "permission denied",
            },
        )
        self.assertTrue(advance)
        self.assertEqual("query_blocked", item["failure_code"])

        first = {"id": "game_background", "status": "failed"}
        second = {"id": "game_background", "status": "succeeded"}
        post_step = {
            "id": "game_background",
            "status": "in_progress",
            "cursor": 0,
            "items": [first, second],
        }
        self.executor.advance_background(post_step, first)
        self.assertEqual(1, post_step["cursor"])
        self.assertEqual("in_progress", post_step["status"])
        self.executor.advance_background(post_step, second)
        self.assertEqual("succeeded", post_step["status"])

    @staticmethod
    def _attempt(attempt_no: int) -> dict:
        return {
            "attempt_no": attempt_no,
            "status": "issued",
            "error": None,
            "validation": None,
        }


if __name__ == "__main__":
    unittest.main()
