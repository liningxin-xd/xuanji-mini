from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scripts.alert_card_glue import build_card, load_analysis, render, send_to_mock_lark
from scripts.mock_lark import Handler


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "demo-analysis.json"


class AlertCardGlueTest(unittest.TestCase):
    def test_demo_analysis_renders_complete_analysis_without_truncation(self) -> None:
        card = build_card(load_analysis(FIXTURE))
        serialized = json.dumps(card, ensure_ascii=False)

        self.assertEqual("2.0", card["schema"])
        self.assertEqual("璇玑 Mini · DQC 分析", card["header"]["title"]["content"])
        self.assertIn("示例游戏 A", serialized)
        self.assertIn("示例游戏 B", serialized)
        self.assertIn("示例游戏 C", serialized)
        self.assertIn("完成率较前 7 个完整 cohort 日的池化基线下降约 22bp。", serialized)
        self.assertIn("优先排查方向", serialized)
        self.assertNotIn("证据边界", serialized)
        self.assertIn("指标定义不足", serialized)
        self.assertNotIn("demo-query-id-not-rendered", serialized)

    def test_invalid_analysis_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            path.write_text('{"source":"dataworks_dqc","overall_status":"partial","investigations":[{"status":"unknown"}]}')
            with self.assertRaisesRegex(ValueError, "status is invalid"):
                load_analysis(path)

    def test_invalid_slice_finding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            payload = {
                "source": "dataworks_dqc",
                "overall_status": "completed",
                "investigations": [
                    {
                        "status": "completed",
                        "rule_indexes": [0],
                        "top_findings": [
                            {
                                "adverse_impact_bp": 42.96,
                                "finding": "整体指标下降",
                            }
                        ],
                    }
                ],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "dimension"):
                load_analysis(path)

    def test_renderer_does_not_fallback_to_unknown_slice(self) -> None:
        payload = {
            "source": "dataworks_dqc",
            "project": "tap_dw",
            "table": "tap_dw.example",
            "partition": "dt=2026-08-19",
            "overall_status": "completed",
            "investigations": [
                {
                    "status": "completed",
                    "rule_indexes": [0],
                    "metric": "沙盒下载完成率",
                    "summary": "整体指标下降约 42.96bp",
                    "top_findings": [
                        {
                            "adverse_impact_bp": 42.96,
                            "finding": "这不是具体切片结论",
                        }
                    ],
                    "counterfactual": {"finding": "尚未执行游戏维度反事实"},
                }
            ],
        }

        serialized = json.dumps(build_card(payload), ensure_ascii=False)

        self.assertIn("整体指标下降约 42.96bp", serialized)
        self.assertNotIn("未知切片", serialized)
        self.assertNotIn("这不是具体切片结论", serialized)
        self.assertNotIn("尚未执行游戏维度反事实", serialized)

    def test_rule_indexes_are_required_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            for rule_indexes in (None, [], [1, 0], [0, 0], [True]):
                payload = {
                    "source": "dataworks_dqc",
                    "overall_status": "partial",
                    "investigations": [
                        {
                            "status": "insufficient_definition",
                            "rule_indexes": rule_indexes,
                        }
                    ],
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(rule_indexes=rule_indexes), self.assertRaisesRegex(
                    ValueError, "rule_indexes"
                ):
                    load_analysis(path)

    def test_render_writes_card_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "card.json"
            render(FIXTURE, output)
            self.assertEqual("2.0", json.loads(output.read_text())["schema"])

    def test_send_reaches_local_mock_lark(self) -> None:
        Handler.received_cards = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            card = build_card(load_analysis(FIXTURE))
            message_id = send_to_mock_lark(
                f"http://127.0.0.1:{server.server_port}", "demo-chat", card
            )
            self.assertEqual("om_mock_xuanji", message_id)
            self.assertEqual(1, len(Handler.received_cards))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_send_refuses_non_local_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "localhost"):
            send_to_mock_lark("https://open.feishu.cn", "chat", {})


if __name__ == "__main__":
    unittest.main()
