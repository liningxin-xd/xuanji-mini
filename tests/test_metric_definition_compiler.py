from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.compile_metric_definitions import (
    DOMAIN,
    METRIC_DIRECTIONS,
    compile_bundle,
    render_bundle,
)


class MetricDefinitionCompilerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.skill_root = Path(self.temp_dir.name) / "skill"
        self.knowledge_base = self.skill_root / "knowledge-base"
        self.metric_root = self.knowledge_base / "metrics" / DOMAIN
        self.metric_root.mkdir(parents=True)
        manifest = {
            "knowledge_base": {"version": "fixture-v1"},
            "domains": [
                {
                    "name": DOMAIN,
                    "metric_index": f"metrics/{DOMAIN}/_index.yaml",
                }
            ],
        }
        (self.knowledge_base / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        index = []
        for metric in METRIC_DIRECTIONS:
            filename = f"{metric}.yaml"
            index.append({"file": filename, "aliases": [metric]})
            app_window = "未来3天" if metric == "下载安装完成率" else "最近一天"
            definition = {
                "metric": metric,
                "standard_name": [
                    f"{app_window}_APK{metric}",
                    f"最近一天_沙盒{metric}",
                ],
                "业务口径": "fixture business definition",
                "技术口径": "fixture technical definition",
                "sql": [{"engine": "MaxCompute", "query": "SELECT 1"}],
            }
            (self.metric_root / filename).write_text(
                yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        (self.metric_root / "_index.yaml").write_text(
            yaml.safe_dump(index, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def test_compiler_is_deterministic_and_extracts_scope_windows(self):
        first = compile_bundle(self.skill_root)
        second = compile_bundle(self.skill_root)
        self.assertEqual(render_bundle(first), render_bundle(second))
        self.assertEqual(5, len(first["metrics"]))
        install = next(
            item for item in first["metrics"] if item["metric"] == "下载安装完成率"
        )
        self.assertEqual(
            {"app": "未来3天", "sandbox": "最近一天"},
            install["observation_window"],
        )

    def test_source_definition_drift_changes_the_bundle(self):
        before = compile_bundle(self.skill_root)
        target = self.metric_root / "下载完成率.yaml"
        definition = yaml.safe_load(target.read_text(encoding="utf-8"))
        definition["业务口径"] = "changed definition"
        target.write_text(
            yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        after = compile_bundle(self.skill_root)
        self.assertNotEqual(before["bundle_sha256"], after["bundle_sha256"])
        self.assertNotEqual(
            json.dumps(before, sort_keys=True),
            json.dumps(after, sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
