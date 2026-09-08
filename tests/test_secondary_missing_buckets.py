from __future__ import annotations

import copy
import sqlite3
import unittest
from datetime import date, timedelta
from runtime.analysis_v5 import DIMENSION_DISPLAY_NAMES
from runtime.contracts import ContractError, RepositoryContracts
from runtime.result_validator import ResultValidationError
from runtime.secondary_query_builder import SecondaryQueryBuilder
from runtime.secondary_result_validator import SecondaryResultValidator
from tests.test_secondary_identity import secondary_fixture


NULL = "__dimension_null__"
BLANK = "__dimension_blank__"
COLLISION = "__dimension_reserved_collision__"
METRICS = {"download": "下载完成率", "install": "下载安装完成率"}


class SecondaryMissingBucketsTest(unittest.TestCase):
    def setUp(self):
        self.contracts = RepositoryContracts()

    def query(self, chain="download", dimension="device_brand", metric=None):
        return SecondaryQueryBuilder(self.contracts).build(
            chain=chain, metric=metric or METRICS[chain], business_date="2026-08-22",
            game_type="app", parent_dimension="game_id", parent_value="281223",
            child_dimension=dimension)

    def execute(self, groups, *, chain="download", metric=None, dimension="device_brand"):
        """Execute the production SQL; adapt only MaxCompute date/cast syntax."""
        query = self.query(chain, dimension=dimension, metric=metric)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.create_function("TO_DATE", 1, lambda value: value)
        db.create_function("TO_CHAR", 2, lambda value, _: value)
        db.create_function("DATEADD", 3, lambda value, days, _: (
            date.fromisoformat(value) + timedelta(days=days)).isoformat())
        db.execute("ATTACH DATABASE ':memory:' AS tap_dw")
        table = query.binding.data_sources[0]
        db.execute(f"CREATE TABLE {table} (dt TEXT, device_id TEXT, game_id TEXT, "
                   "platform TEXT, game_type TEXT, device_brand TEXT, device_dimension_matched INTEGER, "
                   "download_sample_flag INTEGER, is_download_complete INTEGER, "
                   "is_explicit_failed INTEGER, game_download_cnt_1d INTEGER, "
                   "game_download_failed_cnt_1d INTEGER, is_human_stop INTEGER, "
                   "official_download_complete INTEGER, official_install_complete INTEGER, "
                   "official_observation_days INTEGER, is_metric_anchor INTEGER)")
        source_rows = []
        for group_index, (value, matched, current, baseline, parent) in enumerate(groups):
            for day in range(8):
                count = current if day == 0 else baseline
                for row_index in range(count):
                    success = int(row_index < count * (0.6 if day == 0 and parent == "281223" else 0.8))
                    source_rows.append(((date(2026, 8, 22) - timedelta(days=day)).isoformat(),
                        f"{group_index}-{row_index}", parent, "ANDROID", "app", value, matched,
                        1, success, success, 1, success, success, 1, success, 3, 1))
        db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(17))})", source_rows)
        sql = query.sql.replace(" AS STRING)", " AS TEXT)")
        cursor = db.execute(sql)
        raw = {"columns": [column[0] for column in cursor.description],
               "rows": [dict(row) for row in cursor.fetchall()]}
        self.assertLessEqual(len(raw["rows"]), 205)
        self.assertEqual(1, sum(row["bucket_kind"] == "outside_parent" for row in raw["rows"]))
        parent_counts = {}
        root_counts = {}
        for name in ("current_denominator", "current_numerator", "baseline_denominator", "baseline_numerator"):
            root_counts[name] = sum(row[name] for row in raw["rows"])
            parent_counts[name] = sum(row[name] for row in raw["rows"] if row["bucket_kind"] != "outside_parent")
            self.assertTrue(all(row[f"overall_{name}"] == root_counts[name] for row in raw["rows"]))
        kwargs = dict(binding=query.binding, chain=chain, metric=metric or METRICS[chain],
                      analysis_date="2026-08-22", game_type="app", parent_value="281223",
                      parent_counts=parent_counts, root_counts=root_counts)
        return raw, kwargs

    def validate(self, raw, kwargs):
        return SecondaryResultValidator(self.contracts).validate(raw_result=raw, **kwargs)

    def test_t05_t08_sql_separates_missing_and_literal_values(self):
        for chain in METRICS:
            with self.subTest(chain=chain):
                groups = [(None, 1, 200, 200, "281223"), ("", 1, 100, 100, "281223"),
                          ("   ", 1, 100, 100, "281223"), ("Brand A", 1, 200, 200, "281223"),
                          ("null", 1, 200, 200, "281223"), (None, 1, 200, 200, "outside")]
                raw, kwargs = self.execute(groups, chain=chain)
                rows = {row["dimension_value"]: row for row in raw["rows"]}
                self.assertEqual({NULL, BLANK, "Brand A", "null", "outside_parent"}, set(rows))
                for value in (NULL, BLANK):
                    self.assertEqual(200, rows[value]["current_denominator"])
                    self.assertEqual(1400, rows[value]["baseline_denominator"])
                    self.assertEqual("child", rows[value]["bucket_kind"])
                    self.assertEqual(value, rows[value]["dimension_label"])
                outcome = self.validate(raw, kwargs)
                candidates = {item["value"]: item for item in outcome.candidates}
                self.assertEqual("设备品牌不适用或未包含", candidates[NULL]["label"])
                self.assertEqual("设备品牌为空白", candidates[BLANK]["label"])
                self.assertLessEqual(outcome.candidate_count, 3)

    def test_t06_unmatched_precedes_missing_and_collision(self):
        for chain in METRICS:
            for value in (None, "", "   ", NULL, BLANK, COLLISION):
                with self.subTest(chain=chain, value=value):
                    raw, kwargs = self.execute([(value, 0, 200, 200, "281223"),
                                               ("outside", 1, 800, 800, "outside")], chain=chain)
                    self.assertEqual({"unmatched", "outside_parent"}, {row["dimension_value"] for row in raw["rows"]})
                    self.assertEqual(0, self.validate(raw, kwargs).candidate_count)

    def test_t07_collision_survives_low_sample_and_share(self):
        for chain in METRICS:
            for value in (NULL, BLANK, COLLISION):
                with self.subTest(chain=chain, value=value):
                    raw, kwargs = self.execute([(value, 1, 1, 1, "281223"),
                        (None, 1, 200, 200, "281223"), ("outside", 1, 800, 800, "outside")], chain=chain)
                    collision = next(row for row in raw["rows"] if row["dimension_value"] == COLLISION)
                    self.assertEqual("quality", collision["bucket_kind"])
                    self.assertEqual(1, collision["collapsed_source_bucket_count"])
                    with self.assertRaisesRegex(ResultValidationError, "reserved_identity_collision") as error:
                        self.validate(raw, kwargs)
                    self.assertEqual("schema_invalid", error.exception.code)

    def test_t08_policy_is_explicit_complete_and_labels_are_frozen(self):
        for chain, dimensions in self.contracts.registry["secondary"]["missing_child_bucket_policy"].items():
            for dimension, labels in dimensions.items():
                self.assertEqual(DIMENSION_DISPLAY_NAMES[dimension] + "不适用或未包含", labels["null_label"])
                self.assertEqual(DIMENSION_DISPLAY_NAMES[dimension] + "为空白", labels["blank_label"])
                for value, key in ((NULL, "null_label"), (BLANK, "blank_label")):
                    contracts, raw, kwargs = secondary_fixture(chain=chain)
                    kwargs["binding"] = self.query(chain, dimension).binding
                    raw["rows"][0].update(dimension_value=value, dimension_label=value)
                    outcome = self.validate(raw, kwargs)
                    self.assertEqual(labels[key], outcome.candidates[0]["label"])
                    self.assertEqual(value, raw["rows"][0]["dimension_label"])
                    raw["rows"][0]["dimension_label"] = "forged label"
                    with self.assertRaises(ResultValidationError):
                        self.validate(raw, kwargs)
        policy = self.contracts.missing_child_bucket_policy("download", "device_brand")
        policy["null_label"] = "mutated"
        self.assertNotEqual(policy, self.contracts.missing_child_bucket_policy("download", "device_brand"))

    def test_t09_low_sample_share_and_contribution_do_not_publish_labels(self):
        for chain in METRICS:
            for count, outside in ((1, 999), (100, 20000)):
                raw, kwargs = self.execute([(None, 1, count, count, "281223"),
                    (" ", 1, count, count, "281223"), ("outside", 1, outside, outside, "outside")], chain=chain)
                self.assertEqual({"__other_below_threshold__", "outside_parent"},
                                 {row["dimension_value"] for row in raw["rows"]})
                self.assertEqual(0, self.validate(raw, kwargs).candidate_count)
            raw, kwargs = self.execute([(None, 1, 200, 200, "281223"),
                                       (" ", 1, 200, 200, "281223"), ("outside", 1, 600, 600, "outside")], chain=chain)
            for row in raw["rows"]:
                row["current_numerator"] = row["current_denominator"] * 4 // 5
                row["overall_current_numerator"] = 800
            kwargs["root_counts"]["current_numerator"] = 800
            kwargs["parent_counts"]["current_numerator"] = 320
            self.assertEqual(0, self.validate(raw, kwargs).candidate_count)

    def test_t11_future_relation_uses_standard_without_policy(self):
        registry = self.contracts.registry
        registry["dimensions"]["download"]["future_child"] = copy.deepcopy(registry["dimensions"]["download"]["device_brand"])
        self.contracts._secondary_relations["relations"]["download"]["game_id"].append("future_child")
        self.assertIsNone(self.contracts.missing_child_bucket_policy("download", "future_child"))
        query = self.query(dimension="future_child")
        self.assertIn("THEN '__none__'", query.binding.dimension_config["child_value_expression"])
        self.assertNotIn(NULL, query.binding.dimension_config["child_value_expression"])
        sql_raw, sql_kwargs = self.execute([
            (None, 1, 200, 200, "281223"), (" ", 1, 200, 200, "281223"),
            ("outside", 1, 600, 600, "outside"),
        ], dimension="future_child")
        self.assertEqual({"__quality__", "outside_parent"},
                         {row["dimension_value"] for row in sql_raw["rows"]})
        self.assertEqual(0, self.validate(sql_raw, sql_kwargs).candidate_count)
        _, raw, kwargs = secondary_fixture()
        kwargs["binding"] = query.binding
        raw["rows"][0].update(dimension_value="__none__", dimension_label="__none__", bucket_kind="quality")
        self.assertEqual(0, self.validate(raw, kwargs).candidate_count)
        raw["rows"][0].update(dimension_value=NULL, dimension_label=NULL, bucket_kind="child")
        with self.assertRaises(ResultValidationError):
            self.validate(raw, kwargs)

    def test_policy_and_normalizer_contract_reject_drift(self):
        mutations = [
            lambda r: r["secondary"]["missing_child_bucket_policy"]["download"].pop("device_brand"),
            lambda r: r["secondary"]["missing_child_bucket_policy"]["download"].update(future_child={"null_label": "a", "blank_label": "b"}),
            lambda r: r["secondary"]["missing_child_bucket_policy"]["download"]["device_brand"].update(null_label=""),
            lambda r: r["secondary"]["missing_child_bucket_policy"]["download"]["device_brand"].update(null_label="same", blank_label="same"),
            lambda r: r["dimension_normalizers"]["secondary_missing_child"].pop("label_expression"),
            lambda r: r["dimensions"]["download"]["device_brand"].update(normalizer="secondary_missing_child"),
        ]
        for mutate in mutations:
            contracts = RepositoryContracts()
            mutate(contracts.registry)
            with self.assertRaises(ContractError):
                contracts._validate_registry()
                contracts._validate_missing_child_bucket_policy()
        self.contracts._secondary_relations["relations"]["download"]["game_id"].remove("device_brand")
        with self.assertRaisesRegex(ContractError, "subset"):
            self.contracts._validate_missing_child_bucket_policy()

    def test_t12_template_bound_and_full_bucket_closure(self):
        for collision in (False, True):
            for chain in METRICS:
                # Disjoint current/baseline 1% sets. Missing values occupy ordinary child slots.
                values = [None, " "] + [f"current-{i}" for i in range(97)]
                groups = [(value, 1, 100, 0, "281223") for value in values]
                groups += [(f"baseline-{i}", 1, 0, 100, "281223") for i in range(99)]
                groups += [("quality", 0, 1, 1, "281223"), ("invalid_x", 1, 1, 1, "281223"),
                           ("small", 1, 1, 1, "281223"), (COLLISION, 1, 1, 1, "outside")]
                if collision:
                    groups.append((BLANK, 1, 1, 1, "281223"))
                raw, kwargs = self.execute(groups, chain=chain)
                self.assertEqual(203 if collision else 202, len(raw["rows"]))
                self.assertEqual(198, sum(row["bucket_kind"] == "child" for row in raw["rows"]))
                self.assertEqual(sum(row["collapsed_source_bucket_count"] for row in raw["rows"]), raw["rows"][0]["source_bucket_count"])
                if collision:
                    with self.assertRaisesRegex(ResultValidationError, "reserved_identity_collision"):
                        self.validate(raw, kwargs)
                else:
                    self.validate(raw, kwargs)
                    for change in ("parent", "root", "row", "source", "outside_missing", "outside_duplicate"):
                        broken, changed = copy.deepcopy(raw), copy.deepcopy(kwargs)
                        if change == "parent":
                            changed["parent_counts"]["current_denominator"] += 1
                        elif change == "root":
                            changed["root_counts"]["current_denominator"] += 1
                        elif change == "row":
                            broken["rows"][0]["current_row_count"] += 1
                        elif change == "source":
                            broken["rows"][0]["collapsed_source_bucket_count"] += 1
                        elif change == "outside_missing":
                            broken["rows"] = [r for r in broken["rows"] if r["bucket_kind"] != "outside_parent"]
                        else:
                            broken["rows"].append(copy.deepcopy(next(r for r in broken["rows"] if r["bucket_kind"] == "outside_parent")))
                        with self.assertRaises(ResultValidationError):
                            self.validate(broken, changed)
        # Existing threshold implies <=100 current + <=100 baseline + 3 quality + residual + outside.
        self.assertEqual(205, 2 * int(1 / self.contracts.result_defaults["minimum_share"]) + 3 + 1 + 1)

    def test_all_download_metrics_share_the_secondary_normalizer(self):
        for metric in self.contracts.registry["download_metrics"]:
            raw, kwargs = self.execute([(None, 1, 200, 200, "281223"),
                (" ", 1, 200, 200, "281223"), ("outside", 1, 600, 600, "outside")], metric=metric)
            self.assertEqual({NULL, BLANK, "outside_parent"}, {r["dimension_value"] for r in raw["rows"]})
            self.validate(raw, kwargs)
