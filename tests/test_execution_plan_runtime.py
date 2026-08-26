import shutil
import tempfile
import unittest
import json
from pathlib import Path

import yaml

from runtime.contracts import ContractError, RepositoryContracts
from runtime.runner import AttributionRunner, RunnerError


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DOWNLOAD = (
    "game_id",
    "is_reserve_auto_download",
    "device_brand",
    "channel_group",
    "app_major_version",
    "os_major_version",
    "apk_size_tier",
)
EXPECTED_INSTALL = (
    "game_id",
    "install_stage",
    "device_brand",
    "storage_headroom_tier",
    "os_major_version",
    "apk_size_tier",
)


class ExecutionPlanRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contracts = RepositoryContracts(ROOT)

    def test_download_app_and_sandbox_use_the_exact_fixed_queue(self):
        for game_type in ("app", "sandbox"):
            with self.subTest(game_type=game_type):
                plan = self.contracts.select_plan(
                    "download", game_type, "下载完成率"
                )
                self.assertEqual(EXPECTED_DOWNLOAD, tuple(step.id for step in plan.steps))
                self.assertTrue(all(step.failure_scope == "step" for step in plan.steps))

    def test_install_app_and_sandbox_use_the_exact_fixed_queue(self):
        app = self.contracts.select_plan("install", "app", "下载安装完成率")
        sandbox = self.contracts.select_plan(
            "install", "sandbox", "下载安装完成率"
        )
        self.assertEqual(EXPECTED_INSTALL, tuple(step.id for step in app.steps))
        self.assertEqual(EXPECTED_INSTALL, tuple(step.id for step in sandbox.steps))

        app_stage = app.steps[1]
        sandbox_stage = sandbox.steps[1]
        self.assertFalse(app_stage.produces_candidates)
        self.assertIsNone(app_stage.automatic_status)
        self.assertEqual("skipped_not_applicable", sandbox_stage.automatic_status)
        self.assertTrue(sandbox_stage.automatic_reason)

    def test_plan_queues_are_literal_matches_for_the_playbook(self):
        playbook = (ROOT / "references/download-install-playbook.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "game_id -> is_reserve_auto_download -> device_brand -> channel_group\n"
            "-> app_major_version -> os_major_version -> apk_size_tier",
            playbook,
        )
        self.assertIn(
            "game_id -> install_stage -> device_brand -> storage_headroom_tier\n"
            "-> os_major_version -> apk_size_tier",
            playbook,
        )

    def test_each_download_metric_has_an_independent_asset_binding(self):
        plan = self.contracts.select_plan("download", "app", "下载完成率")
        game_paths = set()
        template_paths = set()
        for metric in plan.allowed_metrics:
            game = self.contracts.binding_for(plan, "game_id", metric, "app")
            primary = self.contracts.binding_for(
                plan, "device_brand", metric, "app"
            )
            game_paths.add(game.asset_path)
            template_paths.add(primary.asset_path)
        self.assertEqual(4, len(game_paths))
        self.assertEqual(4, len(template_paths))

    def test_install_metric_cannot_bind_download_assets(self):
        with self.assertRaisesRegex(ContractError, "not allowed"):
            self.contracts.select_plan("install", "app", "下载完成率")

    def test_only_sandbox_install_stage_may_be_automatic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "contracts", temp_root / "contracts")
            shutil.copytree(ROOT / "references", temp_root / "references")
            path = temp_root / "contracts/execution-plans.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["plans"]["download"]["steps"][2]["automatic_status"] = (
                "skipped_not_applicable"
            )
            data["plans"]["download"]["steps"][2]["automatic_reason"] = "invalid"
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ContractError, "only install_sandbox"):
                RepositoryContracts(temp_root)


class DeterministicQueueRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runner = AttributionRunner(ROOT, runs_root=self.temp_dir.name)

    def _init_download(self, run_id="download-run"):
        return self.runner.init_run(
            run_id=run_id,
            chain="download",
            game_type="app",
            metric="下载完成率",
            alert_date="2026-08-22",
            analysis_date="2026-08-22",
        )

    def _success_event(self, ticket, candidate_count=0):
        return {
            "event": "query_succeeded",
            "step_id": ticket["step_id"],
            "attempt_no": ticket["attempt_no"],
            "submitted_sql_sha256": ticket["rendered_sql_sha256"],
            "query_id": f"query-{ticket['step_id']}",
            "candidate_count": candidate_count,
            "warning_codes": [],
            "raw_result": {"rows": []},
        }

    def test_candidate_discovery_does_not_end_or_reorder_the_queue(self):
        self._init_download()
        first = self.runner.next_action("download-run")
        self.assertEqual("game_id", first["step_id"])
        self.runner.record("download-run", self._success_event(first, 3))
        second = self.runner.next_action("download-run")
        self.assertEqual("is_reserve_auto_download", second["step_id"])

    def test_family_failure_advances_to_the_next_fixed_step(self):
        self._init_download()
        first = self.runner.next_action("download-run")
        self.runner.record(
            "download-run",
            {
                "event": "step_validation_failed",
                "step_id": "game_id",
                "attempt_no": 0,
                "query_id": "query-game",
                "reason": "result contribution did not close",
                "warning_codes": ["result_incomplete"],
            },
        )
        second = self.runner.next_action("download-run")
        self.assertEqual("is_reserve_auto_download", second["step_id"])

    def test_next_is_idempotent_until_the_current_result_is_recorded(self):
        self._init_download()
        first = self.runner.next_action("download-run")
        second = self.runner.next_action("download-run")
        self.assertEqual(first, second)

    def test_identical_record_event_is_idempotent(self):
        self._init_download()
        ticket = self.runner.next_action("download-run")
        event = self._success_event(ticket)
        first = self.runner.record("download-run", event)
        second = self.runner.record("download-run", event)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["revision"], second["revision"])

    def test_non_current_step_and_early_export_are_rejected(self):
        self._init_download()
        ticket = self.runner.next_action("download-run")
        event = self._success_event(ticket)
        event["step_id"] = "device_brand"
        with self.assertRaisesRegex(RunnerError, "non-current"):
            self.runner.record("download-run", event)
        with self.assertRaisesRegex(RunnerError, "cannot export"):
            self.runner.export("download-run")

    def test_sandbox_install_stage_is_automatically_skipped(self):
        self.runner.init_run(
            run_id="sandbox-install",
            chain="install",
            game_type="sandbox",
            metric="下载安装完成率",
            alert_date="2026-08-24",
            analysis_date="2026-08-22",
        )
        game = self.runner.next_action("sandbox-install")
        self.runner.record("sandbox-install", self._success_event(game, 1))
        brand = self.runner.next_action("sandbox-install")
        self.assertEqual("device_brand", brand["step_id"])
        state = self.runner.load_state("sandbox-install")
        self.assertEqual("skipped_not_applicable", state["steps"][1]["status"])

    def test_full_queue_exports_every_step_in_original_order(self):
        self._init_download()
        while True:
            ticket = self.runner.next_action("download-run")
            if ticket["action"] == "queue_complete":
                break
            count = 1 if ticket["step_id"] == "game_id" else 0
            self.runner.record(
                "download-run", self._success_event(ticket, count)
            )
        exported = self.runner.export("download-run")
        self.assertEqual("full_queue", exported["mode"])
        self.assertEqual(
            list(EXPECTED_DOWNLOAD), [step["step"] for step in exported["steps"]]
        )
        self.assertTrue(all(step["status"] == "succeeded" for step in exported["steps"]))

    def test_manual_step_deletion_is_detected(self):
        self._init_download()
        state_path = Path(self.temp_dir.name) / "download-run/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["steps"].pop()
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "integrity"):
            self.runner.status("download-run")


if __name__ == "__main__":
    unittest.main()
