import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.runner import AttributionRunner


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = ROOT / "tests/fixtures/runtime/scenario-replays.json"


class RuntimeCliTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = dict(os.environ)
        self.env["XUANJI_RUNS_ROOT"] = self.temp_dir.name

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

    def test_verify_assets_cli(self):
        result = self._cli("verify-assets")
        self.assertEqual("ok", result["status"])
        self.assertEqual(16, result["verified_count"])

    def test_skill_requires_the_runtime_runner_protocol(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for command in (
            "python3 -m runtime.runner init",
            "python3 -m runtime.runner next",
            "python3 -m runtime.runner record",
            "python3 -m runtime.runner export",
            "python3 -m runtime.runner validate-final",
        ):
            self.assertIn(command, skill)
        self.assertIn("不得直接修改 `.runs/*/state.json`", skill)
        self.assertIn("当前为任务票据模式", skill)

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
        )
        self.assertEqual(0, initialized["cursor"])

        while True:
            ticket = self._cli("next", "--run-id", "cli-run")
            if ticket["action"] == "queue_complete":
                break
            self._cli(
                "record",
                "--run-id",
                "cli-run",
                input_value={
                    "event": "query_succeeded",
                    "step_id": ticket["step_id"],
                    "attempt_no": ticket["attempt_no"],
                    "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                    "query_id": f"cli-{ticket['step_id']}",
                    "candidate_count": 0,
                    "warning_codes": [],
                    "raw_result": {"rows": []},
                },
            )

        execution = self._cli("export", "--run-id", "cli-run")
        analysis = {
            "investigations": [
                {
                    "status": "no_dominant_slice",
                    "attribution_execution": execution,
                }
            ]
        }
        analysis_path = Path(self.temp_dir.name) / "analysis.json"
        analysis_path.write_text(
            json.dumps(analysis, ensure_ascii=False), encoding="utf-8"
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
        )
        ticket = self._cli("next", "--run-id", "cli-invalid")
        error = self._cli(
            "record",
            "--run-id",
            "cli-invalid",
            input_value={
                "event": "query_succeeded",
                "step_id": "device_brand",
                "attempt_no": 0,
                "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                "candidate_count": 0,
                "warning_codes": [],
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
                    analysis_date="2026-08-22",
                )
                issued_steps = []
                while True:
                    ticket = self.runner.next_action(run_id)
                    if ticket["action"] == "queue_complete":
                        break
                    issued_steps.append(ticket["step_id"])
                    if ticket["step_id"] in scenario["failed_steps"]:
                        event = {
                            "event": "step_validation_failed",
                            "step_id": ticket["step_id"],
                            "attempt_no": ticket["attempt_no"],
                            "reason": "scenario family validation failed",
                            "warning_codes": ["result_incomplete"],
                        }
                    else:
                        event = {
                            "event": "query_succeeded",
                            "step_id": ticket["step_id"],
                            "attempt_no": ticket["attempt_no"],
                            "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                            "candidate_count": scenario["candidate_steps"].get(
                                ticket["step_id"], 0
                            ),
                            "warning_codes": [],
                            "raw_result": {"rows": []},
                        }
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
        )
        ticket = self.runner.next_action("semantic-regression")
        result = self.runner.record(
            "semantic-regression",
            {
                "event": "query_error",
                "step_id": ticket["step_id"],
                "attempt_no": ticket["attempt_no"],
                "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                "error_class": scenario["error_class"],
                "error_code": "ODPS-0130071",
                "error_message": "semantic check failed",
            },
        )
        self.assertEqual(scenario["expected_cursor"], result["cursor"])
        next_action = self.runner.next_action("semantic-regression")
        self.assertEqual(scenario["expected_next_action"], next_action["action"])


if __name__ == "__main__":
    unittest.main()
