import hashlib
import json
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
PLAYBOOK_PATH = ROOT / "references" / "download-install-playbook.md"
REGISTRY_PATH = ROOT / "references" / "queries" / "alert-query-registry.yaml"
AUTHORITATIVE_REGISTRY_CHECKSUM = (
    "ade7324832b50b41011d4b97d3658092508a8c933a254158773ad9fb422436eb"
)


class DrilldownContractTest(unittest.TestCase):
    def test_download_keeps_both_mandatory_primary_families(self):
        content = PLAYBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "不能因为 `game_id` 已经达到反事实主导条件而省略或丢弃 "
            "`is_reserve_auto_download` 家族",
            content,
        )
        self.assertIn(
            "`game_id` 与 `is_reserve_auto_download` 两个规定一级家族",
            SKILL_PATH.read_text(encoding="utf-8"),
        )

    def test_download_fallback_order_is_fixed(self):
        content = PLAYBOOK_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"实际执行时按 `([^`]+)` 的稳定顺序",
            content,
        )

        self.assertIsNotNone(match)
        self.assertEqual(
            "device_brand -> channel_group -> app_major_version -> "
            "os_major_version -> apk_size_tier",
            match.group(1),
        )

    def test_sql_failures_require_outer_retry_instead_of_business_blocker(self):
        skill = SKILL_PATH.read_text(encoding="utf-8")
        playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("由调用方重新调用，不得伪装为业务阻塞", skill)
        self.assertIn("`semantic_analysis` 不能归入其中", playbook)
        self.assertIn("`query_failed`、普通调用失败和 `incomplete_analysis`", playbook)

    def test_local_insufficient_data_cannot_stop_the_dimension_plan(self):
        content = PLAYBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "任一家族因局部数据质量不足未形成可信结果时仍须继续完整计划，"
            "结束后返回 `insufficient_data`",
            content,
        )
        self.assertIn(
            "后一种情况不要求最后一个家族也数据不足，但不得已经形成合法候选，"
            "不能用它跳过中间维度",
            content,
        )
        self.assertIn(
            "只有全部适用家族都实际执行成功并分别形成可信的 "
            "`no_candidate|candidate_insufficient` 后仍无候选，才可返回 "
            "`no_dominant_slice`",
            content,
        )

    def test_blocker_registry_has_deterministic_formal_source_probes(self):
        registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))

        self.assertEqual(1, registry["schema_version"])
        probes = {item["id"]: item for item in registry["source_probes"]}
        self.assertEqual(3, len(probes))
        for probe in probes.values():
            self.assertEqual({"business_date", "game_type"}, set(probe["parameters"]))
            self.assertEqual(set(probe["source_tables"]), {
                table.lower()
                for table in re.findall(r"\b(tap_[a-z0-9_]+\.[a-z0-9_]+)\b", probe["sql"], re.I)
            })
            self.assertIn("${business_date}", probe["sql"])
            self.assertIn("${game_type}", probe["sql"])

        for query in registry["queries"]:
            probe = probes[query["source_probe_id"]]
            self.assertEqual(set(query["source_tables"]), set(probe["source_tables"]))
            self.assertIn(query["blocker_scope"], {"root", "shared_attribution", "local"})

    def test_blocker_registry_matches_the_daily_push_trust_anchor(self):
        registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
        canonical = json.dumps(
            registry,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        self.assertEqual(
            AUTHORITATIVE_REGISTRY_CHECKSUM,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def test_unregistered_later_dimensions_cannot_claim_blocker(self):
        registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
        registered_download_dimensions = {
            dimension
            for query in registry["queries"]
            if "download" in query["chains"]
            for dimension in query["dimensions"]
        }

        self.assertIn("game_id", registered_download_dimensions)
        self.assertIn("is_reserve_auto_download", registered_download_dimensions)
        self.assertTrue(
            {
                "device_brand",
                "channel_group",
                "app_major_version",
                "os_major_version",
                "apk_size_tier",
            }.isdisjoint(registered_download_dimensions)
        )


if __name__ == "__main__":
    unittest.main()
