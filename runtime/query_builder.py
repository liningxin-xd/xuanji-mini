from __future__ import annotations

import re
import difflib
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .contracts import ContractError, RepositoryContracts, sha256_bytes, sha256_text
from .models import BuiltQuery, QueryBinding


PARAMETER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DIMENSION_PLACEHOLDER_COUNTS = {
    "__DIMENSION_SOURCE_FIELD__": 1,
    "__DIMENSION_QUALITY_SOURCE_EXPR__": 1,
    "__DIMENSION_VALUE_EXPR__": 2,
    "__DIMENSION_LABEL_EXPR__": 1,
}


class QueryBuildError(ContractError):
    pass


class QueryBuilder:
    def __init__(self, contracts: RepositoryContracts):
        self.contracts = contracts
        self.root = contracts.root

    def build(
        self, binding: QueryBinding, parameters: dict[str, Any]
    ) -> BuiltQuery:
        asset_path = self.root / binding.asset_path
        actual_asset_hash = sha256_bytes(asset_path.read_bytes())
        if actual_asset_hash != binding.asset_sha256:
            raise QueryBuildError(
                f"query asset changed after plan compilation: {binding.asset_path}"
            )

        if binding.asset_kind == "query_spec":
            sql, parameter_specs = self._load_query_spec(asset_path)
        elif binding.asset_kind == "markdown_template":
            sql = self._load_markdown_template(asset_path)
            parameter_specs = {
                "business_date": {"type": "date"},
                "game_type": {
                    "type": "enum",
                    "allowed_values": ["app", "sandbox"],
                },
            }
            sql = self._replace_dimension_placeholders(sql, binding)
        else:
            raise QueryBuildError(f"unsupported asset kind: {binding.asset_kind}")

        rendered_parameters = self._validate_parameters(parameter_specs, parameters)
        rendered_sql = self._render_parameters(sql, parameter_specs, rendered_parameters)
        self.validate_sql(rendered_sql, binding, rendered_parameters)
        return BuiltQuery(
            sql=rendered_sql,
            sha256=sha256_text(rendered_sql),
            parameters=dict(rendered_parameters),
            binding=binding,
        )

    def validate_sql(
        self,
        sql: str,
        binding: QueryBinding,
        parameters: dict[str, Any],
    ) -> None:
        if not isinstance(sql, str) or not sql.strip():
            raise QueryBuildError("rendered SQL must be a non-empty string")
        normalized = self._sql_without_comments(sql).strip()
        if not re.match(r"(?is)^(WITH\b|SELECT\b)", normalized):
            raise QueryBuildError("only read-only SELECT/WITH SQL is allowed")
        statement_sql = self._sql_without_literals(normalized)
        forbidden_statements = re.search(
            r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|"
            r"GRANT|REVOKE|CALL)\b",
            statement_sql,
        )
        if forbidden_statements:
            raise QueryBuildError(
                f"forbidden SQL statement: {forbidden_statements.group(1)}"
            )
        if re.search(r"(?i)\bCROSS\s+JOIN\b", normalized):
            raise QueryBuildError("CROSS JOIN is forbidden")
        if re.search(r"(?is)\bJOIN\b.{0,400}?\bON\s+1\s*=\s*1\b", normalized):
            raise QueryBuildError("JOIN ... ON 1 = 1 is forbidden")
        if re.search(
            r"(?is)\bFROM\s+[A-Za-z0-9_.]+(?:\s+(?:AS\s+)?[A-Za-z0-9_]+)?"
            r"\s*,\s*[A-Za-z0-9_.]+",
            normalized,
        ):
            raise QueryBuildError("comma cartesian products are forbidden")
        if re.search(r"(?i)\bLIMIT\b", normalized):
            raise QueryBuildError("LIMIT is forbidden")
        if PARAMETER_PATTERN.search(normalized) or re.search(
            r"__[A-Z][A-Z0-9_]*__", normalized
        ):
            raise QueryBuildError("rendered SQL contains unresolved placeholders")
        business_date = parameters.get("business_date")
        if not isinstance(business_date, str) or f"'{business_date}'" not in normalized:
            raise QueryBuildError("current business_date literal is missing")
        actual_sources = self._physical_data_sources(normalized)
        expected_sources = {source.lower() for source in binding.data_sources}
        if actual_sources != expected_sources:
            raise QueryBuildError(
                "physical data sources must exactly equal the registered set: "
                f"expected={sorted(expected_sources)}, actual={sorted(actual_sources)}"
            )
        for token in binding.protected_tokens:
            if not re.search(rf"(?i)\b{re.escape(token)}\b", normalized):
                raise QueryBuildError(f"protected metric token is missing: {token}")
        for predicate in binding.required_predicates:
            predicate_pattern = r"\s*".join(
                re.escape(part) for part in predicate.split()
            )
            if not re.search(rf"(?i)\b{predicate_pattern}\b", normalized):
                raise QueryBuildError(f"required predicate is missing: {predicate}")

        config = binding.dimension_config or {}
        if config.get("post_primary") == "game_background":
            game_id = parameters.get("game_id")
            if (
                isinstance(game_id, bool)
                or not isinstance(game_id, int)
                or game_id <= 0
            ):
                raise QueryBuildError(
                    "game background game_id must be a positive integer"
                )
            if not re.search(
                rf"(?i)\bapp_id\s*=\s*{game_id}\b", normalized
            ) or not re.search(
                rf"(?i)\bgame_id\s*=\s*{game_id}\b", normalized
            ):
                raise QueryBuildError(
                    "game background SQL changed its frozen game identity"
                )
            if "max_pt('tap_bi.dwd_app_operation_events_df')" not in normalized.lower():
                raise QueryBuildError(
                    "game background operation snapshot filter is missing"
                )
            if not re.search(r"(?i)\bWHERE\s+dt\s+BETWEEN\b", normalized):
                raise QueryBuildError(
                    "game background lifecycle partition range is missing"
                )
            return

        if not re.search(r"(?i)\bWHERE\s+dt\s+BETWEEN\b", normalized):
            raise QueryBuildError("registered partition range filter is missing")
        if not re.search(r"(?i)\bplatform\s*=\s*'ANDROID'", normalized):
            raise QueryBuildError("Android platform filter is missing")

        game_type = parameters.get("game_type")
        if game_type not in {"app", "sandbox"}:
            raise QueryBuildError("game_type must be app or sandbox")
        if not re.search(
            rf"(?i)\bgame_type\s*=\s*'{re.escape(game_type)}'", normalized
        ):
            raise QueryBuildError("current game_type filter is missing")

        if binding.dimension is not None:
            source_field = config.get("source_field")
            required_dimension_fields = {source_field}
            if config.get("secondary") is True:
                required_dimension_fields.add(config.get("parent_source_field"))
                parent_value = config.get("parent_value")
                if (
                    not isinstance(parent_value, str)
                    or not parent_value
                    or "outside_parent" not in normalized
                    or ("'" + parent_value.replace("'", "''") + "'")
                    not in normalized
                ):
                    raise QueryBuildError(
                        "secondary SQL changed its parent identity or closure bucket"
                    )
            if any(
                not isinstance(field, str)
                or not re.search(rf"(?i)\b{re.escape(field)}\b", normalized)
                for field in required_dimension_fields
            ):
                raise QueryBuildError(
                    f"current dimension source is missing: {binding.dimension}"
                )
            for other_field in self.contracts.all_primary_dimension_fields() - {
                source_field
            }:
                if re.search(rf"(?i)\b{re.escape(other_field)}\b", normalized):
                    raise QueryBuildError(
                        f"SQL injects another primary dimension field: {other_field}"
                    )

    def validate_repair(
        self,
        baseline_sql: str,
        failed_sql: str,
        repaired_sql: str,
        binding: QueryBinding,
        parameters: dict[str, Any],
    ) -> str:
        if not isinstance(repaired_sql, str) or not repaired_sql.strip():
            raise QueryBuildError("repaired_sql must be a non-empty string")
        if sha256_text(failed_sql) == sha256_text(repaired_sql):
            raise QueryBuildError("repaired SQL must differ from the failed SQL")
        self.validate_sql(repaired_sql, binding, parameters)

        original_semantic = self._sql_without_comments(baseline_sql)
        repaired_semantic = self._sql_without_comments(repaired_sql)
        original_sources = self._physical_data_sources(original_semantic)
        repaired_sources = self._physical_data_sources(repaired_semantic)
        if original_sources != repaired_sources:
            raise QueryBuildError(
                "repair cannot add, remove, or replace a physical data source"
            )

        protected = list(binding.protected_tokens)
        if binding.dimension_config:
            source_field = binding.dimension_config.get("source_field")
            if isinstance(source_field, str):
                protected.append(source_field)
            parent_source = binding.dimension_config.get("parent_source_field")
            if isinstance(parent_source, str):
                protected.append(parent_source)
        for token in protected:
            original_count = len(
                re.findall(rf"(?i)\b{re.escape(token)}\b", original_semantic)
            )
            repaired_count = len(
                re.findall(rf"(?i)\b{re.escape(token)}\b", repaired_semantic)
            )
            if original_count != repaired_count:
                raise QueryBuildError(
                    f"repair changed protected token usage for {token}: "
                    f"{original_count} -> {repaired_count}"
                )
        for predicate in binding.required_predicates:
            pattern = r"\s*".join(re.escape(part) for part in predicate.split())
            original_count = len(re.findall(rf"(?i)\b{pattern}\b", original_semantic))
            repaired_count = len(re.findall(rf"(?i)\b{pattern}\b", repaired_semantic))
            if original_count != repaired_count:
                raise QueryBuildError(
                    f"repair changed required predicate usage: {predicate}"
                )
        if binding.dimension_config and binding.dimension_config.get("secondary"):
            parent_value = binding.dimension_config["parent_value"]
            parent_literal = "'" + parent_value.replace("'", "''") + "'"
            if original_semantic.count(parent_literal) != repaired_semantic.count(
                parent_literal
            ):
                raise QueryBuildError("repair cannot change the frozen parent value")

        date_pattern = re.compile(r"'\d{4}-\d{2}-\d{2}'")
        if set(date_pattern.findall(original_semantic)) != set(
            date_pattern.findall(repaired_semantic)
        ):
            raise QueryBuildError("repair cannot change the registered date scope")

        if self._normalize_sql(self._first_cte_body(original_semantic)) != (
            self._normalize_sql(self._first_cte_body(repaired_semantic))
        ):
            raise QueryBuildError(
                "repair cannot change the frozen range CTE or its WHERE scope"
            )
        if self._function_calls(original_semantic, "DATEADD") != self._function_calls(
            repaired_semantic, "DATEADD"
        ):
            raise QueryBuildError("repair cannot change DATEADD offsets or arguments")

        protected_expressions = set(binding.protected_tokens)
        if binding.dimension_config:
            source_field = binding.dimension_config.get("source_field")
            if isinstance(source_field, str):
                protected_expressions.add(source_field)
        if self._protected_select_expressions(
            original_semantic, protected_expressions
        ) != self._protected_select_expressions(
            repaired_semantic, protected_expressions
        ):
            raise QueryBuildError(
                "repair cannot change registered metric aggregation expressions"
            )
        if self._quality_select_expressions(
            original_semantic
        ) != self._quality_select_expressions(repaired_semantic):
            raise QueryBuildError(
                "repair cannot change quality-bucket or residual-bucket logic"
            )
        if self._final_output_columns(original_semantic) != self._final_output_columns(
            repaired_semantic
        ):
            raise QueryBuildError("repair cannot change final output columns")

        similarity = difflib.SequenceMatcher(
            None,
            self._normalize_sql(original_semantic),
            self._normalize_sql(repaired_semantic),
        ).ratio()
        changed_lines = sum(
            1
            for line in difflib.ndiff(
                baseline_sql.splitlines(), repaired_sql.splitlines()
            )
            if line.startswith(("+ ", "- "))
        )
        if similarity < 0.85 or changed_lines > 32:
            raise QueryBuildError(
                "repair exceeds the registered semantic diff budget"
            )

        diff = "".join(
            difflib.unified_diff(
                baseline_sql.splitlines(keepends=True),
                repaired_sql.splitlines(keepends=True),
                fromfile="attempt-0.sql",
                tofile="repaired.sql",
            )
        )
        if not diff:
            raise QueryBuildError("repair did not produce a reviewable SQL diff")
        return diff

    def _load_query_spec(
        self, path: Path
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        try:
            query_spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise QueryBuildError(f"cannot load QuerySpec {path}: {exc}") from exc
        if not isinstance(query_spec, dict):
            raise QueryBuildError(f"QuerySpec must be a mapping: {path}")
        required = {"version", "id", "parameters", "sql", "output", "quality"}
        missing = sorted(required - set(query_spec))
        if missing:
            raise QueryBuildError(f"QuerySpec missing required fields {missing}: {path}")
        if not isinstance(query_spec["id"], str) or not query_spec["id"]:
            raise QueryBuildError(f"QuerySpec id must be non-empty: {path}")
        if not isinstance(query_spec["parameters"], dict):
            raise QueryBuildError(f"QuerySpec parameters must be a mapping: {path}")
        if not isinstance(query_spec["sql"], str) or not query_spec["sql"].strip():
            raise QueryBuildError(f"QuerySpec SQL must be non-empty: {path}")
        for field in ("output", "quality"):
            if not isinstance(query_spec[field], dict):
                raise QueryBuildError(f"QuerySpec {field} must be a mapping: {path}")
        return query_spec["sql"], query_spec["parameters"]

    def _load_markdown_template(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```sql\s*\n(.*?)\n```", text, flags=re.DOTALL)
        candidates = [
            block
            for block in blocks
            if re.match(r"(?is)^\s*WITH\b", block)
            and all(placeholder in block for placeholder in DIMENSION_PLACEHOLDER_COUNTS)
        ]
        if len(candidates) != 1:
            raise QueryBuildError(
                f"Markdown template must contain exactly one executable SQL block: {path}"
            )
        return candidates[0]

    def _replace_dimension_placeholders(
        self, sql: str, binding: QueryBinding
    ) -> str:
        config = binding.dimension_config
        if binding.dimension is None or not isinstance(config, dict):
            raise QueryBuildError("primary template requires a registered dimension")
        replacements = {
            "__DIMENSION_SOURCE_FIELD__": config["source_field"],
            "__DIMENSION_QUALITY_SOURCE_EXPR__": config[
                "quality_source_expression"
            ],
            "__DIMENSION_VALUE_EXPR__": config["value_expression"],
            "__DIMENSION_LABEL_EXPR__": config["label_expression"],
        }
        for placeholder, expected_count in DIMENSION_PLACEHOLDER_COUNTS.items():
            actual_count = sql.count(placeholder)
            if actual_count != expected_count:
                raise QueryBuildError(
                    f"placeholder {placeholder} expected {expected_count} times, "
                    f"found {actual_count}"
                )
            sql = sql.replace(placeholder, replacements[placeholder])
        return sql

    def _validate_parameters(
        self,
        specs: dict[str, Any],
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(parameters, dict):
            raise QueryBuildError("query parameters must be a mapping")
        unknown = sorted(set(parameters) - set(specs))
        missing = sorted(set(specs) - set(parameters))
        if unknown or missing:
            raise QueryBuildError(
                f"query parameter mismatch; missing={missing}, unknown={unknown}"
            )
        validated: dict[str, Any] = {}
        for name, raw_spec in specs.items():
            if not isinstance(raw_spec, dict):
                raise QueryBuildError(f"parameter spec must be a mapping: {name}")
            parameter_type = raw_spec.get("type")
            value = parameters[name]
            if parameter_type == "date":
                if not isinstance(value, str):
                    raise QueryBuildError(f"date parameter must be a string: {name}")
                try:
                    parsed = date.fromisoformat(value)
                except ValueError as exc:
                    raise QueryBuildError(f"invalid date parameter {name}: {value}") from exc
                if parsed.isoformat() != value:
                    raise QueryBuildError(f"date parameter must use YYYY-MM-DD: {name}")
            elif parameter_type == "enum":
                allowed_values = raw_spec.get("allowed_values")
                if not isinstance(value, str) or value not in allowed_values:
                    raise QueryBuildError(
                        f"enum parameter {name} must be one of {allowed_values}"
                    )
            elif parameter_type == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise QueryBuildError(f"integer parameter required: {name}")
            elif parameter_type == "string":
                if not isinstance(value, str):
                    raise QueryBuildError(f"string parameter required: {name}")
            else:
                raise QueryBuildError(
                    f"unsupported parameter type for {name}: {parameter_type}"
                )
            validated[name] = value
        return validated

    def _render_parameters(
        self,
        sql: str,
        specs: dict[str, dict[str, Any]],
        parameters: dict[str, Any],
    ) -> str:
        placeholders = set(PARAMETER_PATTERN.findall(sql))
        if placeholders != set(specs):
            raise QueryBuildError(
                f"SQL parameter placeholders do not match QuerySpec: {placeholders}"
            )
        for name, value in parameters.items():
            parameter_type = specs[name]["type"]
            if parameter_type in {"date", "enum", "string"}:
                literal = "'" + value.replace("'", "''") + "'"
            elif parameter_type == "integer":
                literal = str(value)
            else:  # pragma: no cover - rejected during validation
                raise QueryBuildError(f"unsupported parameter type: {parameter_type}")
            sql = re.sub(rf"\$\{{{re.escape(name)}\}}", literal, sql)
        if PARAMETER_PATTERN.search(sql):
            raise QueryBuildError("unresolved SQL parameters remain after rendering")
        return sql

    def _sql_without_comments(self, sql: str) -> str:
        without_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
        return re.sub(r"--[^\n]*", " ", without_blocks)

    def _sql_without_literals(self, sql: str) -> str:
        return re.sub(r"'(?:''|[^'])*'", "''", sql)

    def _physical_data_sources(self, sql: str) -> set[str]:
        sources = set()
        for match in re.finditer(
            r"(?i)\b(?:FROM|JOIN)\s+"
            r"(`?[A-Za-z_][A-Za-z0-9_]*`?\s*\.\s*"
            r"`?[A-Za-z_][A-Za-z0-9_]*`?)",
            sql,
        ):
            sources.add(re.sub(r"[`\s]", "", match.group(1)).lower())
        return sources

    def _normalize_sql(self, sql: str) -> str:
        return re.sub(r"\s+", " ", sql).strip().lower()

    def _first_cte_body(self, sql: str) -> str:
        match = re.search(
            r"(?is)^\s*WITH\s+[A-Za-z_][A-Za-z0-9_]*\s+AS\s*\(", sql
        )
        if not match:
            raise QueryBuildError("registered SQL must start with a named CTE")
        opening = sql.find("(", match.start())
        closing = self._matching_parenthesis(sql, opening)
        return sql[opening + 1 : closing]

    def _function_calls(self, sql: str, function_name: str) -> tuple[str, ...]:
        calls: list[str] = []
        pattern = re.compile(rf"(?i)\b{re.escape(function_name)}\s*\(")
        for match in pattern.finditer(sql):
            opening = sql.find("(", match.start())
            closing = self._matching_parenthesis(sql, opening)
            calls.append(self._normalize_sql(sql[match.start() : closing + 1]))
        return tuple(calls)

    def _matching_parenthesis(self, sql: str, opening: int) -> int:
        depth = 0
        quote: str | None = None
        index = opening
        while index < len(sql):
            char = sql[index]
            if quote:
                if char == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        raise QueryBuildError("SQL contains unbalanced parentheses")

    def _select_expression_lists(self, sql: str) -> list[list[str]]:
        result: list[list[str]] = []
        for match in re.finditer(r"(?i)\bSELECT\b", sql):
            start = match.end()
            depth = self._depth_at(sql, start)
            end = self._find_keyword_at_depth(sql, "FROM", start, depth)
            if end is None:
                continue
            result.append(self._split_at_depth(sql[start:end], ","))
        return result

    def _depth_at(self, sql: str, end: int) -> int:
        depth = 0
        quote: str | None = None
        index = 0
        while index < end:
            char = sql[index]
            if quote:
                if char == quote:
                    if index + 1 < end and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        return depth

    def _find_keyword_at_depth(
        self, sql: str, keyword: str, start: int, depth: int
    ) -> int | None:
        pattern = re.compile(rf"(?i)\b{re.escape(keyword)}\b")
        for match in pattern.finditer(sql, start):
            if self._depth_at(sql, match.start()) == depth:
                return match.start()
        return None

    def _split_at_depth(self, value: str, separator: str) -> list[str]:
        parts: list[str] = []
        start = 0
        depth = 0
        quote: str | None = None
        index = 0
        while index < len(value):
            char = value[index]
            if quote:
                if char == quote:
                    if index + 1 < len(value) and value[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == separator and depth == 0:
                parts.append(value[start:index])
                start = index + 1
            index += 1
        parts.append(value[start:])
        return [part.strip() for part in parts if part.strip()]

    def _protected_select_expressions(
        self, sql: str, protected_tokens: set[str]
    ) -> tuple[str, ...]:
        expressions = []
        for select_list in self._select_expression_lists(sql):
            for expression in select_list:
                if any(
                    re.search(rf"(?i)\b{re.escape(token)}\b", expression)
                    for token in protected_tokens
                ):
                    expressions.append(self._normalize_sql(expression))
        return tuple(expressions)

    def _quality_select_expressions(self, sql: str) -> tuple[str, ...]:
        markers = (
            "'quality'",
            "'residual'",
            "'__none__'",
            "'__quality__'",
            "'__other_below_threshold__'",
            "'unmatched'",
            "'invalid_'",
        )
        expressions = []
        for select_list in self._select_expression_lists(sql):
            for expression in select_list:
                normalized = self._normalize_sql(expression)
                if any(marker in normalized for marker in markers):
                    expressions.append(normalized)
        return tuple(expressions)

    def _final_output_columns(self, sql: str) -> tuple[str, ...]:
        select_lists = self._select_expression_lists(sql)
        if not select_lists:
            raise QueryBuildError("SQL has no SELECT list")
        columns: list[str] = []
        for expression in select_lists[-1]:
            alias = re.search(
                r"(?is)\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", expression
            )
            if alias:
                columns.append(alias.group(1).lower())
                continue
            identifier = re.search(
                r"(?is)([A-Za-z_][A-Za-z0-9_]*)\s*$", expression
            )
            if not identifier:
                raise QueryBuildError("final SELECT expression lacks a stable column")
            columns.append(identifier.group(1).lower())
        return tuple(columns)
