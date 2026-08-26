from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from runtime.host_adapter import DViewExecutionError

_QUERY_ID_PATTERNS = (
    re.compile(r"查询ID\s*:?\s*`(?P<query_id>[^`]+)`"),
    re.compile(r"\bquery_id=(?P<query_id>[A-Za-z0-9._:-]+)\b", re.IGNORECASE),
)
_ERROR_CODE = re.compile(r"\bODPS-\d{6,8}\b", re.IGNORECASE)
_ERROR_CATEGORY = re.compile(r"错误类别:\s*([a-z_]+)", re.IGNORECASE)
_QUERY_ID_FOOTER = re.compile(r"\n*\*查询ID\s*:?\s*`[^`]+`\*\s*$")


class DViewMCPResponseError(RuntimeError):
    """A DView response could not be bound to verifiable query evidence."""


class DViewQuerySession:
    def __init__(self, session: ClientSession):
        self._session = session

    async def query(
        self,
        *,
        sql: str,
        database_type: str,
        limit: int,
    ) -> Any:
        result = await self._session.call_tool(
            "query",
            arguments={
                "sql": sql,
                "database_type": database_type,
                "limit": limit,
            },
        )
        text = _text_content(result)
        if bool(getattr(result, "isError", False)) or text.startswith("**查询失败**"):
            _raise_typed_query_error(text)

        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return {"structuredContent": structured}
        if not text:
            raise DViewMCPResponseError("DView query returned no parseable content")
        return text


class DViewMCPClient:
    def __init__(
        self,
        *,
        url: str,
        bearer_token: str,
        read_timeout_seconds: int,
    ):
        self._url = url
        self._bearer_token = bearer_token
        self._read_timeout_seconds = read_timeout_seconds

    @asynccontextmanager
    async def session(self) -> AsyncIterator[DViewQuerySession]:
        timeout = httpx.Timeout(
            connect=30.0,
            read=None,
            write=30.0,
            pool=30.0,
        )
        async with (
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._bearer_token}"},
                timeout=timeout,
            ) as http_client,
            streamable_http_client(
                self._url,
                http_client=http_client,
            ) as (read_stream, write_stream, _),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self._read_timeout_seconds),
            ) as session,
        ):
            await session.initialize()
            yield DViewQuerySession(session)


def _text_content(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", ()):
        if getattr(block, "type", None) == "text":
            value = getattr(block, "text", "")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).strip()


def _raise_typed_query_error(text: str) -> None:
    query_id = None
    for pattern in _QUERY_ID_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            query_id = match.group("query_id")
            break
    if not query_id:
        raise DViewMCPResponseError("DView query failed without a verifiable query ID")

    category_match = _ERROR_CATEGORY.search(text)
    category = category_match.group(1).lower() if category_match else "query_execution"
    lowered = text.lower()
    if category == "semantic_analysis":
        error_class = "semantic_analysis"
    elif category == "permission_denied" or any(
        marker in lowered
        for marker in ("permission", "access denied", "unauthorized", "forbidden")
    ):
        error_class = "permission"
    else:
        error_class = "query_execution"

    code_match = _ERROR_CODE.search(text)
    error_code = code_match.group(0).upper() if code_match else category
    message = _QUERY_ID_FOOTER.sub("", text).strip()
    message = message.replace(query_id, "[query-id-retained-by-host]")[:2000]
    raise DViewExecutionError(
        query_id=query_id,
        error_class=error_class,
        error_code=error_code,
        error_message=message or "DView query failed",
    )
