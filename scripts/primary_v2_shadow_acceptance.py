from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from runtime.contracts import ContractError, RepositoryContracts, canonical_sha256
from runtime.task_assembler import writer_pack_size


_ROOT = Path(__file__).resolve().parents[1]
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SQL = re.compile(r"(?is)(\bselect\s+.+?\bfrom\b|\bwith\s+\w+\s+as\s*\()")
_MARKDOWN_TABLE = re.compile(r"(?m)^\s*\|.+\|\s*$\n^\s*\|\s*:?-{3,}")
_POST_TERMINAL = {"succeeded", "failed", "skipped_by_policy"}
_PRIMARY_TERMINAL = {"succeeded", "failed", "skipped_not_applicable"}
_FORBIDDEN_TEXT = (
    '"query_id"',
    '"raw_result"',
    '"raw_result_sha256"',
    '"rendered_sql"',
    '"receipt_signature"',
    '"validation_receipt": {"execution_mode"',
    "/var/lib/xuanji/",
    "/root-snapshots/",
    "validation-receipt.json",
)
_PRIVATE_VALUE_KEYS = {
    "query_id",
    "receipt_id",
    "receipt_signature",
    "raw_result_sha256",
    "rendered_sql_sha256",
    "submitted_sql_sha256",
}
_SCENARIOS = {
    "same-metric": {
        "rule_indexes": [[0, 1, 2]],
        "routes": [("download", "app", "下载完成率")],
    },
    "app-download": {
        "rule_indexes": [[0]],
        "routes": [("download", "app", "下载完成率")],
    },
    "sandbox-download": {
        "rule_indexes": [[0]],
        "routes": [("download", "sandbox", "下载完成率")],
    },
    "apk-install": {
        "rule_indexes": [[0]],
        "routes": [("install", "app", "下载安装完成率")],
    },
    "restart-resume": {
        "rule_indexes": [[0]],
        "routes": [("download", "app", "下载完成率")],
    },
    "paired-v2": {
        "rule_indexes": [[0]],
        "routes": [("download", "app", "下载完成率")],
    },
}


class ShadowAcceptanceError(ValueError):
    pass


