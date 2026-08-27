import json
import unittest
from pathlib import Path

from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import raw_result_for_ticket, self_reported_result_event


ROOT = Path(__file__).resolve().parents[1]


class ContextBudgetContractTest(unittest.TestCase):
    def test_primary_profile_documents_stay_within_the_runtime_budget(self):
        skill = (ROOT / "SKILL.md").read_bytes()
        guide = (ROOT / "references/runtime-writing-guide.md").read_text(
            encoding="utf-8"
        )
        self.assertLessEqual(len(skill), 4 * 1024)
        self.assertTrue(50 <= len(skill.decode("utf-8").splitlines()) <= 70)
        self.assertTrue(30 <= len(guide.splitlines()) <= 60)
        decoded = skill.decode("utf-8")
        self.assertIn("Host 是路由、定义和调查选择的唯一控制者", decoded)
        self.assertNotIn("完整读取 [DQC 告警路由表]", decoded)
        self.assertNotIn("通过 `taptap-data-analysis` 的 manifest", decoded)
        self.assertIn("正常路径禁止模型读取", decoded)

    def test_writer_pack_stays_compact_and_excludes_internal_evidence(self):
        import tempfile

        with tempfile.TemporaryDirectory() as runs_root:
            runner = AttributionRunner(ROOT, runs_root=runs_root)
            runner.init_run(
                run_id="context-budget",
                chain="download",
                game_type="app",
                metric="下载完成率",
                alert_date="2026-08-22",
                receipt_mode="self_reported",
            )
            while True:
                ticket = runner.next_action("context-budget")
                if ticket["action"] == "queue_complete":
                    break
                raw_result = raw_result_for_ticket(
                    runner,
                    "context-budget",
                    ticket,
                    candidate=ticket["step_id"] == "game_id",
                )
                runner.record(
                    "context-budget",
                    self_reported_result_event(
                        ticket,
                        raw_result,
                        f"context-{ticket['step_id']}",
                    ),
                )
            pack = runner.build_writer_pack("context-budget")
            encoded = json.dumps(
                pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.assertLessEqual(len(encoded), 12 * 1024)
            serialized = encoded.decode("utf-8")
            for forbidden in (
                "rendered_sql",
                "raw_result",
                "receipt_signature",
                "receipt_id",
                "sql_sha256",
                "state.json",
            ):
                self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
