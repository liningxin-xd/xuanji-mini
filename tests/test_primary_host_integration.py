import json
import tempfile
import unittest

from host_integration import PrimaryInvestigationHost
from runtime.host_adapter import DViewExecutionError
from runtime.runner import RunnerError
from tests.runtime_result_fixtures import raw_result_for_ticket


class PrimaryHostIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.service = None
        self.active_run_id = None
        self.query_count = 0
        self.semantic_error_once = False
        self.unexpected_error_once = False
        self.sink_error_once = False
        self.sink_calls = []
        self.service = PrimaryInvestigationHost(
            dview_query=self._query,
            receipt_key_id="primary-host-test",
            receipt_secret=b"h" * 32,
            runs_root=self.temp_dir.name,
            validated_result_sink=self._sink,
        )

    def _query(self, *, sql, database_type, limit):
        runner = self.service._runner
        ticket = runner.next_action(self.active_run_id)
        self.assertEqual(ticket["rendered_sql"], sql)
        self.assertEqual("MaxCompute", database_type)
        self.assertEqual(250, limit)
        self.query_count += 1
        if self.unexpected_error_once:
            self.unexpected_error_once = False
            raise RuntimeError("SELECT raw_result FROM private-query-id")
        if self.semantic_error_once:
            self.semantic_error_once = False
            raise DViewExecutionError(
                query_id=f"private-{self.active_run_id}-{self.query_count}",
                error_class="semantic_analysis",
                error_code="ODPS-0130071",
                error_message="column is not in GROUP BY",
            )
        raw_result = raw_result_for_ticket(
            runner,
            self.active_run_id,
            ticket,
        )
        return {
            "query_id": f"private-{self.active_run_id}-{self.query_count}",
            "columns": raw_result["columns"],
            "rows": raw_result["rows"],
        }

    def _sink(self, run_id, analysis, receipt):
        if self.sink_error_once:
            self.sink_error_once = False
            raise RuntimeError("query_id=private-sink-query raw_result=rows")
        self.sink_calls.append((run_id, analysis, receipt))

    def _run(self, run_id, chain, game_type, metric):
        self.active_run_id = run_id
        self.query_count = 0
        return self.service.xuanji_run_investigation(
            run_id=run_id,
            chain=chain,
            game_type=game_type,
            metric=metric,
            alert_date="2026-08-24",
            canonical_root_metric={
                "current_value": 0.79,
                "baseline_value": 0.80,
                "delta": -0.01,
            },
        )

    def _analysis_context(self, metric):
        return {
            "source": "dataworks_dqc",
            "project": "tap_dw",
            "table": "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d",
            "partition": "dt=2026-08-24",
            "investigation": {
                "rule_indexes": [0],
                "metric_hint": metric,
                "alert_partition": "dt=2026-08-24",
                "alert_rules": [{"rule_name": f"{metric} registered rule"}],
            },
        }

    def _writer_patch(self):
        return {
            "summary": (
                "已完成固定一级队列检查，未发现达到候选门槛的切片。"
            ),
            "finding_texts": {},
            "evidence_limits": [],
            "recommended_action": "继续跟踪后续业务日并核查对应链路。",
        }

    def _assert_no_private_evidence(self, value, private_query_prefix):
        encoded = json.dumps(value, ensure_ascii=False)
        for marker in (
            "rendered_sql",
            "raw_result",
            "receipt_signature",
            "submitted_sql_sha256",
            "query_id",
            private_query_prefix,
            "SELECT ",
            "WITH ",
            "| --- |",
        ):
            self.assertNotIn(marker, encoded)

    def test_download_and_apk_install_complete_behind_one_host_call(self):
        scenarios = (
            ("host-download-shadow", "download", "app", "下载完成率", 7),
            ("host-install-shadow", "install", "app", "下载安装完成率", 6),
        )
        for run_id, chain, game_type, metric, expected_queries in scenarios:
            with self.subTest(run_id=run_id):
                result = self._run(run_id, chain, game_type, metric)
                self.assertEqual("write_conclusion", result["action"])
                self.assertEqual(expected_queries, result["executed_query_count"])
                self.assertLessEqual(
                    len(
                        json.dumps(
                            result["writer_pack"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                    12 * 1024,
                )
                self._assert_no_private_evidence(result, f"private-{run_id}")
                state = self.service._runner.load_state(run_id)
                self.assertTrue(
                    all(
                        attempt.get("query_id", "").startswith(f"private-{run_id}")
                        and isinstance(attempt.get("raw_result_sha256"), str)
                        for step in state["steps"]
                        if step["automatic_status"] is None
                        for attempt in step["attempts"]
                    )
                )
                if chain == "install":
                    self.assertEqual(
                        [
                            "game_id",
                            "install_stage",
                            "device_brand",
                            "storage_headroom_tier",
                            "os_major_version",
                            "apk_size_tier",
                        ],
                        [step["id"] for step in state["steps"]],
                    )

    def test_semantic_repair_is_the_only_sql_bearing_public_action(self):
        self.active_run_id = "host-repair-shadow"
        self.semantic_error_once = True
        result = self.service.xuanji_run_investigation(
            run_id=self.active_run_id,
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-24",
            canonical_root_metric={
                "current_value": 0.79,
                "baseline_value": 0.80,
                "delta": -0.01,
            },
        )
        self.assertEqual("repair_required", result["action"])
        self.assertEqual(1, result["executed_query_count"])
        self.assertIn("original_sql", result["repair"])
        self.assertNotIn(f"private-{self.active_run_id}", json.dumps(result))

        repaired_sql = result["repair"]["original_sql"].replace(
            "scoped_rows", "scoped_rows_repaired"
        )
        resumed = self.service.xuanji_submit_repair(
            run_id=self.active_run_id,
            step_id=result["repair"]["step_id"],
            repair_attempt=result["repair"]["repair_attempt"],
            repair_reason="原始错误指出 CTE 别名作用域不一致",
            error_evidence="ODPS-0130071 points to a missing scoped alias",
            repaired_sql=repaired_sql,
        )
        self.assertEqual("write_conclusion", resumed["action"])
        self._assert_no_private_evidence(resumed, f"private-{self.active_run_id}")

    def test_finalize_returns_redacted_analysis_and_sinks_full_validated_artifact(self):
        run_id = "host-final-shadow"
        result = self._run(run_id, "download", "app", "下载完成率")
        self.assertEqual("write_conclusion", result["action"])
        finalized = self.service.xuanji_finalize(
            run_id=run_id,
            writer_patch=self._writer_patch(),
            analysis_context=self._analysis_context("下载完成率"),
        )
        self.assertEqual("finalized", finalized["action"])
        self.assertEqual("valid", finalized["validation_receipt"]["status"])
        self.assertIn("analysis_preview", finalized)
        self.assertNotIn("analysis", finalized)
        self._assert_no_private_evidence(finalized, f"private-{run_id}")
        self.assertEqual(1, len(self.sink_calls))
        _, authoritative, receipt = self.sink_calls[0]
        self.assertEqual("valid", receipt["status"])
        self.assertIn(
            "query_id",
            authoritative["investigations"][0]["attribution_execution"]["steps"][0],
        )

        repeated = self.service.xuanji_finalize(
            run_id=run_id,
            writer_patch=self._writer_patch(),
            analysis_context=self._analysis_context("下载完成率"),
        )
        self.assertEqual(finalized, repeated)
        self.assertEqual(2, len(self.sink_calls))

        changed_patch = self._writer_patch()
        changed_patch["summary"] = "不同的总结不允许覆盖已校验结果。"
        with self.assertRaisesRegex(RunnerError, "different writer patch"):
            self.service.xuanji_finalize(
                run_id=run_id,
                writer_patch=changed_patch,
                analysis_context=self._analysis_context("下载完成率"),
            )

    def test_private_callback_errors_do_not_leak_their_messages(self):
        self.active_run_id = "host-private-dview-error"
        self.unexpected_error_once = True
        with self.assertRaisesRegex(
            RunnerError,
            "DView Host call failed before a typed response: RuntimeError",
        ) as dview_error:
            self.service.xuanji_run_investigation(
                run_id=self.active_run_id,
                chain="download",
                game_type="app",
                metric="下载完成率",
                alert_date="2026-08-24",
                canonical_root_metric={
                    "current_value": 0.79,
                    "baseline_value": 0.80,
                    "delta": -0.01,
                },
            )
        self.assertNotIn("private-query-id", str(dview_error.exception))

        run_id = "host-private-sink-error"
        result = self._run(run_id, "download", "app", "下载完成率")
        self.assertEqual("write_conclusion", result["action"])
        self.sink_error_once = True
        with self.assertRaisesRegex(
            RunnerError,
            "validated result sink failed: RuntimeError",
        ) as sink_error:
            self.service.xuanji_finalize(
                run_id=run_id,
                writer_patch=self._writer_patch(),
                analysis_context=self._analysis_context("下载完成率"),
            )
        self.assertNotIn("private-sink-query", str(sink_error.exception))

        retried = self.service.xuanji_finalize(
            run_id=run_id,
            writer_patch=self._writer_patch(),
            analysis_context=self._analysis_context("下载完成率"),
        )
        self.assertEqual("finalized", retried["action"])

    def test_schema_v4_run_resumes_only_with_identical_immutable_identity(self):
        run_id = "host-v4-resume"
        first = self._run(run_id, "download", "app", "下载完成率")
        resumed = self._run(run_id, "download", "app", "下载完成率")
        self.assertEqual("write_conclusion", resumed["action"])
        self.assertEqual(0, resumed["executed_query_count"])
        self.assertEqual(first["writer_pack"], resumed["writer_pack"])

        with self.assertRaisesRegex(RunnerError, "immutable run state"):
            self._run(run_id, "download", "sandbox", "下载完成率")


if __name__ == "__main__":
    unittest.main()
