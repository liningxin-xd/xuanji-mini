import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.contracts import (
    ContractError,
    EXPECTED_LOCKED_ASSETS,
    RepositoryContracts,
)


ROOT = Path(__file__).resolve().parents[1]


class QueryAssetIntegrityTest(unittest.TestCase):
    def test_lock_hashes_match_every_registered_v1_asset(self):
        result = RepositoryContracts(ROOT).verify_assets()
        self.assertEqual(len(EXPECTED_LOCKED_ASSETS), result["verified_count"])
        self.assertEqual(
            EXPECTED_LOCKED_ASSETS,
            {asset["path"] for asset in result["assets"]},
        )

    def test_asset_modification_is_rejected_without_lock_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "contracts", temp_root / "contracts")
            shutil.copytree(ROOT / "references", temp_root / "references")
            path = temp_root / "references/queries/download-game-attribution.yaml"
            path.write_text(
                path.read_text(encoding="utf-8") + "\n# unreviewed drift\n",
                encoding="utf-8",
            )
            contracts = RepositoryContracts(temp_root)
            with self.assertRaisesRegex(ContractError, "hash mismatch"):
                contracts.verify_assets()

    def test_every_registry_query_path_is_locked_and_exists(self):
        contracts = RepositoryContracts(ROOT)
        for plan in contracts.plans.values():
            for metric in plan.allowed_metrics:
                for step in plan.steps:
                    binding = contracts.binding_for(
                        plan, step.id, metric, plan.allowed_game_types[0]
                    )
                    if binding is None:
                        continue
                    self.assertIn(binding.asset_path, contracts.asset_hashes)
                    self.assertTrue((ROOT / binding.asset_path).is_file())


if __name__ == "__main__":
    unittest.main()
