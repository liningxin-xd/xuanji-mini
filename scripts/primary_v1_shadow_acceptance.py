from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from runtime.contracts import RepositoryContracts, canonical_sha256
from runtime.task_assembler import writer_pack_size


_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ROOT = Path(__file__).resolve().parents[1]
_SQL = re.compile(r"(?is)(\bselect\s+.+?\bfrom\b|\bwith\s+\w+\s+as\s*\()")
_MARKDOWN_TABLE = re.compile(r"(?m)^\s*\|.+\|\s*$\n^\s*\|\s*:?-{3,}")
_FORBIDDEN_TEXT = (
    '"query_id"',
    '"raw_result"',
    '"raw_result_sha256"',
    '"receipt_signature"',
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
_EXPECTED_SCENARIOS = {
    "same-metric": {
        "investigation_count": 1,
        "rule_indexes": [[0, 1, 2]],
        "routes": [("download", "app", "下载完成率")],
        "root_snapshot_count": 1,
    },
    "same-scope": {
        "investigation_count": 3,
        "rule_indexes": [[0], [1], [2]],
        "routes": [
            ("download", "app", "下载完成率"),
            ("download", "app", "下载失败率"),
            ("download", "app", "下载人为停止率"),
        ],
        "root_snapshot_count": 1,
    },
    "mixed-scope": {
        "investigation_count": 3,
        "rule_indexes": [[0], [1], [2]],
        "routes": [
            ("download", "app", "下载完成率"),
            ("install", "app", "下载安装完成率"),
            ("download", "sandbox", "下载完成率"),
        ],
        "root_snapshot_count": 2,
    },
}
_TASK_RESULT_FIELDS = {
    "schema_version",
    "task_id",
    "analysis",
    "validation_receipt",
}


class ShadowAcceptanceError(ValueError):
    pass


def verify_shadow(
    *,
    data_root: Path | str,
    task_id: str,
    scenario: str,
    transcript_path: Path | str,
) -> dict[str, Any]:
    if _TASK_ID.fullmatch(task_id) is None:
        raise ShadowAcceptanceError("task_id is invalid")
    contract = _EXPECTED_SCENARIOS.get(scenario)
    if contract is None:
        raise ShadowAcceptanceError("shadow scenario is invalid")
    root = Path(data_root).resolve()
    task_root = root / "tasks" / task_id
    state = _load_private_json(task_root / "state.json")
    sink = _load_private_json(
        root / "results" / "tasks" / task_id / "validated-task-result.json"
    )
    _verify_state(state, task_id)
    analysis, receipt = _verify_sink(sink, task_id)
    investigations = state.get("investigations")
    if not isinstance(investigations, list):
        raise ShadowAcceptanceError("task state investigations are invalid")
    _verify_scenario(investigations, contract, scenario)
    _verify_analysis_order(analysis, investigations)

    (
        snapshot_hashes,
        root_query_count,
        private_markers,
    ) = _verify_snapshots(task_root, investigations)
    if len(snapshot_hashes) != contract["root_snapshot_count"]:
        raise ShadowAcceptanceError("root snapshot scope count does not match scenario")
    if receipt.get("root_snapshot_sha256s") != snapshot_hashes:
        raise ShadowAcceptanceError("task receipt root snapshots changed")

    attribution_query_count = 0
    writer_pack_sizes: list[int] = []
    contracts = RepositoryContracts(_ROOT)
    private_markers.update(snapshot_hashes)
    for investigation in investigations:
        run_id = investigation.get("run_id")
        if not isinstance(run_id, str):
            raise ShadowAcceptanceError("shadow investigation lacks a full-queue run")
        run_root = root / "runs" / run_id
        run_state = _load_private_json(run_root / "state.json")
        private_markers.update(_private_values(run_state))
        _verify_run_state(run_state, run_id)
        steps = run_state.get("steps")
        route = investigation["route"]
        plan = contracts.select_plan(
            route["chain"], route["game_type"], route["canonical_metric"]
        )
        if (
            not isinstance(steps, list)
            or run_state.get("cursor") != len(steps)
            or [item.get("id") for item in steps]
            != [item.id for item in plan.steps]
            or any(
                item.get("status")
                not in {"succeeded", "failed", "skipped_not_applicable"}
                for item in steps
            )
        ):
            raise ShadowAcceptanceError("attribution fixed queue is incomplete")
        query_ids = {
            attempt["query_id"]
            for step in steps
            for attempt in step.get("attempts", [])
            if isinstance(attempt.get("query_id"), str) and attempt["query_id"]
        }
        attribution_query_count += len(query_ids)
        private_markers.update(query_ids)
        writer_pack = _load_private_json(run_root / "exports" / "writer-pack.json")
        size = writer_pack_size(writer_pack)
        if size > 12 * 1024:
            raise ShadowAcceptanceError("writer pack exceeds the 12 KB context budget")
        writer_pack_sizes.append(size)

    _verify_transcript(Path(transcript_path), private_markers)
    return {
        "status": "passed",
        "scenario": scenario,
        "task_id": task_id,
        "investigation_count": len(investigations),
        "root_query_count": root_query_count,
        "attribution_query_count": attribution_query_count,
        "root_snapshot_count": len(snapshot_hashes),
        "writer_pack_max_bytes": max(writer_pack_sizes, default=0),
        "overall_status": analysis.get("overall_status"),
        "idempotency": {
            "task_id": task_id,
            "analysis_sha256": receipt["analysis_sha256"],
            "validation_receipt_sha256": receipt[
                "validation_receipt_sha256"
            ],
        },
    }


def _verify_state(state: dict[str, Any], task_id: str) -> None:
    expected = state.get("integrity_sha256")
    unsigned = dict(state)
    unsigned.pop("integrity_sha256", None)
    if (
        state.get("task_id") != task_id
        or state.get("status") != "completed"
        or expected != canonical_sha256(unsigned)
    ):
        raise ShadowAcceptanceError("task state identity or integrity is invalid")


def _verify_sink(
    sink: dict[str, Any], task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = sink.get("analysis")
    receipt = sink.get("validation_receipt")
    if (
        set(sink) != _TASK_RESULT_FIELDS
        or isinstance(sink.get("schema_version"), bool)
        or sink.get("schema_version") != 1
        or sink.get("task_id") != task_id
        or not isinstance(analysis, dict)
        or not isinstance(receipt, dict)
        or receipt.get("status") != "valid"
        or receipt.get("task_id") != task_id
        or receipt.get("analysis_sha256") != canonical_sha256(analysis)
    ):
        raise ShadowAcceptanceError("authoritative task sink is invalid")
    receipt_hash = receipt.get("validation_receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("validation_receipt_sha256", None)
    if receipt_hash != canonical_sha256(unsigned):
        raise ShadowAcceptanceError("task validation receipt integrity failed")
    return analysis, receipt


def _verify_run_state(state: dict[str, Any], run_id: str) -> None:
    expected = state.get("integrity_sha256")
    unsigned = dict(state)
    unsigned.pop("integrity_sha256", None)
    if (
        state.get("schema_version") != 4
        or state.get("run_id") != run_id
        or state.get("status") != "finalized"
        or expected != canonical_sha256(unsigned)
    ):
        raise ShadowAcceptanceError("attribution run identity or integrity is invalid")


def _verify_scenario(
    investigations: list[dict[str, Any]],
    contract: dict[str, Any],
    scenario: str,
) -> None:
    if len(investigations) != contract["investigation_count"]:
        raise ShadowAcceptanceError("investigation count does not match scenario")
    if [item.get("rule_indexes") for item in investigations] != contract["rule_indexes"]:
        raise ShadowAcceptanceError("rule indexes are not grouped exactly once")
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
    if routes != contract["routes"]:
        raise ShadowAcceptanceError("investigation route order does not match scenario")
    if scenario == "mixed-scope":
        install = investigations[1]
        preflight = install.get("root_preflight")
        if not isinstance(preflight, dict):
            raise ShadowAcceptanceError("install investigation lacks root preflight")
        expected = (
            date.fromisoformat(install["alert_date"]) - timedelta(days=2)
        ).isoformat()
        if preflight.get("analysis_date") != expected:
            raise ShadowAcceptanceError("install analysis date is not alert date minus two")


def _verify_analysis_order(
    analysis: dict[str, Any], investigations: list[dict[str, Any]]
) -> None:
    results = analysis.get("investigations")
    expected = [item.get("rule_indexes") for item in investigations]
    if (
        not isinstance(results, list)
        or [item.get("rule_indexes") for item in results] != expected
    ):
        raise ShadowAcceptanceError("task sink investigation order changed")


def _verify_snapshots(
    task_root: Path, investigations: list[dict[str, Any]]
) -> tuple[list[str], int, set[str]]:
    expected = list(
        dict.fromkeys(
            item.get("root_preflight", {}).get("root_snapshot_sha256")
            for item in investigations
        )
    )
    if any(not isinstance(value, str) for value in expected):
        raise ShadowAcceptanceError("investigation root snapshot identity is invalid")
    actual_hashes: list[str] = []
    query_count = 0
    private_markers: set[str] = set()
    for path in sorted((task_root / "root-snapshots").glob("*.json")):
        snapshot = _load_private_json(path)
        private_markers.update(_private_values(snapshot))
        snapshot_hash = snapshot.get("snapshot_sha256")
        unsigned = dict(snapshot)
        unsigned.pop("snapshot_sha256", None)
        private_queries = snapshot.get("private_queries")
        if (
            snapshot_hash != canonical_sha256(unsigned)
            or not isinstance(private_queries, list)
            or len(private_queries) != 8
        ):
            raise ShadowAcceptanceError("root snapshot integrity or coverage failed")
        actual_hashes.append(snapshot_hash)
        query_count += len(private_queries)
    if len(actual_hashes) != len(expected) or set(actual_hashes) != set(expected):
        raise ShadowAcceptanceError("task state root snapshots do not match private files")
    return expected, query_count, private_markers


def _verify_transcript(path: Path, private_markers: set[str]) -> None:
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
    if _SQL.search(content) or _MARKDOWN_TABLE.search(content):
        raise ShadowAcceptanceError("model transcript contains SQL or a raw table")


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
        description="Verify one completed primary_v1 production shadow task."
    )
    parser.add_argument("--data-root", type=Path, default=Path("/var/lib/xuanji"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--scenario", choices=sorted(_EXPECTED_SCENARIOS), required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_shadow(
            data_root=args.data_root,
            task_id=args.task_id,
            scenario=args.scenario,
            transcript_path=args.transcript,
        )
    except ShadowAcceptanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