def verify_shadow(
    *,
    data_root: Path | str,
    task_id: str,
    scenario: str,
    transcript_path: Path | str,
    allow_repair: bool = False,
) -> dict[str, Any]:
    if _TASK_ID.fullmatch(task_id) is None:
        raise ShadowAcceptanceError("task_id is invalid")
    scenario_contract = _SCENARIOS.get(scenario)
    if scenario_contract is None:
        raise ShadowAcceptanceError("shadow scenario is invalid")

    contracts = RepositoryContracts(_ROOT)
    root = Path(data_root).resolve()
    task_root = root / "tasks" / task_id
    state = _load_private_json(task_root / "state.json")
    sink = _load_private_json(
        root / "results" / "tasks" / task_id / "validated-task-result.json"
    )
    _verify_task_state(state, task_id, contracts)
    analysis, receipt = _verify_task_sink(sink, state, task_id)
    investigations = state.get("investigations")
    if not isinstance(investigations, list):
        raise ShadowAcceptanceError("task state investigations are invalid")
    _verify_scenario(investigations, scenario_contract, scenario)
    _verify_rule_coverage(state, investigations, analysis)

    (
        snapshot_hashes,
        root_query_count,
        private_markers,
        root_query_ids,
    ) = _verify_snapshots(
        task_root, investigations
    )
    if receipt.get("root_snapshot_sha256s") != snapshot_hashes:
        raise ShadowAcceptanceError("task receipt root snapshots changed")

    primary_query_count = 0
    primary_attempt_count = 0
    post_primary_query_count = 0
    post_primary_attempt_count = 0
    writer_pack_sizes: list[int] = []
    run_receipts: list[dict[str, Any]] = []
    completed_run_count = 0
    all_query_ids: set[str] = set(root_query_ids)
    for investigation in investigations:
        run_id = investigation.get("run_id")
        if run_id is None:
            continue
        if not isinstance(run_id, str) or _TASK_ID.fullmatch(run_id) is None:
            raise ShadowAcceptanceError("investigation run identity is invalid")
        completed_run_count += 1
        run_root = root / "runs" / run_id
        run_state = _load_private_json(run_root / "state.json")
        private_markers.update(_private_values(run_state))
        counts = _verify_run_state(
            run_state,
            run_id=run_id,
            route=investigation.get("route"),
            contracts=contracts,
            allow_repair=allow_repair,
            all_query_ids=all_query_ids,
        )
        primary_query_count += counts["primary_queries"]
        primary_attempt_count += counts["primary_attempts"]
        post_primary_query_count += counts["post_primary_queries"]
        post_primary_attempt_count += counts["post_primary_attempts"]

        run_sink = _load_private_json(
            root / "results" / run_id / "validated-result.json"
        )
        run_receipt = _verify_run_sink(
            run_sink,
            run_state=run_state,
            investigation=investigation,
            run_id=run_id,
        )
        run_receipts.append(run_receipt)
        private_markers.update(_private_values(run_sink))

        writer_pack = _load_private_json(run_root / "exports" / "writer-pack.json")
        if writer_pack.get("analysis_profile") != "primary_v2":
            raise ShadowAcceptanceError("writer pack does not prove primary_v2")
        size = writer_pack_size(writer_pack)
        if size > 12 * 1024:
            raise ShadowAcceptanceError("writer pack exceeds the 12 KB context budget")
        writer_pack_sizes.append(size)

    if completed_run_count == 0:
        raise ShadowAcceptanceError("primary_v2 shadow lacks a full-queue run")
    _verify_task_receipt_summaries(receipt, investigations, run_receipts)
    _verify_transcript(
        Path(transcript_path),
        private_markers,
        task_id=task_id,
        task_receipt=receipt,
        allow_repair=allow_repair,
    )
    return {
        "status": "passed",
        "scenario": scenario,
        "task_id": task_id,
        "analysis_profile": "primary_v2",
        "investigation_count": len(investigations),
        "run_count": completed_run_count,
        "root_query_count": root_query_count,
        "primary_query_count": primary_query_count,
        "primary_attempt_count": primary_attempt_count,
        "post_primary_query_count": post_primary_query_count,
        "post_primary_attempt_count": post_primary_attempt_count,
        "root_snapshot_count": len(snapshot_hashes),
        "writer_pack_max_bytes": max(writer_pack_sizes, default=0),
        "overall_status": analysis.get("overall_status"),
        "evidence_limits": {
            "normal_release_repair_free": not allow_repair,
            "post_primary_query_cap": 6,
            "enhancement_query_module_cap": 2,
        },
        "idempotency": {
            "task_id": task_id,
            "analysis_sha256": receipt["analysis_sha256"],
            "validation_receipt_sha256": receipt[
                "validation_receipt_sha256"
            ],
        },
    }


def _verify_task_state(
    state: dict[str, Any], task_id: str, contracts: RepositoryContracts
) -> None:
    expected = state.get("integrity_sha256")
    unsigned = dict(state)
    unsigned.pop("integrity_sha256", None)
    if (
        state.get("schema_version") != 1
        or state.get("task_id") != task_id
        or state.get("status") != "completed"
        or state.get("analysis_profile") != "primary_v2"
        or state.get("definition_bundle_sha256")
        != contracts.definition_bundle_sha256
        or _SHA256.fullmatch(str(state.get("payload_sha256"))) is None
        or expected != canonical_sha256(unsigned)
    ):
        raise ShadowAcceptanceError("task state identity, profile, or integrity is invalid")


