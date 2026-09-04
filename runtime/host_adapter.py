from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from .contracts import canonical_sha256
from .query_observation import observe_query
from .receipts import TrustedReceiptVerifier
from .runner import AttributionRunner, RunnerError


@dataclass(frozen=True)
class HostQueryResponse:
    query_id: str
    receipt_id: str
    raw_result: dict[str, Any] | None = None
    error_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DViewQueryExecutor(Protocol):
    def execute_read_only(self, sql: str) -> HostQueryResponse: ...


@dataclass(frozen=True)
class DViewExecutionError(Exception):
    query_id: str
    error_class: str
    error_code: str
    error_message: str
    receipt_id: str | None = None


class ProductionDViewExecutor:
    """Narrow adapter for the current read-only DView MCP query response."""

    _QUERY_FOOTER = re.compile(
        r"\*查询ID `(?P<query_id>[^`]+)`, 共 (?P<row_count>\d+) 行, 耗时 [^*]+\*"
    )
    _INTEGER = re.compile(r"[-+]?\d+")
    _NUMBER = re.compile(
        r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?"
    )
    _TEXT_COLUMNS = {
        "analysis_date",
        "game_type",
        "scope",
        "bucket_kind",
        "error_code",
        "dimension_value",
        "dimension_label",
    }

    def __init__(
        self,
        query: Callable[..., Any],
        *,
        database_type: str = "MaxCompute",
        limit: int = 250,
    ):
        if not callable(query):
            raise RunnerError("DView query client must be callable")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise RunnerError("DView query limit must be a positive integer")
        self.query = query
        self.database_type = database_type
        self.limit = limit

    def execute_read_only(self, sql: str) -> HostQueryResponse:
        try:
            response = self.query(
                sql=sql,
                database_type=self.database_type,
                limit=self.limit,
            )
        except DViewExecutionError as exc:
            return HostQueryResponse(
                query_id=exc.query_id,
                receipt_id=exc.receipt_id or exc.query_id,
                error_class=exc.error_class,
                error_code=exc.error_code,
                error_message=exc.error_message,
            )
        return self._normalize_response(response)

    def _normalize_response(self, response: Any) -> HostQueryResponse:
        payload = self._unwrap_payload(response)
        if isinstance(payload, str):
            query_id, raw_result = self._parse_markdown_result(payload)
        elif isinstance(payload, Mapping):
            query_id = self._required_text(payload, "query_id")
            columns = self._ordered_column_names(payload.get("columns"))
            rows = self._normalize_rows(payload.get("rows"), columns)
            raw_result = {"columns": columns, "rows": rows}
        else:
            raise RunnerError("DView MCP response has an unsupported result shape")
        return HostQueryResponse(
            query_id=query_id,
            receipt_id=query_id,
            raw_result=raw_result,
        )

    def _unwrap_payload(self, response: Any) -> Any:
        structured = getattr(response, "structuredContent", None)
        if structured is not None:
            response = structured
        if isinstance(response, Mapping) and "structuredContent" in response:
            response = response["structuredContent"]
        if isinstance(response, Mapping) and set(response) == {"result"}:
            return response["result"]
        return response

    def _parse_markdown_result(self, content: str) -> tuple[str, dict[str, Any]]:
        footer = self._QUERY_FOOTER.search(content)
        if footer is None:
            raise RunnerError("DView MCP response lacks its query ID footer")
        table_lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip().startswith("|") and line.strip().endswith("|")
        ]
        if len(table_lines) < 2:
            raise RunnerError("DView MCP response lacks a Markdown result table")
        columns = self._split_markdown_row(table_lines[0])
        separator = self._split_markdown_row(table_lines[1])
        if not columns or len(separator) != len(columns) or any(
            re.fullmatch(r":?-{3,}:?", cell) is None for cell in separator
        ):
            raise RunnerError("DView MCP response has an invalid table header")
        rows = []
        for row_index, line in enumerate(table_lines[2:]):
            cells = self._split_markdown_row(line)
            if len(cells) != len(columns):
                raise RunnerError(
                    f"DView MCP row {row_index} does not match the column count"
                )
            rows.append(
                [
                    self._markdown_scalar(cell, column)
                    for column, cell in zip(columns, cells, strict=True)
                ]
            )
        declared_count = int(footer.group("row_count"))
        if declared_count != len(rows):
            raise RunnerError("DView MCP footer row count does not match the table")
        return footer.group("query_id"), {"columns": columns, "rows": rows}

    def _split_markdown_row(self, line: str) -> list[str]:
        body = line.strip()[1:-1]
        cells: list[str] = []
        current: list[str] = []
        escaped = False
        for character in body:
            if escaped:
                current.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "|":
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        if escaped:
            current.append("\\")
        cells.append("".join(current).strip())
        return cells

    def _markdown_scalar(self, value: str, column: str) -> Any:
        if value in {"None", "NULL", "null", "<null>"}:
            return None
        if column in self._TEXT_COLUMNS:
            return value
        if self._INTEGER.fullmatch(value):
            return int(value)
        if self._NUMBER.fullmatch(value):
            numeric = float(value)
            if not math.isfinite(numeric):
                raise RunnerError("DView MCP returned a non-finite numeric value")
            return numeric
        return value

    def _ordered_column_names(self, columns: Any) -> list[str]:
        if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes)):
            raise RunnerError("DView structured columns must be an ordered array")
        names = []
        for column in columns:
            if isinstance(column, str):
                name = column
            elif isinstance(column, Mapping):
                name = column.get("name")
            else:
                name = None
            if not isinstance(name, str) or not name.strip():
                raise RunnerError("DView column metadata lacks a name")
            names.append(name)
        if not names or len(names) != len(set(names)):
            raise RunnerError("DView column names must be non-empty and unique")
        return names

    def _normalize_rows(self, rows: Any, columns: list[str]) -> list[list[Any]]:
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise RunnerError("DView structured rows must be an array")
        normalized = []
        for row_index, row in enumerate(rows):
            if isinstance(row, Mapping):
                if set(row) != set(columns):
                    raise RunnerError(
                        f"DView structured row {row_index} does not match columns"
                    )
                values = [row[name] for name in columns]
            elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
                if len(row) != len(columns):
                    raise RunnerError(
                        f"DView structured row {row_index} does not match columns"
                    )
                values = list(row)
            else:
                raise RunnerError(f"DView structured row {row_index} is invalid")
            normalized.append([self._transport_scalar(value) for value in values])
        return normalized

    def _transport_scalar(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float)):
            return value
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        raise RunnerError(f"unsupported DView transport value: {type(value).__name__}")

    def _required_text(self, value: Mapping[str, Any], key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result.strip():
            raise RunnerError(f"DView structured response lacks {key}")
        return result


class HostDViewAdapter:
    """Executes the issued SQL without exposing SQL copy/paste to the model."""

    def __init__(
        self,
        *,
        runner: AttributionRunner,
        executor: DViewQueryExecutor,
        receipt_signer: TrustedReceiptVerifier,
    ):
        if runner.trusted_receipt_verifier is not receipt_signer:
            raise RunnerError("runner and Host adapter must share one receipt authority")
        self.runner = runner
        self.executor = executor
        self.receipt_signer = receipt_signer

    def execute_current(self, run_id: str) -> dict[str, Any]:
        ticket = self.runner.next_action(run_id)
        if ticket.get("action") != "execute_query":
            return ticket
        return self._execute_ticket(run_id, ticket)

    def execute_until_blocked(self, run_id: str) -> dict[str, Any]:
        executed_query_count = 0
        while True:
            ticket = self.runner.next_action(run_id)
            if ticket.get("action") != "execute_query":
                return {**ticket, "executed_query_count": executed_query_count}
            self._execute_ticket(run_id, ticket)
            executed_query_count += 1

    def _execute_ticket(
        self, run_id: str, ticket: dict[str, Any]
    ) -> dict[str, Any]:
        with observe_query(
            stage="attribution",
            step_id=ticket["step_id"],
            attempt_no=ticket["attempt_no"],
        ):
            response = self.executor.execute_read_only(ticket["rendered_sql"])
            common = {
                "step_id": ticket["step_id"],
                "attempt_no": ticket["attempt_no"],
                "receipt_type": "trusted_host_receipt",
                "receipt_key_id": self.receipt_signer.key_id,
                "receipt_id": response.receipt_id,
                "submitted_sql_sha256": ticket["rendered_sql_sha256"],
                "query_id": response.query_id,
            }
            if response.raw_result is not None:
                event = {
                    "event": "query_returned",
                    **common,
                    "raw_result": response.raw_result,
                    "raw_result_sha256": canonical_sha256(response.raw_result),
                }
            else:
                error_fields = (
                    response.error_class,
                    response.error_code,
                    response.error_message,
                )
                if not all(
                    isinstance(value, str) and value.strip() for value in error_fields
                ):
                    raise RunnerError(
                        "Host query response must contain raw_result or a complete raw error"
                    )
                event = {
                    "event": "query_error",
                    **common,
                    "error_class": response.error_class,
                    "error_code": response.error_code,
                    "error_message": response.error_message,
                }
            event["receipt_signature"] = self.receipt_signer.sign(run_id, event)
            return self.runner.record(run_id, event)
