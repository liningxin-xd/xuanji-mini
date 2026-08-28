from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


EXPECTED_TOOLS = {
    "xuanji_run_task",
    "xuanji_submit_repair",
    "xuanji_finalize",
}
TASK_ID = "container-smoke-task"
SNAPSHOT_TASK_ID = "container-root-cache-task"
PAYLOAD = {
    "projectName": "tap_dw",
    "dqcEntityQuality": {
        "entityName": "ads_dmg_quality_platform_download_chain_monitor_1d",
        "actualExpression": "dt=2026-08-24",
    },
    "ruleChecks": [
        {
            "ruleName": "container smoke unregistered rule",
            "tableName": "ads_dmg_quality_platform_download_chain_monitor_1d",
            "actualExpression": "dt=2026-08-24",
        }
    ],
}
WRITER_PATCH = {
    "summary": "The rule is not present in the registered route contract.",
    "finding_texts": {},
    "evidence_limits": ["insufficient_definition"],
    "recommended_action": "Register the exact DQC rule before attribution.",
}
SNAPSHOT_PAYLOAD = {
    "projectName": "tap_dw",
    "dqcEntityQuality": {
        "entityName": "ads_dmg_quality_platform_download_chain_monitor_1d",
        "actualExpression": "dt=2026-08-24",
    },
    "ruleChecks": [
        {
            "ruleName": "【apk下载完成率】最近1天_低于80%",
            "tableName": "ads_dmg_quality_platform_download_chain_monitor_1d",
            "actualExpression": "dt=2026-08-24",
            "property": "game_download_complete_rate_1d",
            "op": ">=",
            "expectValue": 0.8,
        },
        {
            "ruleName": "【apk下载失败率】最近1天_高于3%",
            "tableName": "ads_dmg_quality_platform_download_chain_monitor_1d",
            "actualExpression": "dt=2026-08-24",
            "property": "game_download_failed_rate_1d",
            "op": "<=",
            "expectValue": 0.03,
        },
    ],
}
SNAPSHOT_WRITER_PATCH = {
    "summary": "The registered root anomaly already existed without new adverse change.",
    "finding_texts": {},
    "evidence_limits": [],
    "recommended_action": "Continue monitoring the registered Android download chain.",
}


class ContainerProbeError(RuntimeError):
    pass


async def unauthenticated_status(url: str) -> int:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "container-smoke", "version": "1"},
        },
    }
    async with httpx.AsyncClient(timeout=2.0) as client:
        response = await client.post(
            url,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json=request,
        )
    return response.status_code


async def exercise_task(
    url: str, token: str, *, resumed: bool
) -> dict[str, Any]:
    async with _session(url, token) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        if names != EXPECTED_TOOLS:
            raise ContainerProbeError(f"unexpected model tool surface: {sorted(names)}")

        run = await _call(session, "xuanji_run_task", {
            "task_id": TASK_ID,
            "dqc_payload": PAYLOAD,
        })
        if resumed:
            _require_action(run, "task_complete")
            _require_signed_handoff(run)
            return run

        _require_action(run, "write_conclusion")
        investigation_id = run.get("investigation_id")
        if not isinstance(investigation_id, str):
            raise ContainerProbeError("writer response lacks investigation identity")
        arguments = {
            "task_id": TASK_ID,
            "investigation_id": investigation_id,
            "writer_patch": WRITER_PATCH,
        }
        completed = await _call(session, "xuanji_finalize", arguments)
        _require_action(completed, "task_complete")
        _require_signed_handoff(completed)
        if completed.get("overall_status") != "failed":
            raise ContainerProbeError("unknown-only task must have failed overall status")

        repeated = await _call(session, "xuanji_finalize", arguments)
        if repeated != completed:
            raise ContainerProbeError("identical finalize retry is not idempotent")

        conflicting = dict(arguments)
        conflicting["writer_patch"] = {
            **WRITER_PATCH,
            "summary": "Conflicting writer content must be rejected.",
        }
        result = await session.call_tool("xuanji_finalize", arguments=conflicting)
        if not result.isError:
            raise ContainerProbeError("conflicting finalize was accepted")

        conflicting_payload = {
            **PAYLOAD,
            "ruleChecks": [
                {**PAYLOAD["ruleChecks"][0], "ruleName": "changed immutable rule"}
            ],
        }
        result = await session.call_tool(
            "xuanji_run_task",
            arguments={"task_id": TASK_ID, "dqc_payload": conflicting_payload},
        )
        if not result.isError:
            raise ContainerProbeError("conflicting task payload was accepted")
        return completed


