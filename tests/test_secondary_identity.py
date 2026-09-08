from __future__ import annotations

import copy
import unittest

from runtime.contracts import RepositoryContracts
from runtime.host_adapter import ProductionDViewExecutor
from runtime.result_validator import ResultValidationError
from runtime.secondary_result_validator import SecondaryResultValidator
from tests.runtime_result_fixtures import _secondary_bucket_rows
from tests.test_result_validator_runtime import _markdown_result


def secondary_fixture(parent_value="281223", chain="download"):
    contracts = RepositoryContracts()
    columns = contracts.result_schema("secondary_bucket")["columns_by_chain"][chain]
    parent = dict(current_denominator=500, current_numerator=300,
                  baseline_denominator=3500, baseline_numerator=2800)
    root = dict(current_denominator=1000, current_numerator=700,
                baseline_denominator=7000, baseline_numerator=5600)
    state = {
        "analysis_date": "2026-08-22", "game_type": "app",
        "steps": [{"id": "game_id", "candidates": [
            {"value": parent_value, "private_counts": parent}],
            **{f"root_{key}": value for key, value in root.items()}}],
    }
    raw = {"columns": list(columns), "rows": _secondary_bucket_rows(
        columns, state, {"parent_value": parent_value})}
    kwargs = dict(
        binding=contracts.secondary_binding(chain=chain,
            metric="下载完成率" if chain == "download" else "下载安装完成率",
            parent_dimension="game_id", parent_value=parent_value,
            child_dimension="device_brand"),
        chain=chain, metric="下载完成率" if chain == "download" else "下载安装完成率",
        analysis_date="2026-08-22", game_type="app", parent_value=parent_value,
        parent_counts=parent, root_counts=root,
    )
    return contracts, raw, kwargs


class SecondaryIdentityTest(unittest.TestCase):
    def test_t01_complete_markdown_adapter_to_validator(self):
        for value in ("281223", "00123", "999999999999999999999999999999", "1e12"):
            with self.subTest(value=value):
                contracts, raw, kwargs = secondary_fixture(value)
                response = ProductionDViewExecutor(lambda **_: {
                    "result": _markdown_result(raw, "secondary-identity")
                }).execute_read_only("SELECT fixture")
                parent_index = response.raw_result["columns"].index("parent_value")
                self.assertEqual(value, response.raw_result["rows"][0][parent_index])
                outcome = SecondaryResultValidator(contracts).validate(
                    raw_result=response.raw_result, **kwargs)
                self.assertEqual("succeeded", outcome.status)

    def test_t02_t03_identity_and_other_column_types(self):
        for token in ("null", "NULL", "None", "<null>"):
            raw = {"columns": ["parent_value", "dimension_value", "dimension_label",
                               "game_type", "analysis_date", "count", "ratio", "scope"],
                   "rows": [[token, token, token, token, "2026-08-22", 123, 0.25, "root"]]}
            response = ProductionDViewExecutor(lambda **_: {
                "result": _markdown_result(raw, "identity-types")
            }).execute_read_only("SELECT fixture")
            self.assertEqual([token, token, token, None, "2026-08-22", 123, 0.25, "root"],
                             response.raw_result["rows"][0])

    def test_t04_structured_transport_does_not_coerce_identity(self):
        for column in ("parent_value", "dimension_value", "dimension_label"):
            for value in ("00123", 123, 1.25, None):
                with self.subTest(column=column, value=value):
                    contracts, raw, kwargs = secondary_fixture()
                    if column == "parent_value" and isinstance(value, str):
                        contracts, raw, kwargs = secondary_fixture(value)
                    else:
                        raw["rows"][0][column] = value
                    response = ProductionDViewExecutor(lambda **_: {
                        "query_id": "structured-identity", **copy.deepcopy(raw)
                    }).execute_read_only("SELECT fixture")
                    parsed = response.raw_result["rows"][0][raw["columns"].index(column)]
                    self.assertEqual(value, parsed)
                    self.assertIs(type(value), type(parsed))
                    if isinstance(value, str):
                        SecondaryResultValidator(contracts).validate(
                            raw_result=response.raw_result, **kwargs)
                    else:
                        with self.assertRaises(ResultValidationError) as error:
                            SecondaryResultValidator(contracts).validate(
                                raw_result=response.raw_result, **kwargs)
                        self.assertEqual("schema_invalid", error.exception.code)

    def test_t03_literal_null_children_pass_the_complete_secondary_schema(self):
        for value in ("null", "NULL", "None", "<null>"):
            contracts, raw, kwargs = secondary_fixture()
            raw["rows"][0].update(dimension_value=value, dimension_label=value)
            parsed = ProductionDViewExecutor(lambda **_: {
                "result": _markdown_result(raw, "literal-null-child")
            }).execute_read_only("SELECT fixture").raw_result
            outcome = SecondaryResultValidator(contracts).validate(raw_result=parsed, **kwargs)
            self.assertEqual(value, outcome.candidates[0]["value"])
            self.assertEqual(value, outcome.candidates[0]["label"])
