import json
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.runner import AttributionRunner
from tests.runtime_result_fixtures import (
    raw_result_for_ticket,
    self_reported_result_event,
)


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = ROOT / "tests/fixtures/runtime/scenario-replays.json"
ANALYSIS_REPLAYS_PATH = ROOT / "tests/fixtures/runtime/analysis-replays.json"


class RuntimeCliTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = dict(os.environ)
        self.env["XUANJI_RUNS_ROOT"] = self.temp_dir.name
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)

    def _cli(self, *args, input_value=None, expected_returncode=0):
        completed = subprocess.run(
            [sys.executable, "-m", "runtime.runner", *args],
            cwd=ROOT,
            env=self.env,
            input=(json.dumps(input_value, ensure_ascii=False) if input_value else None),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            expected_returncode,
            completed.returncode,
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        output = completed.stdout if completed.returncode == 0 else completed.stderr
        return json.loads(output)

    def _analysis_context(self):
        return {
            "source": "dataworks_dqc",
            "project": "tap_dw",
            "table": "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d",
            "partition": "dt=2026-08-22",
            "investigation": {
                "rule_indexes": [0],
                "metric_hint": "下载完成率",
                "alert_partition": "dt=2026-08-22",
                "alert_rules": [{"rule_name": "【APK下载完成率】最近1天低于阈值"}],
            },
        }

    def test_verify_assets_cli(self):
        result = self._cli("verify-assets")
        self.assertEqual("ok", result["status"])
        self.assertEqual(17, result["verified_count"])

    def test_production_skill_excludes_the_development_runner_protocol(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for command in (
            "python3 -m runtime.runner init",
            "python3 -m runtime.runner next",
            "python3 -m runtime.runner record",
            "python3 -m runtime.runner export",
            "python3 -m runtime.runner writer-pack",
            "python3 -m runtime.runner assemble-final",
            "python3 -m runtime.runner validate-final",
        ):
            self.assertNotIn(command, skill)
        self.assertNotIn("HostDViewAdapter.execute_until_blocked(run_id)", skill)
        self.assertNotIn("ProductionDViewExecutor", skill)
        self.assertNotIn("`query_returned` / `query_error`", skill)
        for tool in ("xuanji_run_task", "xuanji_submit_repair", "xuanji_finalize"):
            self.assertIn(tool, skill)
        self.assertNotIn("实际提交 SQL 与票据一致性仍依赖调用方", skill)

    def test_cli_round_trip_reaches_export_and_final_validation(self):
        initialized = self._cli(
            "init",
            "--run-id",
            "cli-run",
            "--chain",
            "download",
            "--game-type",
            "app",
            "--metric",
            "下载完成率",
            "--alert-date",
            "2026-08-22",
            "--analysis-date",
            "2026-08-22",
            "--receipt-mode",
            "self_reported",
        )
        self.assertEqual(0, initialized["cursor"])

        while True:
            ticket = self._cli("next", "--run-id", "cli-run")
            if ticket["action"] == "queue_complete":
                break
            raw_result = raw_result_for_ticket(
                self.runner, "cli-run", ticket
            )
            self._cli(
                "record",
                "--run-id",
                "cli-run",
                input_value=self_reported_result_event(
                    ticket, raw_result, f"cli-{ticket['step_id']}"
                ),
            )

        writer_pack = self._cli("writer-pack", "--run-id", "cli-run")
        self.assertEqual("no_dominant_slice", writer_pack["result_status_hint"])
        patch_path = Path(self.temp_dir.name) / "writer-patch.json"
        patch_path.write_text(
            json.dumps(
                {
                    "summary": "下载完成率已完成固定一级队列检查。",
                    "finding_texts": {},
                    "evidence_limits": [],
                    "recommended_action": "继续跟踪下载完成率的后续业务日表现。",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        context_path = Path(self.temp_dir.name) / "analysis-context.json"
        context_path.write_text(
            json.dumps(self._analysis_context(), ensure_ascii=False), encoding="utf-8"
        )
        analysis = self._cli(
            "assemble-final",
            "--run-id",
            "cli-run",
            "--writer-patch",
            str(patch_path),
            "--analysis-context",
            str(context_path),
        )
        self.assertEqual("no_dominant_slice", analysis["investigations"][0]["status"])
        analysis_path = (
            Path(self.temp_dir.name) / "cli-run/final/assembled-analysis.json"
        )
        validated = self._cli(
            "validate-final",
            "--run-id",
            "cli-run",
            "--analysis-json",
            str(analysis_path),
            "--investigation-index",
            "0",
        )
        self.assertEqual("valid", validated["status"])
        state = self.runner.load_state("cli-run")
        self.assertEqual("finalized", state["status"])
        self.assertEqual(validated["analysis_sha256"], state["final_analysis_sha256"])

    def test_cli_rejects_cross_step_record(self):
        self._cli(
            "init",
            "--run-id",
            "cli-invalid",
            "--chain",
            "download",
            "--game-type",
            "app",
            "--metric",
            "下载完成率",
            "--alert-date",
            "2026-08-22",
            "--analysis-date",
            "2026-08-22",
            "--receipt-mode",
            "self_reported",
        )
        ticket = self._cli("next", "--run-id", "cli-invalid")
        error = self._cli(
            "record",
            "--run-id",
            "cli-invalid",
            input_value={
                "event": "query_returned",
                "step_id": "device_brand",
                "attempt_no": 0,
                "submitted_sql_sha256": ticket["rendered_sql_sha256"],
            },
            expected_returncode=2,
        )
        self.assertIn("non-current", error["error"])


class ScenarioReplayTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        self.scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))

    def test_fixed_queue_scenarios_replay_to_completion(self):
        for scenario in self.scenarios[:4]:
            with self.subTest(scenario=scenario["id"]):
                run_id = scenario["id"]
                self.runner.init_run(
                    run_id=run_id,
                    chain=scenario["chain"],
                    game_type=scenario["game_type"],
                    metric=scenario["metric"],
                    alert_date="2026-08-24",
                    receipt_mode="self_reported",
                )
                issued_steps = []
                while True:
                    ticket = self.runner.next_action(run_id)
                    if ticket["action"] == "queue_complete":
                        break
                    issued_steps.append(ticket["step_id"])
                    if ticket["step_id"] in scenario["failed_steps"]:
                        raw_result = raw_result_for_ticket(
                            self.runner, run_id, ticket
                        )
                        raw_result["rows"][0]["analysis_date"] = "2026-01-01"
                    else:
                        raw_result = raw_result_for_ticket(
                            self.runner,
                            run_id,
                            ticket,
                            candidate=ticket["step_id"]
                            in scenario["candidate_steps"],
                        )
                    event = self_reported_result_event(
                        ticket,
                        raw_result,
                        f"{run_id}-{ticket['step_id']}",
                    )
                    self.runner.record(run_id, event)
                self.assertEqual(scenario["expected_query_steps"], issued_steps)
                state = self.runner.load_state(run_id)
                self.assertTrue(state["ready_for_final_validation"])
                if scenario.get("expected_automatic_step"):
                    automatic = next(
                        step
                        for step in state["steps"]
                        if step["id"] == scenario["expected_automatic_step"]
                    )
                    self.assertEqual("skipped_not_applicable", automatic["status"])

    def test_semantic_analysis_regression_always_routes_to_repair(self):
        scenario = self.scenarios[4]
        self.runner.init_run(
            run_id="semantic-regression",
            chain=scenario["chain"],
            game_type=scenario["game_type"],
            metric=scenario["metric"],
            alert_date="2026-08-22",
            analysis_date="2026-08-22",
            receipt_mode="self_reported",
        )
        ticket = self.runner.next_action("semantic-regression")
        result = self.runner.record(
            "semantic-regression",
            {
                "event": "query_error",
                "step_id": ticket["step_id"],
                "attempt_no": ticket["attempt_no"],
                "receipt_type": "self_reported_receipt",
                "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                "query_id": "semantic-regression-game",
                "error_class": scenario["error_class"],
                "error_code": "ODPS-0130071",
                "error_message": "semantic check failed",
            },
        )
        self.assertEqual(scenario["expected_cursor"], result["cursor"])
        next_action = self.runner.next_action("semantic-regression")
        self.assertEqual(scenario["expected_next_action"], next_action["action"])


class AnalysisReplayTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)
        self.fixture = json.loads(ANALYSIS_REPLAYS_PATH.read_text(encoding="utf-8"))

    def test_raw_dqc_route_and_query_outputs_replay_to_expected_analysis(self):
        for scenario in self.fixture["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                route = scenario["routing"]
                raw_dqc = scenario["raw_dqc_input"]
                self.assertIn(route["metric"].replace("下载", ""), raw_dqc["ruleChecks"][0]["ruleName"])
                run_id = scenario["id"]
                self.runner.init_run(
                    run_id=run_id,
                    chain=route["chain"],
                    game_type=route["game_type"],
                    metric=route["metric"],
                    alert_date=route["alert_date"],
                    analysis_date=route["analysis_date"],
                    receipt_mode="self_reported",
                )
                while True:
                    ticket = self.runner.next_action(run_id)
                    if ticket["action"] == "queue_complete":
                        break
                    result_name = scenario["step_results"][ticket["step_id"]]
                    raw_result = copy.deepcopy(self.fixture["result_sets"][result_name])
                    self.runner.record(
                        run_id,
                        self_reported_result_event(
                            ticket,
                            raw_result,
                            f"{run_id}-{ticket['step_id']}",
                        ),
                    )
                state = self.runner.load_state(run_id)
                actual_counts = {
                    step["id"]: step["candidate_count"]
                    for step in state["steps"]
                    if step["candidate_count"]
                }
                self.assertEqual(scenario["expected"]["candidate_counts"], actual_counts)
                writer_pack = self.runner.build_writer_pack(run_id)
                finding_texts = {}
                if scenario["expected"]["status"] == "completed":
                    candidate = writer_pack["candidates"][0]
                    finding_texts[candidate["candidate_id"]] = (
                        "The replayed slice passed every machine gate."
                    )
                raw_rule = raw_dqc["ruleChecks"][0]
                context = {
                    "source": "dataworks_dqc",
                    "project": raw_dqc["projectName"],
                    "table": (
                        f"{raw_dqc['projectName']}.{raw_rule['tableName']}"
                    ),
                    "partition": raw_rule["actualExpression"],
                    "investigation": {
                        "rule_indexes": [0],
                        "metric_hint": route["metric"],
                        "alert_partition": raw_rule["actualExpression"],
                        "alert_rules": [{"rule_name": raw_rule["ruleName"]}],
                    },
                }
                analysis = self.runner.assemble_final(
                    run_id,
                    {
                        "summary": "The replayed queue completed.",
                        "finding_texts": finding_texts,
                        "evidence_limits": [],
                        "recommended_action": "Review the replayed primary evidence.",
                    },
                    context,
                )
                self.assertEqual(route["metric"], analysis["investigations"][0]["metric"])
                path = Path(self.temp_dir.name) / run_id / "final/assembled-analysis.json"
                receipt = self.runner.validate_final(run_id, path, 0)
                self.assertEqual(scenario["expected"]["status"], receipt["investigation_status"])


if __name__ == "__main__":
    unittest.main()
