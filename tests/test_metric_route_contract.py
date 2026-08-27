import hashlib
import json
import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in minimal environments
    yaml = None

from runtime.contracts import RepositoryContracts, canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
ROUTING_PATH = ROOT / "references" / "dqc-alert-routing.md"
ROUTE_CONTRACT_PATH = ROOT / "contracts" / "dqc-routes.yaml"
REGISTERED_ROOT_PATH = ROOT / "references" / "queries" / "registered-monitor-root.yaml"
DOWNLOAD_ATTRIBUTION_PATH = (
    ROOT / "references" / "queries" / "download-game-attribution.yaml"
)
DOWNLOAD_PRIMARY_TEMPLATE_PATH = (
    ROOT / "references" / "queries" / "download-primary-attribution-template.md"
)
PLAYBOOK_PATH = ROOT / "references" / "download-install-playbook.md"
QUERY_ASSETS = {
    "下载完成率": (
        DOWNLOAD_ATTRIBUTION_PATH,
        DOWNLOAD_PRIMARY_TEMPLATE_PATH,
        "is_download_complete",
        "download_sample_flag",
    ),
    "下载失败率": (
        ROOT / "references" / "queries" / "download-failed-rate-game-attribution.yaml",
        ROOT
        / "references"
        / "queries"
        / "download-failed-rate-primary-attribution-template.md",
        "is_explicit_failed",
        "download_sample_flag",
    ),
    "下载失败次数比率": (
        ROOT
        / "references"
        / "queries"
        / "download-failed-pv-rate-game-attribution.yaml",
        ROOT
        / "references"
        / "queries"
        / "download-failed-pv-rate-primary-attribution-template.md",
        "game_download_failed_cnt_1d",
        "game_download_cnt_1d",
    ),
    "下载人为停止率": (
        ROOT / "references" / "queries" / "download-stop-rate-game-attribution.yaml",
        ROOT
        / "references"
        / "queries"
        / "download-stop-rate-primary-attribution-template.md",
        "is_human_stop",
        "download_sample_flag",
    ),
}


def _route_rows():
    return yaml.safe_load(ROUTE_CONTRACT_PATH.read_text(encoding="utf-8"))["routes"]


