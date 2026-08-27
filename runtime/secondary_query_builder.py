from __future__ import annotations

import re
from datetime import date
from typing import Any

from .contracts import RepositoryContracts, sha256_bytes, sha256_text
from .models import BuiltQuery, QueryBinding
from .query_builder import QueryBuildError, QueryBuilder


SECONDARY_PLACEHOLDERS = (
    "__PARENT_SOURCE_FIELD__",
    "__PARENT_QUALITY_SOURCE_EXPR__",
    "__CHILD_SOURCE_FIELD__",
    "__CHILD_QUALITY_SOURCE_EXPR__",
    "__PARENT_VALUE_EXPR__",
    "__CHILD_VALUE_EXPR__",
)
DOWNLOAD_METRIC_PLACEHOLDERS = (
    "__DENOMINATOR_SOURCE_FIELD__",
    "__NUMERATOR_SOURCE_FIELD__",
    "__INVALID_METRIC_PREDICATE__",
)


class SecondaryQueryBuilder:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts
        self.query_builder = QueryBuilder(contracts)

    def build(
        self,
        *,
        chain: str,
        metric: str,
        business_date: str,
        game_type: str,
        parent_dimension: str,
        parent_value: str,
        child_dimension: str,
    ) -> BuiltQuery:
        self._validate_inputs(business_date, game_type, parent_value)
        binding = self.contracts.secondary_binding(
            chain=chain,
            metric=metric,
            parent_dimension=parent_dimension,
            parent_value=parent_value,
            child_dimension=child_dimension,
        )
        asset_path = self.contracts.root / binding.asset_path
        if sha256_bytes(asset_path.read_bytes()) != binding.asset_sha256:
            raise QueryBuildError(
                f"query asset changed after plan compilation: {binding.asset_path}"
            )
        sql = self._select_sql_block(asset_path.read_text(encoding="utf-8"), binding)
        config = binding.dimension_config or {}
        replacements = {
            "__PARENT_SOURCE_FIELD__": config["parent_source_field"],
            "__PARENT_QUALITY_SOURCE_EXPR__": config[
                "parent_quality_source_expression"
            ],
            "__CHILD_SOURCE_FIELD__": config["child_source_field"],
            "__CHILD_QUALITY_SOURCE_EXPR__": config[
                "child_quality_source_expression"
            ],
            "__PARENT_VALUE_EXPR__": self._scoped_expression(
                config["parent_value_expression"], "parent"
            ),
            "__CHILD_VALUE_EXPR__": self._scoped_expression(
                config["child_value_expression"], "child"
            ),
        }
        projection = config.get("metric_projection")
        if chain == "download":
            if not isinstance(projection, dict):
                raise QueryBuildError("download secondary metric projection is missing")
            replacements.update(
                {
                    "__DENOMINATOR_SOURCE_FIELD__": projection[
                        "denominator_source_field"
                    ],
                    "__NUMERATOR_SOURCE_FIELD__": projection[
                        "numerator_source_field"
                    ],
                    "__INVALID_METRIC_PREDICATE__": projection[
                        "invalid_metric_predicate"
                    ],
                }
            )
        for placeholder in SECONDARY_PLACEHOLDERS:
            if sql.count(placeholder) != 1:
                raise QueryBuildError(
                    f"secondary placeholder count changed: {placeholder}"
                )
            sql = sql.replace(placeholder, replacements[placeholder])
        for placeholder in DOWNLOAD_METRIC_PLACEHOLDERS:
            count = sql.count(placeholder)
            if chain == "download" and count != 1:
                raise QueryBuildError(
                    f"secondary metric placeholder count changed: {placeholder}"
                )
            if chain == "download":
                sql = sql.replace(placeholder, replacements[placeholder])
            elif count:
                raise QueryBuildError(
                    f"install secondary SQL contains download placeholder: {placeholder}"
                )
        literals = {
            "business_date": self._quote(business_date),
            "game_type": self._quote(game_type),
            "parent_value": self._quote(parent_value),
        }
        for name, literal in literals.items():
            sql = re.sub(rf"\$\{{{name}\}}", literal, sql)
        if re.search(r"\$\{|__[A-Z]", sql):
            raise QueryBuildError("secondary SQL contains unresolved placeholders")
        self.query_builder.validate_sql(
            sql,
            binding,
            {"business_date": business_date, "game_type": game_type},
        )
        return BuiltQuery(
            sql=sql,
            sha256=sha256_text(sql),
            parameters={
                "business_date": business_date,
                "game_type": game_type,
                "parent_dimension": parent_dimension,
                "parent_value": parent_value,
                "child_dimension": child_dimension,
            },
            binding=binding,
        )

    def _select_sql_block(self, text: str, binding: QueryBinding) -> str:
        blocks = re.findall(r"```sql\s*\n(.*?)\n```", text, flags=re.DOTALL)
        source = binding.data_sources[0]
        matches = [
            block
            for block in blocks
            if re.match(r"(?is)^\s*WITH\b", block)
            and source in block
            and all(token in block for token in SECONDARY_PLACEHOLDERS)
        ]
        if len(matches) != 1:
            raise QueryBuildError(
                "secondary template must contain one SQL block for the chain"
            )
        return matches[0]

    @staticmethod
    def _scoped_expression(expression: str, scope: str) -> str:
        return expression.replace("dimension_", f"{scope}_")

    @staticmethod
    def _quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _validate_inputs(
        business_date: Any, game_type: Any, parent_value: Any
    ) -> None:
        try:
            valid_date = (
                isinstance(business_date, str)
                and date.fromisoformat(business_date).isoformat() == business_date
            )
        except ValueError:
            valid_date = False
        if not valid_date:
            raise QueryBuildError("secondary business_date must use YYYY-MM-DD")
        if game_type not in {"app", "sandbox"}:
            raise QueryBuildError("secondary game_type must be app or sandbox")
        if not isinstance(parent_value, str) or not parent_value.strip():
            raise QueryBuildError("secondary parent_value must be non-empty")