async def assert_profile_mismatch_rejected(url: str, token: str) -> None:
    async with _session(url, token) as session:
        result = await session.call_tool(
            "xuanji_run_task",
            arguments={"task_id": TASK_ID, "dqc_payload": PAYLOAD},
        )
        if not result.isError:
            raise ContainerProbeError("cross-profile task resume was accepted")


async def exercise_snapshot_task(url: str, token: str, *, resumed: bool) -> None:
    async with _session(url, token) as session:
        run = await _call(
            session,
            "xuanji_run_task",
            {"task_id": SNAPSHOT_TASK_ID, "dqc_payload": SNAPSHOT_PAYLOAD},
        )
        _require_action(run, "write_conclusion")
        if "snapshot" in json.dumps(run, ensure_ascii=False):
            raise ContainerProbeError("root snapshot leaked into the model response")
        if not resumed:
            return

        first_id = run.get("investigation_id")
        if not isinstance(first_id, str):
            raise ContainerProbeError("first snapshot writer lacks investigation identity")
        second = await _call(
            session,
            "xuanji_finalize",
            {
                "task_id": SNAPSHOT_TASK_ID,
                "investigation_id": first_id,
                "writer_patch": SNAPSHOT_WRITER_PATCH,
            },
        )
        _require_action(second, "write_conclusion")
        second_id = second.get("investigation_id")
        if not isinstance(second_id, str) or second_id == first_id:
            raise ContainerProbeError("second snapshot writer identity is invalid")
        completed = await _call(
            session,
            "xuanji_finalize",
            {
                "task_id": SNAPSHOT_TASK_ID,
                "investigation_id": second_id,
                "writer_patch": SNAPSHOT_WRITER_PATCH,
            },
        )
        _require_action(completed, "task_complete")
        if completed.get("overall_status") != "completed":
            raise ContainerProbeError("snapshot task did not complete successfully")
        if "snapshot" in json.dumps([second, completed], ensure_ascii=False):
            raise ContainerProbeError("root snapshot leaked after task resume")


@asynccontextmanager
async def _session(url: str, token: str) -> AsyncIterator[ClientSession]:
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        ) as client,
        streamable_http_client(url, http_client=client) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=30),
        ) as session,
    ):
        await session.initialize()
        yield session


async def _call(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = await session.call_tool(name, arguments=arguments)
    if result.isError:
        raise ContainerProbeError(f"{name} returned a tool error")
    structured = result.structuredContent
    if isinstance(structured, dict):
        value = structured.get("result", structured)
        if isinstance(value, dict):
            return value
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ContainerProbeError(f"{name} returned no structured object")


def _require_action(value: dict[str, Any], expected: str) -> None:
    if value.get("action") != expected:
        raise ContainerProbeError(
            f"expected action {expected}, received {value.get('action')}"
        )


def _require_signed_handoff(value: dict[str, Any]) -> None:
    preview = value.get("analysis_preview")
    handoff = value.get("pipeline_handoff")
    if (
        not isinstance(preview, dict)
        or not isinstance(handoff, dict)
        or handoff.get("task_id") != value.get("task_id")
        or handoff.get("provider") != "xuanji-mini"
        or handoff.get("schema_version") != 1
        or not isinstance(handoff.get("signature"), str)
    ):
        raise ContainerProbeError("task completion lacks a signed pipeline handoff")