class MetricRouteContractTest(unittest.TestCase):
    def test_payload_pass_operators_are_complements_of_alert_operators(self):
        routing = ROUTING_PATH.read_text(encoding="utf-8")
        expected_pairs = {
            "<": ">=",
            "<=": ">",
            ">": "<=",
            ">=": "<",
            "==": "!=",
            "!=": "==",
        }

        for alert_operator, pass_operator in expected_pairs.items():
            self.assertIn(
                f"| `{alert_operator}` | `{pass_operator}` |",
                routing,
            )
        self.assertIn("告警条件 (`alert_operator`)", routing)
        self.assertIn("`pass_operator` 与 `alert_operator`", routing)
        self.assertIn("不得仅因此返回 `insufficient_definition`", routing)
        self.assertNotIn(
            "比较符不一致或字段不一致时返回 `insufficient_definition`",
            routing,
        )

    def test_all_registered_routes_have_canonical_metrics(self):
        rows = _route_rows()

        self.assertEqual(16, len(rows))
        self.assertEqual(
            {
                "下载完成率",
                "下载失败率",
                "下载失败次数比率",
                "下载人为停止率",
                "下载安装完成率",
            },
            {row["canonical_metric"] for row in rows},
        )
        self.assertTrue(
            all("normalized_rule_name" in row for row in rows),
            "registered routes must use exact normalized full rule names",
        )

    def test_registered_root_selects_every_routed_monitor_field(self):
        rows = _route_rows()
        query_spec = REGISTERED_ROOT_PATH.read_text(encoding="utf-8")
        sql = query_spec.split("sql: |", 1)[1].split("\noutput:", 1)[0]

        routed_fields = {
            row[field]
            for row in rows
            for field in (
                "monitor_field",
                "monitor_numerator_field",
                "monitor_denominator_field",
            )
        }
        missing = sorted(
            field for field in routed_fields if not re.search(rf"\b{re.escape(field)}\b", sql)
        )
        self.assertEqual([], missing)

    def test_compiled_metric_definitions_cover_routes_and_drive_direction(self):
        lock = json.loads(
            (ROOT / "contracts" / "metric-definitions.lock.json").read_text(
                encoding="utf-8"
            )
        )
        unsigned = dict(lock)
        bundle_hash = unsigned.pop("bundle_sha256")
        self.assertEqual(bundle_hash, canonical_sha256(unsigned))

        canonical_metrics = {row["canonical_metric"] for row in _route_rows()}
        self.assertEqual(
            canonical_metrics,
            {item["metric"] for item in lock["metrics"]},
        )
        contracts = RepositoryContracts(ROOT)
        for item in lock["metrics"]:
            with self.subTest(metric=item["metric"]):
                self.assertEqual(
                    item["direction"],
                    contracts.metric_result_contract(item["metric"])["direction"],
                )
                self.assertRegex(item["source_definition_sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual({"app", "sandbox"}, set(item["observation_window"]))

    def test_markdown_routes_humans_to_the_machine_contract(self):
        routing = ROUTING_PATH.read_text(encoding="utf-8")
        self.assertIn("contracts/dqc-routes.yaml", routing)
        self.assertNotIn("| 范围 | stage | metric_hint |", routing)

    def test_completion_attribution_assets_match_reviewed_hashes(self):
        expected_hashes = {
            DOWNLOAD_ATTRIBUTION_PATH: (
                "c73b7be9248bb5a1b247a9d9cf3e1415892bdca7cbf9db413d887e76acedb3ca"
            ),
            DOWNLOAD_PRIMARY_TEMPLATE_PATH: (
                "d017ce04f88b89470664d595baf80fccf2db061ff4ffb1f191112d9cf9be6281"
            ),
        }

        for path, expected_hash in expected_hashes.items():
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(expected_hash, actual_hash, path.name)

    def test_each_download_metric_has_dedicated_query_assets(self):
        all_numerators = {assets[2] for assets in QUERY_ASSETS.values()}
        completion_output = DOWNLOAD_ATTRIBUTION_PATH.read_text(encoding="utf-8")
        completion_output = completion_output.split("output:", 1)[1].split(
            "datasets:", 1
        )[0]
        playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")

        for metric, (query_path, template_path, numerator, denominator) in (
            QUERY_ASSETS.items()
        ):
            query_spec = query_path.read_text(encoding="utf-8")
            template = template_path.read_text(encoding="utf-8")
            scoped_query = query_spec.split("sql: |", 1)[1].split(
                "), bucket_aggregates", 1
            )[0]

            self.assertNotIn("${metric}", query_spec, metric)
            self.assertIn(numerator, scoped_query, metric)
            self.assertIn(denominator, scoped_query, metric)
            for other_numerator in all_numerators - {numerator}:
                self.assertNotIn(other_numerator, scoped_query, metric)

            output = query_spec.split("output:", 1)[1].split("datasets:", 1)[0]
            self.assertEqual(completion_output, output, metric)
            self.assertIn(numerator, template, metric)
            self.assertIn(denominator, template, metric)
            self.assertIn(query_path.name, playbook, metric)
            self.assertIn(template_path.name, playbook, metric)

    def test_failed_pv_assets_do_not_apply_entity_rate_upper_bound(self):
        query_path, template_path, _, _ = QUERY_ASSETS["下载失败次数比率"]
        for path in (query_path, template_path):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "game_download_failed_cnt_1d > game_download_cnt_1d", content
            )
            self.assertIn("game_download_failed_cnt_1d < 0", content)


if __name__ == "__main__":
    unittest.main()