def _verify_task_sink(
    sink: dict[str, Any], state: dict[str, Any], task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = sink.get("analysis")
    receipt = sink.get("validation_receipt")
    if (
        sink.get("task_id") != task_id
        or not isinstance(analysis, dict)
        or not isinstance(receipt, dict)
        or receipt.get("status") != "valid"
        or receipt.get("task_id") != task_id
        or receipt.get("payload_sha256") != state.get("payload_sha256")
        or receipt.get("definition_bundle_sha256")
        != state.get("definition_bundle_sha256")
        or receipt.get("analysis_sha256") != canonical_sha256(analysis)
        or receipt.get("analysis_sha256") != state.get("task_analysis_sha256")
        or receipt.get("validation_receipt_sha256")
        != state.get("task_validation_receipt_sha256")
        or receipt.get("overall_status") != analysis.get("overall_status")
    ):
        raise ShadowAcceptanceError("authoritative task sink is invalid")
    receipt_hash = receipt.get("validation_receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("validation_receipt_sha256", None)
    if receipt_hash != canonical_sha256(unsigned):
        raise ShadowAcceptanceError("task validation receipt integrity failed")
    investigations = analysis.get("investigations")
    if (
        not isinstance(investigations, list)
        or receipt.get("investigation_count") != len(investigations)
    ):
        raise ShadowAcceptanceError("task receipt investigation count changed")
    return analysis, receipt


def _verify_scenario(
    investigations: list[dict[str, Any]],
    contract: dict[str, Any],
    scenario: str,
) -> None:
    if [item.get("rule_indexes") for item in investigations] != contract[
        "rule_indexes"
    ]:
        raise ShadowAcceptanceError("rule indexes do not match the shadow scenario")
    routes = []
    for investigation in investigations:
        route = investigation.get("route")
        if not isinstance(route, dict):
            raise ShadowAcceptanceError("registered route is missing from task state")
        routes.append(
            (
                route.get("chain"),
                route.get("game_type"),
                route.get("canonical_metric"),
            )
        )
        if route.get("chain") == "install":
            alert_date = investigation.get("alert_date")
            preflight = investigation.get("root_preflight")
            try:
                expected_date = (
                    date.fromisoformat(alert_date) - timedelta(days=2)
                ).isoformat()
            except (TypeError, ValueError) as exc:
                raise ShadowAcceptanceError(
                    "install shadow alert date is invalid"
                ) from exc
            if not isinstance(preflight, dict) or preflight.get(
                "analysis_date"
            ) != expected_date:
                raise ShadowAcceptanceError(
                    "install analysis date is not alert date minus two"
                )
    if routes != contract["routes"]:
        raise ShadowAcceptanceError(
            f"investigation route order does not match scenario {scenario}"
        )


def _verify_rule_coverage(
    state: dict[str, Any],
    investigations: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> None:
    rules = state.get("normalized_alert", {}).get("rules")
    if not isinstance(rules, list):
        raise ShadowAcceptanceError("normalized task rules are invalid")
    indexes = [
        index
        for investigation in investigations
        for index in investigation.get("rule_indexes", [])
    ]
    expected = list(range(len(rules)))
    results = analysis.get("investigations")
    if (
        indexes != expected
        or len(indexes) != len(set(indexes))
        or not isinstance(results, list)
        or [item.get("rule_indexes") for item in results]
        != [item.get("rule_indexes") for item in investigations]
    ):
        raise ShadowAcceptanceError("task rule indexes are not covered once in order")


def _verify_run_state(
    state: dict[str, Any],
    *,
    run_id: str,
    route: Any,
    contracts: RepositoryContracts,
    allow_repair: bool,
    all_query_ids: set[str],
) -> dict[str, int]:
    expected_integrity = state.get("integrity_sha256")
    unsigned = dict(state)
    unsigned.pop("integrity_sha256", None)
    if (
        state.get("schema_version") != 4
        or state.get("run_id") != run_id
        or state.get("status") != "finalized"
        or state.get("analysis_profile") != "primary_v2"
        or expected_integrity != canonical_sha256(unsigned)
        or not isinstance(route, dict)
    ):
        raise ShadowAcceptanceError("attribution run identity or integrity is invalid")
    try:
        plan = contracts.select_plan(
            route.get("chain"),
            route.get("game_type"),
            route.get("canonical_metric"),
        )
    except ContractError as exc:
        raise ShadowAcceptanceError("run route is not registered") from exc
    expected_hashes = {
        "plan_contract_sha256": plan.sha256,
        "execution_plan_sha256": contracts.execution_plan_sha256,
        "query_registry_sha256": contracts.query_registry_sha256,
        "triage_sha256": contracts.triage_sha256,
        "result_schemas_sha256": contracts.result_schemas_sha256,
        "secondary_relations_sha256": contracts.secondary_relations_sha256,
        "error_code_capabilities_sha256": contracts.error_code_capabilities_sha256,
        "error_code_triggers_sha256": contracts.error_code_triggers_sha256,
        "enhancement_priority_sha256": contracts.enhancement_priority_sha256,
        "analysis_profile_sha256": contracts.analysis_profile_sha256(
            "primary_v2"
        ),
        "post_primary_plan_sha256": (
            contracts.post_primary_plan_contract_sha256("post_primary_v1")
        ),
    }
    if (
        (
            state.get("chain"),
            state.get("game_type"),
            state.get("metric"),
        )
        != (
            route.get("chain"),
            route.get("game_type"),
            route.get("canonical_metric"),
        )
        or state.get("plan_id") != plan.id
        or any(
        state.get(field) != expected for field, expected in expected_hashes.items()
        )
    ):
        raise ShadowAcceptanceError("attribution run contract hashes changed")

    steps = state.get("steps")
    if (
        not isinstance(steps, list)
        or state.get("cursor") != len(steps)
        or [item.get("id") for item in steps]
        != [item.id for item in plan.steps]
        or any(item.get("status") not in _PRIMARY_TERMINAL for item in steps)
    ):
        raise ShadowAcceptanceError("primary fixed queue is incomplete")
    primary_attempts = 0
    primary_queries = 0
    for step in steps:
        attempts = _verify_attempts(
            step,
            allow_repair=allow_repair,
            all_query_ids=all_query_ids,
            label=f"primary step {step.get('id')}",
        )
        primary_attempts += attempts
        primary_queries += int(attempts > 0)

    post_primary = state.get("post_primary")
    post_plan = contracts.post_primary_plan("post_primary_v1")
    post_ids = [item["id"] for item in post_plan["steps"]]
    post_steps = post_primary.get("steps") if isinstance(post_primary, dict) else None
    if (
        not isinstance(post_primary, dict)
        or post_primary.get("profile") != "primary_v2"
        or post_primary.get("plan_id") != "post_primary_v1"
        or post_primary.get("status") != "completed"
        or _SHA256.fullmatch(str(post_primary.get("primary_evidence_sha256")))
        is None
        or not isinstance(post_steps, list)
        or [item.get("id") for item in post_steps] != post_ids
        or any(item.get("status") not in _POST_TERMINAL for item in post_steps)
    ):
        raise ShadowAcceptanceError("post-primary fixed plan is incomplete")
    enhancement = post_primary.get("enhancement_plan")
    enhancement_id = post_plan["enhancement_priority_plan"]
    enhancement_contract = contracts.enhancement_priority_plan(enhancement_id)
    if (
        not isinstance(enhancement, dict)
        or enhancement.get("plan_id") != enhancement_id
        or enhancement.get("plan_contract_sha256")
        != contracts.enhancement_priority_plan_contract_sha256(enhancement_id)
        or enhancement.get("max_query_modules")
        != enhancement_contract["max_query_modules"]
        or enhancement.get("max_query_modules") != 2
        or not isinstance(enhancement.get("selected_modules"), list)
        or enhancement.get("query_module_count")
        != len(enhancement["selected_modules"])
        or enhancement.get("query_module_count") > 2
        or any(
            item not in {"error_code", "cross_dimension_overlap"}
            for item in enhancement["selected_modules"]
        )
    ):
        raise ShadowAcceptanceError("enhancement query-module budget changed")

    post_queries = 0
    post_attempts = 0
    for step, contract_step in zip(post_steps, post_plan["steps"], strict=True):
        step_id = step["id"]
        logical_queries = 0
        attempts = 0
        if step_id in {"counterfactual", "breadth_check"}:
            if step.get("attempts") not in (None, []):
                raise ShadowAcceptanceError(
                    f"deterministic post-primary step issued a query: {step_id}"
                )
        elif step_id == "game_background":
            items = step.get("items", [])
            if not isinstance(items, list) or len(items) > contract_step["max_queries"]:
                raise ShadowAcceptanceError("game background query cap changed")
            if items and step.get("cursor") != len(items):
                raise ShadowAcceptanceError("game background cursor is incomplete")
            for item in items:
                if item.get("status") not in _POST_TERMINAL:
                    raise ShadowAcceptanceError(
                        "game background item is not terminal"
                    )
                item_attempts = _verify_attempts(
                    item,
                    allow_repair=allow_repair,
                    all_query_ids=all_query_ids,
                    label="game background item",
                )
                attempts += item_attempts
                logical_queries += int(item_attempts > 0)
        else:
            attempts = _verify_attempts(
                step,
                allow_repair=allow_repair,
                all_query_ids=all_query_ids,
                label=f"post-primary step {step_id}",
            )
            logical_queries = int(attempts > 0)
        if logical_queries > contract_step["max_queries"]:
            raise ShadowAcceptanceError(
                f"post-primary step query cap exceeded: {step_id}"
            )
        post_queries += logical_queries
        post_attempts += attempts
    if post_queries > post_plan["max_additional_queries"] or post_queries > 6:
        raise ShadowAcceptanceError("post-primary total query cap exceeded")
    return {
        "primary_queries": primary_queries,
        "primary_attempts": primary_attempts,
        "post_primary_queries": post_queries,
        "post_primary_attempts": post_attempts,
    }


def _verify_attempts(
    owner: dict[str, Any],
    *,
    allow_repair: bool,
    all_query_ids: set[str],
    label: str,
) -> int:
    attempts = owner.get("attempts", [])
    if not isinstance(attempts, list) or len(attempts) > 3:
        raise ShadowAcceptanceError(f"{label} attempts are invalid")
    if not allow_repair and len(attempts) > 1:
        raise ShadowAcceptanceError(f"{label} used semantic repair in a release shadow")
    if attempts:
        binding = owner.get("binding")
        if (
            not isinstance(binding, dict)
            or owner.get("binding_sha256") != canonical_sha256(binding)
        ):
            raise ShadowAcceptanceError(f"{label} query binding changed")
    for index, attempt in enumerate(attempts):
        query_id = attempt.get("query_id")
        if (
            not isinstance(attempt, dict)
            or attempt.get("attempt_no") != index
            or attempt.get("status") not in {"succeeded", "failed", "error"}
            or not isinstance(query_id, str)
            or not query_id
            or _SHA256.fullmatch(str(attempt.get("sql_sha256"))) is None
        ):
            raise ShadowAcceptanceError(f"{label} attempt state is incomplete")
        if query_id in all_query_ids:
            raise ShadowAcceptanceError("query identity was replayed across steps")
        all_query_ids.add(query_id)
        if index < len(attempts) - 1 and attempt.get("status") != "error":
            raise ShadowAcceptanceError(f"{label} repair chain is inconsistent")
    return len(attempts)


def _verify_run_sink(
    sink: dict[str, Any],
    *,
    run_state: dict[str, Any],
    investigation: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    analysis = sink.get("analysis")
    receipt = sink.get("validation_receipt")
    state_receipt = run_state.get("validation_receipt")
    result = investigation.get("result")
    if (
        sink.get("run_id") != run_id
        or not isinstance(analysis, dict)
        or not isinstance(analysis.get("investigations"), list)
        or len(analysis["investigations"]) != 1
        or not isinstance(receipt, dict)
        or receipt != state_receipt
        or receipt != investigation.get("validation_receipt")
        or receipt.get("status") != "valid"
        or receipt.get("analysis_sha256") != run_state.get("final_analysis_sha256")
        or analysis["investigations"][0] != result
    ):
        raise ShadowAcceptanceError("run sink, state, and task investigation diverged")
    receipt_hash = receipt.get("validation_receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("validation_receipt_sha256", None)
    if receipt_hash != canonical_sha256(unsigned):
        raise ShadowAcceptanceError("run validation receipt integrity failed")
    return receipt


def _verify_task_receipt_summaries(
    task_receipt: dict[str, Any],
    investigations: list[dict[str, Any]],
    run_receipts: list[dict[str, Any]],
) -> None:
    summaries = task_receipt.get("investigation_receipts")
    if not isinstance(summaries, list) or len(summaries) != len(investigations):
        raise ShadowAcceptanceError("task investigation receipt summaries changed")
    expected_hashes = [
        receipt["validation_receipt_sha256"] for receipt in run_receipts
    ]
    actual_hashes = [
        summary.get("validation_receipt_sha256")
        for summary in summaries
        if isinstance(summary, dict)
        and isinstance(summary.get("validation_receipt_sha256"), str)
    ]
    if actual_hashes != expected_hashes:
        raise ShadowAcceptanceError("task receipt no longer binds the run receipts")


def _verify_snapshots(
    task_root: Path, investigations: list[dict[str, Any]]
) -> tuple[list[str], int, set[str], set[str]]:
    expected = list(
        dict.fromkeys(
            item.get("root_preflight", {}).get("root_snapshot_sha256")
            for item in investigations
            if isinstance(item.get("root_preflight"), dict)
            and isinstance(
                item["root_preflight"].get("root_snapshot_sha256"), str
            )
        )
    )
    actual_hashes: list[str] = []
    query_count = 0
    private_markers: set[str] = set()
    query_ids: set[str] = set()
    for path in sorted((task_root / "root-snapshots").glob("*.json")):
        snapshot = _load_private_json(path)
        private_markers.update(_private_values(snapshot))
        snapshot_hash = snapshot.get("snapshot_sha256")
        unsigned = dict(snapshot)
        unsigned.pop("snapshot_sha256", None)
        private_queries = snapshot.get("private_queries")
        snapshot_query_ids = (
            [item.get("query_id") for item in private_queries]
            if isinstance(private_queries, list)
            else []
        )
        if (
            snapshot_hash != canonical_sha256(unsigned)
            or not isinstance(private_queries, list)
            or len(private_queries) != 8
            or any(
                not isinstance(query_id, str) or not query_id
                for query_id in snapshot_query_ids
            )
            or len(snapshot_query_ids) != len(set(snapshot_query_ids))
            or any(query_id in query_ids for query_id in snapshot_query_ids)
        ):
            raise ShadowAcceptanceError("root snapshot integrity or coverage failed")
        query_ids.update(snapshot_query_ids)
        actual_hashes.append(snapshot_hash)
        query_count += len(private_queries)
    if len(actual_hashes) != len(expected) or set(actual_hashes) != set(expected):
        raise ShadowAcceptanceError("task state root snapshots do not match private files")
    return expected, query_count, private_markers, query_ids


def _verify_transcript(
    path: Path,
    private_markers: set[str],
    *,
    task_id: str,
    task_receipt: dict[str, Any],
    allow_repair: bool,
) -> None:
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ShadowAcceptanceError("model transcript exceeds the acceptance limit")
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ShadowAcceptanceError("model transcript cannot be loaded") from exc
    lowered = content.lower()
    if any(marker.lower() in lowered for marker in _FORBIDDEN_TEXT):
        raise ShadowAcceptanceError("model transcript contains private evidence")
    if any(marker and marker in content for marker in private_markers):
        raise ShadowAcceptanceError("model transcript contains a private evidence identity")
    if _MARKDOWN_TABLE.search(content) or (_SQL.search(content) and not allow_repair):
        raise ShadowAcceptanceError("model transcript contains SQL or a raw table")

    parsed_values = []
    for line in content.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed_values.append(value)
    found_task_complete = False
    for value in parsed_values:
        for item, action_context in _walk_dicts(value):
            own_action = item.get("action")
            repair_context = action_context == "repair_required" and allow_repair
            _verify_public_hash_keys(item, allow_private=repair_context)
            if "validation_receipt" in item and own_action != "task_complete":
                raise ShadowAcceptanceError(
                    "transcript exposes a validation receipt outside task_complete"
                )
            if "pipeline_handoff" in item and own_action != "task_complete":
                raise ShadowAcceptanceError(
                    "transcript exposes a handoff outside task_complete"
                )
            if action_context == "repair_required":
                if not allow_repair:
                    raise ShadowAcceptanceError(
                        "release transcript contains an unexpected repair packet"
                    )
                continue
            if own_action != "task_complete":
                _verify_direct_text_fields(item, allow_sql=False)
                continue
            found_task_complete = True
            _verify_task_complete_transcript(
                item,
                task_id=task_id,
                task_receipt=task_receipt,
            )
    if not found_task_complete:
        raise ShadowAcceptanceError("transcript lacks the current task_complete")


def _walk_dicts(value: Any, inherited_action: str | None = None):
    if isinstance(value, dict):
        action = value.get("action", inherited_action)
        if not isinstance(action, str):
            action = inherited_action
        yield value, action
        for child in value.values():
            yield from _walk_dicts(child, action)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child, inherited_action)
    elif isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _walk_dicts(decoded, inherited_action)


def _verify_public_hash_keys(
    value: dict[str, Any], *, allow_private: bool
) -> None:
    if allow_private:
        return
    allowed = {
        "analysis_sha256",
        "payload_sha256",
        "analysis_preview_sha256",
        "validation_receipt_sha256",
    }
    private = [key for key in value if key.endswith("_sha256") and key not in allowed]
    if private:
        raise ShadowAcceptanceError("transcript contains a private hash field")


def _verify_direct_text_fields(value: dict[str, Any], *, allow_sql: bool) -> None:
    if allow_sql:
        return
    for child in value.values():
        if not isinstance(child, str):
            continue
        if child[:1] in {"{", "["}:
            try:
                json.loads(child)
            except json.JSONDecodeError:
                pass
            else:
                continue
        if _SQL.search(child):
            raise ShadowAcceptanceError("SQL appears outside a bounded repair packet")


def _verify_task_complete_transcript(
    item: dict[str, Any],
    *,
    task_id: str,
    task_receipt: dict[str, Any],
) -> None:
    if item.get("task_id") != task_id:
        raise ShadowAcceptanceError("transcript task_complete identity changed")
    compact_receipt = item.get("validation_receipt")
    allowed_receipt_keys = {
        "status",
        "overall_status",
        "investigation_count",
        "successful_investigation_count",
        "analysis_sha256",
        "validation_receipt_sha256",
    }
    expected_compact = {
        key: task_receipt[key]
        for key in allowed_receipt_keys
        if key in task_receipt
    }
    if (
        not isinstance(compact_receipt, dict)
        or compact_receipt != expected_compact
    ):
        raise ShadowAcceptanceError(
            "transcript contains a non-compact validation receipt"
        )
    handoff = item.get("pipeline_handoff")
    allowed_handoff_keys = {
        "schema_version",
        "provider",
        "task_id",
        "payload_sha256",
        "analysis_preview_sha256",
        "validation_receipt_sha256",
        "signing_key_id",
        "signature",
    }
    preview = item.get("analysis_preview")
    if (
        not isinstance(handoff, dict)
        or set(handoff) != allowed_handoff_keys
        or handoff.get("schema_version") != 1
        or handoff.get("provider") != "xuanji-mini"
        or handoff.get("task_id") != task_id
        or handoff.get("payload_sha256") != task_receipt.get("payload_sha256")
        or not isinstance(preview, dict)
        or handoff.get("analysis_preview_sha256")
        != canonical_sha256(preview)
        or handoff.get("validation_receipt_sha256")
        != task_receipt.get("validation_receipt_sha256")
        or not isinstance(handoff.get("signing_key_id"), str)
        or not handoff["signing_key_id"]
        or not isinstance(handoff.get("signature"), str)
        or not handoff["signature"]
    ):
        raise ShadowAcceptanceError("transcript pipeline handoff contract changed")


def _private_values(value: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _PRIVATE_VALUE_KEYS and isinstance(child, str) and child:
                values.add(child)
            values.update(_private_values(child))
    elif isinstance(value, list):
        for child in value:
            values.update(_private_values(child))
    return values


def _load_private_json(path: Path) -> dict[str, Any]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ShadowAcceptanceError("private Host artifact permissions are too broad")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowAcceptanceError("private Host artifact cannot be loaded") from exc
    if not isinstance(value, dict):
        raise ShadowAcceptanceError("private Host artifact must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify one completed primary_v2 shadow task."
    )
    parser.add_argument("--data-root", type=Path, default=Path("/var/lib/xuanji"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--scenario", choices=sorted(_SCENARIOS), required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument(
        "--allow-repair",
        action="store_true",
        help="Accept the bounded synthetic repair fixture; release shadows omit this.",
    )
    args = parser.parse_args(argv)
    try:
        result = verify_shadow(
            data_root=args.data_root,
            task_id=args.task_id,
            scenario=args.scenario,
            transcript_path=args.transcript,
            allow_repair=args.allow_repair,
        )
    except ShadowAcceptanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
