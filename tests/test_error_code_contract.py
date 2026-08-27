import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from runtime.contracts import ContractError, RepositoryContracts


ROOT = Path(__file__).resolve().parents[1]


class ErrorCodeContractTest(unittest.TestCase):
    def setUp(self):
        self.contracts = RepositoryContracts(ROOT)

    def test_1_8a_registers_only_download_app_failure_rate(self):
        capability = self.contracts.error_code_capability("download", "app")
        self.assertEqual("registered_candidate", capability["source_status"])
        self.assertEqual(["下载失败率"], capability["supported_metrics"])
        self.assertEqual("allowed_after_trigger", capability["error_code_query"])
        self.assertEqual(
            "disabled_until_semantics_confirmed",
            capability["recovery_query"],
        )
        self.assertIsNone(capability["query_asset"])

        trigger = self.contracts.error_code_trigger(
            "download", "app", "下载失败率"
        )
        self.assertEqual("registered_not_enabled", trigger["status"])
        self.assertEqual(
            "frozen_root_metric",
            trigger["requirements"]["evidence_source"],
        )
        self.assertEqual(
            {"operator": "at_least", "value": 5},
            trigger["requirements"]["root_adverse_delta_bp"],
        )
        self.assertEqual(
            {"operator": "at_least", "value": 100},
            trigger["requirements"]["current_affected_entity_count"],
        )

    def test_every_current_route_has_an_explicit_decision(self):
        expected = {
            (plan.chain, game_type, metric)
            for plan in self.contracts.plans.values()
            for game_type in plan.allowed_game_types
            for metric in plan.allowed_metrics
        }
        actual = {
            (chain, game_type, metric)
            for chain, game_type, metric in expected
            if self.contracts.error_code_trigger(chain, game_type, metric)
        }
        self.assertEqual(expected, actual)

        registered = {
            key
            for key in actual
            if self.contracts.error_code_trigger(*key)["status"]
            == "registered_not_enabled"
        }
        self.assertEqual({("download", "app", "下载失败率")}, registered)

    def test_runtime_and_recovery_remain_disabled(self):
        raw = yaml.safe_load(
            (ROOT / "contracts/error-code-capabilities.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(raw["runtime_enabled"])
        self.assertNotIn(
            "error_code",
            self.contracts.analysis_profile("primary_v2")[
                "enabled_post_primary_steps"
            ],
        )
        for scope in raw["capabilities"].values():
            self.assertIsNone(scope["query_asset"])
            self.assertNotEqual("allowed", scope["recovery_query"])

    def test_source_contract_preserves_entity_and_redaction_boundaries(self):
        source = self.contracts.error_code_capability("download", "app")[
            "source"
        ]
        self.assertEqual(
            ["dt", "device_id", "game_id"], source["affected_entity_key"]
        )
        self.assertEqual("unmatched_code", source["unmatched_code_bucket"])
        self.assertEqual("redacted_category_only", source["public_info_policy"])
        self.assertEqual(
            "after_query_success_and_codes_frozen",
            source["dictionary"]["load_policy"],
        )

    def test_unsafe_contract_activation_is_rejected(self):
        mutations = (
            (
                "error-code-capabilities.yaml",
                lambda value: value.__setitem__("runtime_enabled", True),
                "runtime must remain disabled",
            ),
            (
                "error-code-capabilities.yaml",
                lambda value: value["capabilities"]["install_app"].__setitem__(
                    "recovery_query", "allowed"
                ),
                "capability scopes changed",
            ),
            (
                "error-code-triggers.yaml",
                lambda value: value["evidence_policy"].__setitem__(
                    "module_source_scan_before_trigger", True
                ),
                "evidence policy changed",
            ),
        )
        for filename, mutate, message in mutations:
            with self.subTest(filename=filename, message=message):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_root = Path(temp_dir)
                    shutil.copytree(ROOT / "contracts", temp_root / "contracts")
                    shutil.copytree(ROOT / "references", temp_root / "references")
                    path = temp_root / "contracts" / filename
                    value = yaml.safe_load(path.read_text(encoding="utf-8"))
                    mutate(value)
                    path.write_text(
                        yaml.safe_dump(
                            value, allow_unicode=True, sort_keys=False
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ContractError, message):
                        RepositoryContracts(temp_root)


if __name__ == "__main__":
    unittest.main()
