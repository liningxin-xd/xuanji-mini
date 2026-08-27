import hashlib
import os
import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in minimal environments
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
ROUTING_PATH = ROOT / "references" / "dqc-alert-routing.md"
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
    lines = ROUTING_PATH.read_text(encoding="utf-8").splitlines()
    header_index = next(
        index for index, line in enumerate(lines) if line.startswith("| 范围 | stage |")
    )
    headers = [cell.strip() for cell in lines[header_index].strip("|").split("|")]
    rows = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        values = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        rows.append(dict(zip(headers, values, strict=True)))
    return rows


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
        self.assertFalse(
            [row for row in rows if "缺失" in row["知识库指标"]],
            "registered routes must not retain stale missing-definition sentinels",
        )

        expected = {
            "apk下载失败率": "下载失败率",
            "apk下载失败次数比率": "下载失败次数比率",
            "apk人为停止率": "下载人为停止率",
            "沙盒下载失败率": "下载失败率",
            "沙盒下载失败次数比率": "下载失败次数比率",
            "沙盒人为停止率": "下载人为停止率",
        }
        actual = {row["metric_hint"]: row["知识库指标"] for row in rows}
        for metric_hint, canonical_metric in expected.items():
            self.assertEqual(canonical_metric, actual[metric_hint])

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

    def test_canonical_metrics_exist_in_data_analysis_knowledge_base(self):
        skill_root_value = os.environ.get("TAPTAP_DATA_ANALYSIS_SKILL_ROOT")
        if not skill_root_value:
            self.skipTest("TAPTAP_DATA_ANALYSIS_SKILL_ROOT is not configured")
        if yaml is None:
            self.skipTest("PyYAML is not installed")

        skill_root = Path(skill_root_value)
        manifest = yaml.safe_load(
            (skill_root / "knowledge-base" / "manifest.yaml").read_text(encoding="utf-8")
        )
        store_domain = next(
            domain for domain in manifest["domains"] if domain["name"] == "商店（移动端）"
        )
        metric_index_path = skill_root / "knowledge-base" / store_domain["metric_index"]
        metric_index = yaml.safe_load(metric_index_path.read_text(encoding="utf-8"))

        canonical_metrics = {row["知识库指标"] for row in _route_rows()}
        for canonical_metric in canonical_metrics:
            matches = [
                entry for entry in metric_index if canonical_metric in entry["aliases"]
            ]
            self.assertEqual(1, len(matches), canonical_metric)

            metric_definition = yaml.safe_load(
                (metric_index_path.parent / matches[0]["file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(canonical_metric, metric_definition["metric"])
            for required_field in ("业务口径", "技术口径", "sql"):
                self.assertTrue(metric_definition[required_field], canonical_metric)

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
