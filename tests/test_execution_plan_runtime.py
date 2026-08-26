import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from runtime.contracts import ContractError, RepositoryContracts


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


if __name__ == "__main__":
    unittest.main()
