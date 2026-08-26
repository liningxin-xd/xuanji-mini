import unittest
from pathlib import Path

from runtime.contracts import RepositoryContracts
from runtime.query_builder import QueryBuildError, QueryBuilder


ROOT = Path(__file__).resolve().parents[1]


class QueryBuilderRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contracts = RepositoryContracts(ROOT)
        cls.builder = QueryBuilder(cls.contracts)

    def _build(self, chain, game_type, metric, step_id):
        plan = self.contracts.select_plan(chain, game_type, metric)
        binding = self.contracts.binding_for(plan, step_id, metric, game_type)
        return self.builder.build(
            binding,
            {"business_date": "2026-08-22", "game_type": game_type},
        )

    def test_each_download_metric_uses_its_registered_game_query(self):
        expected_tokens = {
            "下载完成率": ("download-game-attribution.yaml", "is_download_complete"),
            "下载失败率": (
                "download-failed-rate-game-attribution.yaml",
                "is_explicit_failed",
            ),
            "下载失败次数比率": (
                "download-failed-pv-rate-game-attribution.yaml",
                "game_download_failed_cnt_1d",
            ),
            "下载人为停止率": (
                "download-stop-rate-game-attribution.yaml",
                "is_human_stop",
            ),
        }
        for metric, (filename, token) in expected_tokens.items():
            with self.subTest(metric=metric):
                built = self._build("download", "app", metric, "game_id")
                self.assertTrue(built.binding.asset_path.endswith(filename))
                self.assertIn(token, built.sql)
                self.assertIn("game_type = 'app'", built.sql)
                self.assertNotIn("${", built.sql)
                self.assertNotIn(" LIMIT ", built.sql.upper())

    def test_each_download_metric_uses_its_independent_primary_template(self):
        paths = set()
        for metric in self.contracts.plans["download"].allowed_metrics:
            built = self._build("download", "sandbox", metric, "device_brand")
            paths.add(built.binding.asset_path)
            self.assertIn("device_brand AS dimension_source", built.sql)
            self.assertIn("game_type = 'sandbox'", built.sql)
            for other_dimension in (
                "channel_group",
                "app_major_version",
                "os_major_version",
                "apk_size_tier",
                "is_reserve_auto_download",
            ):
                self.assertNotIn(other_dimension, built.sql)
        self.assertEqual(4, len(paths))

    def test_reserve_dimension_uses_the_registered_binary_normalizer(self):
        built = self._build(
            "download", "app", "下载完成率", "is_reserve_auto_download"
        )
        self.assertIn("THEN 'reserve_auto_download'", built.sql)
        self.assertIn("THEN 'other_download'", built.sql)
        self.assertIn("ELSE CONCAT('invalid_'", built.sql)

    def test_install_primary_keeps_official_projection_and_anchor(self):
        built = self._build(
            "install", "app", "下载安装完成率", "storage_headroom_tier"
        )
        self.assertIn("storage_headroom_tier AS dimension_source", built.sql)
        self.assertIn("official_download_complete", built.sql)
        self.assertIn("official_install_complete", built.sql)
        self.assertIn("is_metric_anchor = 1", built.sql)
        self.assertNotIn("install_event_app_major_version", built.sql)

    def test_parameter_whitelist_rejects_missing_unknown_and_invalid_values(self):
        plan = self.contracts.select_plan("download", "app", "下载完成率")
        binding = self.contracts.binding_for(plan, "game_id", "下载完成率", "app")
        invalid_parameters = (
            {"business_date": "2026-08-22"},
            {
                "business_date": "2026-08-22",
                "game_type": "app",
                "unknown": "x",
            },
            {"business_date": "20260822", "game_type": "app"},
            {"business_date": "2026-08-22", "game_type": "ios"},
        )
        for parameters in invalid_parameters:
            with self.subTest(parameters=parameters), self.assertRaises(QueryBuildError):
                self.builder.build(binding, parameters)

    def test_static_gate_rejects_limit_cartesian_and_scope_removal(self):
        built = self._build("download", "app", "下载完成率", "device_brand")
        mutations = (
            built.sql + "\nLIMIT 10",
            built.sql.replace("WITH scoped_rows", "WITH x AS (SELECT 1), scoped_rows").replace(
                "FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d",
                "FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d CROSS JOIN x",
                1,
            ),
            built.sql.replace("AND platform = 'ANDROID'", ""),
            built.sql.replace("AND game_type = 'app'", ""),
            built.sql.replace(
                "FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d",
                "FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d src "
                "JOIN tap_dw.unregistered_dimension_snapshot extra "
                "ON src.dt = extra.dt",
                1,
            ),
            built.sql.replace(
                "FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d",
                "FROM tap_dw.ads_report_store_platform_device_game_download_chain_attribution_1d src "
                "JOIN `tap_dw.unregistered_dimension_snapshot` extra "
                "ON src.dt = extra.dt",
                1,
            ),
        )
        for sql in mutations:
            with self.subTest(sql=sql[-80:]), self.assertRaises(QueryBuildError):
                self.builder.validate_sql(sql, built.binding, built.parameters)

    def test_rendered_sql_hash_is_stable(self):
        first = self._build("download", "app", "下载完成率", "apk_size_tier")
        second = self._build("download", "app", "下载完成率", "apk_size_tier")
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.sql, second.sql)


if __name__ == "__main__":
    unittest.main()
