from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    RepositoryContracts,
    canonical_sha256,
    sha256_text,
)
from .models import RunStatus, StepStatus, TERMINAL_STEP_STATUSES
from .query_builder import QueryBuildError, QueryBuilder


ROOT = Path(__file__).resolve().parents[1]
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RunnerError(ValueError):
    pass


class AttributionRunner:
    def __init__(
        self,
        root: Path | str = ROOT,
        runs_root: Path | str | None = None,
    ):
        self.root = Path(root).resolve()
        self.contracts = RepositoryContracts(self.root)
        self.query_builder = QueryBuilder(self.contracts)
        configured_runs_root = os.environ.get("XUANJI_RUNS_ROOT")
        if runs_root is not None:
            self.runs_root = Path(runs_root).resolve()
        elif configured_runs_root:
            self.runs_root = Path(configured_runs_root).resolve()
        else:
            self.runs_root = self.root / ".runs"

    def verify_assets(self) -> dict[str, Any]:
        result = self.contracts.verify_assets()
        return {"status": "ok", **result}

    def init_run(
        self,
        *,
        run_id: str,
        chain: str,
        game_type: str,
        metric: str,
        alert_date: str,
        analysis_date: str,
        resume: bool = False,
    ) -> dict[str, Any]:
        self._validate_run_id(run_id)
        self._validate_date(alert_date, "alert_date")
        self._validate_date(analysis_date, "analysis_date")
        self.contracts.verify_assets()
        plan = self.contracts.select_plan(chain, game_type, metric)
        run_dir = self._run_dir(run_id)
        if run_dir.exists():
            if not resume:
                raise RunnerError(f"run already exists: {run_id}")
            state = self._load_state(run_id)
            expected = {
                "chain": chain,
                "game_type": game_type,
                "metric": metric,
                "alert_date": alert_date,
                "analysis_date": analysis_date,
                "plan_id": plan.id,
                "plan_contract_sha256": plan.sha256,
            }
            mismatches = {
                key: {"expected": value, "actual": state.get(key)}
                for key, value in expected.items()
                if state.get(key) != value
            }
            if mismatches:
                raise RunnerError(
                    f"resume arguments do not match immutable run state: {mismatches}"
                )
            return {"resumed": True, **self._status_payload(state)}

        steps = []
        for plan_step in plan.steps:
            binding = self.contracts.binding_for(
                plan, plan_step.id, metric, game_type
            )
            steps.append(
                {
                    "id": plan_step.id,
                    "kind": plan_step.kind,
                    "produces_candidates": plan_step.produces_candidates,
                    "failure_scope": plan_step.failure_scope,
                    "automatic_status": plan_step.automatic_status,
                    "automatic_reason": plan_step.automatic_reason,
                    "status": StepStatus.PENDING.value,
                    "query_asset_path": binding.asset_path if binding else None,
                    "query_asset_sha256": binding.asset_sha256 if binding else None,
                    "attempts": [],
                    "candidate_count": None,
                    "reason": None,
                    "warning_codes": [],
                }
            )

        state = {
            "schema_version": 1,
            "run_id": run_id,
            "revision": 0,
            "status": RunStatus.ACTIVE.value,
            "execution_mode": "task_ticket",
            "plan_id": plan.id,
            "plan_contract_sha256": plan.sha256,
            "chain": chain,
            "game_type": game_type,
            "metric": metric,
            "alert_date": alert_date,
            "analysis_date": analysis_date,
            "cursor": 0,
            "ready_for_final_validation": False,
            "steps": steps,
            "processed_events": {},
        }
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:  # pragma: no cover - race protection
            raise RunnerError(f"run already exists: {run_id}") from exc
        for name in ("tickets", "sql", "events"):
            (run_dir / name).mkdir()
        self._write_state(state)
        return {"resumed": False, **self._status_payload(state)}

    def next_action(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        if state["cursor"] >= len(state["steps"]):
            return self._queue_complete_ticket(state)

        changed = False
        while state["cursor"] < len(state["steps"]):
            step = state["steps"][state["cursor"]]
            if step["automatic_status"] is None:
                break
            if step["status"] != StepStatus.PENDING.value:
                raise RunnerError("automatic step is not pending at the current cursor")
            step["status"] = step["automatic_status"]
            step["reason"] = step["automatic_reason"]
            state["cursor"] += 1
            changed = True

        if state["cursor"] >= len(state["steps"]):
            self._mark_queue_complete(state)
            state["revision"] += 1
            self._write_state(state)
            return self._queue_complete_ticket(state)

        step = state["steps"][state["cursor"]]
        if step["status"] == StepStatus.PENDING.value:
            plan = self.contracts.plans[state["plan_id"]]
            binding = self.contracts.binding_for(
                plan, step["id"], state["metric"], state["game_type"]
            )
            if binding is None:
                raise RunnerError(f"current step has no query binding: {step['id']}")
            built = self.query_builder.build(
                binding,
                {
                    "business_date": state["analysis_date"],
                    "game_type": state["game_type"],
                },
            )
            attempt_no = 0
            sql_path = self._sql_relative_path(state["cursor"], step["id"], attempt_no)
            self._atomic_write_text(self._run_dir(run_id) / sql_path, built.sql)
            step["attempts"].append(
                {
                    "attempt_no": attempt_no,
                    "status": "issued",
                    "sql_sha256": built.sha256,
                    "sql_path": sql_path,
                    "query_id": None,
                    "error": None,
                    "event_path": None,
                }
            )
            step["status"] = StepStatus.IN_PROGRESS.value
            changed = True

        if step["status"] != StepStatus.IN_PROGRESS.value:
            raise RunnerError(
                f"current step cannot issue an execution ticket: {step['status']}"
            )
        if changed:
            state["revision"] += 1
            self._write_state(state)
        return self._execution_ticket(state, step)

    def record(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise RunnerError("record event must be a JSON object")
        state = self._load_state(run_id)
        event_hash = canonical_sha256(event)
        if event_hash in state["processed_events"]:
            return {
                "idempotent_replay": True,
                "event_sha256": event_hash,
                **self._status_payload(state),
            }
        if state["cursor"] >= len(state["steps"]):
            raise RunnerError("queue is complete; no further events are accepted")
        step = state["steps"][state["cursor"]]
        if event.get("step_id") != step["id"]:
            raise RunnerError(
                f"event targets non-current step {event.get('step_id')}; "
                f"current step is {step['id']}"
            )
        if step["status"] != StepStatus.IN_PROGRESS.value:
            raise RunnerError(f"current step is not awaiting a query result: {step['status']}")
        if not step["attempts"]:
            raise RunnerError("current step has no issued attempt")
        attempt = step["attempts"][-1]
        attempt_no = event.get("attempt_no")
        if isinstance(attempt_no, bool) or attempt_no != attempt["attempt_no"]:
            raise RunnerError(
                f"event attempt_no must equal current attempt {attempt['attempt_no']}"
            )

        event_type = event.get("event")
        if event_type not in {
            "query_succeeded",
            "query_error",
            "step_validation_failed",
        }:
            raise RunnerError(f"unsupported record event: {event_type}")
        if event_type in {"query_succeeded", "query_error"}:
            self._require_submitted_hash(event, attempt)

        event_path = Path("events") / f"{event_hash}.json"
        self._atomic_write_json(self._run_dir(run_id) / event_path, event)
        attempt["event_path"] = str(event_path)
        query_id = self._optional_non_empty_string(event, "query_id")
        attempt["query_id"] = query_id

        if event_type == "query_succeeded":
            candidate_count = event.get("candidate_count")
            if (
                isinstance(candidate_count, bool)
                or not isinstance(candidate_count, int)
                or candidate_count < 0
            ):
                raise RunnerError("candidate_count must be a non-negative integer")
            if not step["produces_candidates"] and candidate_count != 0:
                raise RunnerError("diagnostic steps cannot produce candidates")
            warning_codes = self._warning_codes(event)
            attempt["status"] = "succeeded"
            step["status"] = StepStatus.SUCCEEDED.value
            step["candidate_count"] = candidate_count
            step["warning_codes"] = warning_codes
        elif event_type == "step_validation_failed":
            reason = self._required_non_empty_string(event, "reason")
            warning_codes = self._warning_codes(event)
            attempt["status"] = "failed"
            step["status"] = StepStatus.FAILED.value
            step["reason"] = reason
            step["warning_codes"] = warning_codes
        else:
            raw_error = {
                "class": self._required_non_empty_string(event, "error_class"),
                "code": self._required_non_empty_string(event, "error_code"),
                "message": self._required_non_empty_string(event, "error_message"),
            }
            attempt["status"] = "error"
            attempt["error"] = raw_error
            step["status"] = StepStatus.FAILED.value
            step["reason"] = (
                f"{raw_error['class']} {raw_error['code']}: {raw_error['message']}"
            )

        state["processed_events"][event_hash] = {
            "event": event_type,
            "step_id": step["id"],
            "event_path": str(event_path),
        }
        state["cursor"] += 1
        if state["cursor"] >= len(state["steps"]):
            self._mark_queue_complete(state)
        state["revision"] += 1
        self._write_state(state)
        return {
            "idempotent_replay": False,
            "event_sha256": event_hash,
            **self._status_payload(state),
        }

    def status(self, run_id: str) -> dict[str, Any]:
        return self._status_payload(self._load_state(run_id))

    def export(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        if state["cursor"] != len(state["steps"]):
            raise RunnerError("cannot export before every fixed queue step is terminal")
        for step in state["steps"]:
            if step["status"] not in TERMINAL_STEP_STATUSES:
                raise RunnerError(
                    f"cannot export non-terminal step {step['id']}: {step['status']}"
                )
        if state["status"] == RunStatus.QUEUE_COMPLETE.value:
            state["status"] = RunStatus.FINALIZED.value
            state["revision"] += 1
            self._write_state(state)

        exported_steps = []
        for step in state["steps"]:
            exported = {"step": step["id"], "status": step["status"]}
            if step["status"] == StepStatus.SUCCEEDED.value:
                exported["candidate_count"] = step["candidate_count"]
            else:
                exported["reason"] = step["reason"]
            query_id = self._last_query_id(step)
            if query_id:
                exported["query_id"] = query_id
            if step["warning_codes"]:
                exported["warning_codes"] = list(step["warning_codes"])
            exported_steps.append(exported)
        return {
            "mode": "full_queue",
            "chain": state["chain"],
            "game_type": state["game_type"],
            "steps": exported_steps,
        }

    def load_state(self, run_id: str) -> dict[str, Any]:
        return self._load_state(run_id)

    def _execution_ticket(
        self, state: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = step["attempts"][-1]
        sql = (self._run_dir(state["run_id"]) / attempt["sql_path"]).read_text(
            encoding="utf-8"
        )
        if sha256_text(sql) != attempt["sql_sha256"]:
            raise RunnerError("stored SQL no longer matches its issued hash")
        ticket = {
            "action": "execute_query",
            "run_id": state["run_id"],
            "revision": state["revision"],
            "step_id": step["id"],
            "attempt_no": attempt["attempt_no"],
            "query_asset_path": step["query_asset_path"],
            "query_asset_sha256": step["query_asset_sha256"],
            "rendered_sql_sha256": attempt["sql_sha256"],
            "parameters": {
                "business_date": state["analysis_date"],
                "game_type": state["game_type"],
            },
            "rendered_sql": sql,
            "allowed_outcomes": [
                "query_succeeded",
                "query_error",
                "step_validation_failed",
            ],
        }
        self._atomic_write_json(
            self._run_dir(state["run_id"])
            / "tickets"
            / f"{state['cursor']:02d}-{step['id']}-attempt-{attempt['attempt_no']}.json",
            ticket,
        )
        return ticket

    def _queue_complete_ticket(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "queue_complete",
            "run_id": state["run_id"],
            "revision": state["revision"],
            "ready_for_final_validation": True,
        }

    def _status_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        cursor = state["cursor"]
        current = state["steps"][cursor] if cursor < len(state["steps"]) else None
        repair_count = sum(
            1
            for step in state["steps"]
            for attempt in step["attempts"]
            if attempt.get("repair") is not None
        )
        completed_candidates = sum(
            step["candidate_count"]
            for step in state["steps"]
            if step["status"] == StepStatus.SUCCEEDED.value
            and step["produces_candidates"]
        )
        return {
            "run_id": state["run_id"],
            "revision": state["revision"],
            "status": state["status"],
            "cursor": cursor,
            "current_step": (
                {"id": current["id"], "status": current["status"]}
                if current
                else None
            ),
            "steps": [
                {"step": step["id"], "status": step["status"]}
                for step in state["steps"]
            ],
            "repair_count": repair_count,
            "completed_candidate_count": completed_candidates,
            "remaining_steps": [step["id"] for step in state["steps"][cursor:]],
            "ready_for_final_validation": state["ready_for_final_validation"],
            "blocking_reason": (
                "current step requires a SQL repair"
                if current and current["status"] == StepStatus.REPAIR_REQUIRED.value
                else None
            ),
        }

    def _load_state(self, run_id: str) -> dict[str, Any]:
        self._validate_run_id(run_id)
        self.contracts.verify_assets()
        path = self._state_path(run_id)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RunnerError(f"run does not exist: {run_id}") from exc
        except json.JSONDecodeError as exc:
            raise RunnerError(f"state.json is not valid JSON for run {run_id}") from exc
        if not isinstance(state, dict):
            raise RunnerError("state.json must contain an object")
        expected_integrity = state.get("integrity_sha256")
        state_without_integrity = dict(state)
        state_without_integrity.pop("integrity_sha256", None)
        if expected_integrity != canonical_sha256(state_without_integrity):
            raise RunnerError("state.json integrity check failed")
        self._validate_state_contract(state)
        return state

    def _validate_state_contract(self, state: dict[str, Any]) -> None:
        if state.get("schema_version") != 1:
            raise RunnerError("unsupported state schema version")
        self._validate_run_id(state.get("run_id"))
        plan = self.contracts.select_plan(
            state.get("chain"), state.get("game_type"), state.get("metric")
        )
        if state.get("plan_id") != plan.id:
            raise RunnerError("state plan_id does not match immutable inputs")
        if state.get("plan_contract_sha256") != plan.sha256:
            raise RunnerError("state plan hash no longer matches the contract")
        steps = state.get("steps")
        if not isinstance(steps, list) or len(steps) != len(plan.steps):
            raise RunnerError("state step list length does not match the fixed plan")
        cursor = state.get("cursor")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or not (
            0 <= cursor <= len(steps)
        ):
            raise RunnerError("state cursor is invalid")

        for index, (step, plan_step) in enumerate(zip(steps, plan.steps, strict=True)):
            if not isinstance(step, dict) or step.get("id") != plan_step.id:
                raise RunnerError("state step order does not match the fixed plan")
            expected_properties = {
                "kind": plan_step.kind,
                "produces_candidates": plan_step.produces_candidates,
                "failure_scope": plan_step.failure_scope,
                "automatic_status": plan_step.automatic_status,
                "automatic_reason": plan_step.automatic_reason,
            }
            if any(step.get(key) != value for key, value in expected_properties.items()):
                raise RunnerError(f"state step contract changed: {plan_step.id}")
            binding = self.contracts.binding_for(
                plan, plan_step.id, state["metric"], state["game_type"]
            )
            expected_asset_path = binding.asset_path if binding else None
            expected_asset_hash = binding.asset_sha256 if binding else None
            if step.get("query_asset_path") != expected_asset_path or step.get(
                "query_asset_sha256"
            ) != expected_asset_hash:
                raise RunnerError(f"state query asset changed: {plan_step.id}")
            status = step.get("status")
            if status not in {member.value for member in StepStatus}:
                raise RunnerError(f"invalid step status: {status}")
            if index < cursor and status not in TERMINAL_STEP_STATUSES:
                raise RunnerError("a completed cursor prefix contains non-terminal steps")
            if index == cursor and cursor < len(steps) and status in TERMINAL_STEP_STATUSES:
                raise RunnerError("cursor points at a terminal step")
            if index > cursor and status != StepStatus.PENDING.value:
                raise RunnerError("a future queue step is not pending")
            candidate_count = step.get("candidate_count")
            if status == StepStatus.SUCCEEDED.value:
                if (
                    isinstance(candidate_count, bool)
                    or not isinstance(candidate_count, int)
                    or candidate_count < 0
                ):
                    raise RunnerError("succeeded step has invalid candidate_count")
                if not plan_step.produces_candidates and candidate_count != 0:
                    raise RunnerError("diagnostic step has candidates")
            elif candidate_count is not None:
                raise RunnerError("non-succeeded step cannot have candidate_count")
            if status in {
                StepStatus.FAILED.value,
                StepStatus.SKIPPED_NOT_APPLICABLE.value,
            } and not isinstance(step.get("reason"), str):
                raise RunnerError("failed/skipped step requires a reason")
            if status == StepStatus.SKIPPED_NOT_APPLICABLE.value and not (
                plan.id == "install_sandbox" and plan_step.id == "install_stage"
            ):
                raise RunnerError("illegal skipped_not_applicable step")

        if cursor < len(steps):
            if state.get("status") != RunStatus.ACTIVE.value:
                raise RunnerError("incomplete queue must remain active")
            if state.get("ready_for_final_validation") is not False:
                raise RunnerError("incomplete queue cannot be ready for final validation")
        else:
            if state.get("status") not in {
                RunStatus.QUEUE_COMPLETE.value,
                RunStatus.FINALIZED.value,
            }:
                raise RunnerError("complete queue has an invalid run status")
            if state.get("ready_for_final_validation") is not True:
                raise RunnerError("complete queue must be ready for final validation")

    def _write_state(self, state: dict[str, Any]) -> None:
        state.pop("integrity_sha256", None)
        state["integrity_sha256"] = canonical_sha256(state)
        self._atomic_write_json(self._state_path(state["run_id"]), state)

    def _mark_queue_complete(self, state: dict[str, Any]) -> None:
        state["status"] = RunStatus.QUEUE_COMPLETE.value
        state["ready_for_final_validation"] = True

    def _require_submitted_hash(
        self, event: dict[str, Any], attempt: dict[str, Any]
    ) -> None:
        submitted_hash = self._required_non_empty_string(
            event, "submitted_sql_sha256"
        )
        if submitted_hash != attempt["sql_sha256"]:
            raise RunnerError("submitted SQL hash does not match the issued ticket")

    def _warning_codes(self, event: dict[str, Any]) -> list[str]:
        warning_codes = event.get("warning_codes", [])
        if not isinstance(warning_codes, list) or any(
            not isinstance(code, str) or not code.strip() for code in warning_codes
        ):
            raise RunnerError("warning_codes must be an array of non-empty strings")
        return list(dict.fromkeys(warning_codes))

    def _required_non_empty_string(self, value: dict[str, Any], key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result.strip():
            raise RunnerError(f"{key} must be a non-empty string")
        return result

    def _optional_non_empty_string(
        self, value: dict[str, Any], key: str
    ) -> str | None:
        if key not in value or value[key] is None:
            return None
        return self._required_non_empty_string(value, key)

    def _last_query_id(self, step: dict[str, Any]) -> str | None:
        for attempt in reversed(step["attempts"]):
            if attempt.get("query_id"):
                return attempt["query_id"]
        return None

    def _sql_relative_path(self, cursor: int, step_id: str, attempt_no: int) -> str:
        return str(Path("sql") / f"{cursor:02d}-{step_id}-attempt-{attempt_no}.sql")

    def _run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self.runs_root / run_id

    def _state_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "state.json"

    def _validate_run_id(self, run_id: Any) -> None:
        if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
            raise RunnerError(
                "run_id must contain only letters, digits, dots, underscores, or hyphens"
            )

    def _validate_date(self, value: str, field: str) -> None:
        from datetime import date

        if not isinstance(value, str):
            raise RunnerError(f"{field} must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise RunnerError(f"{field} must use YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise RunnerError(f"{field} must use YYYY-MM-DD")

    def _atomic_write_json(self, path: Path, value: Any) -> None:
        content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._atomic_write_text(path, content)

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise


def _read_json_input(path_value: str) -> dict[str, Any]:
    if path_value == "-":
        content = sys.stdin.read()
    else:
        content = Path(path_value).read_text(encoding="utf-8")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"record input is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError("record input must contain a JSON object")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m runtime.runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("verify-assets")

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--chain", required=True, choices=("download", "install"))
    init_parser.add_argument("--game-type", required=True, choices=("app", "sandbox"))
    init_parser.add_argument("--metric", required=True)
    init_parser.add_argument("--alert-date", required=True)
    init_parser.add_argument("--analysis-date", required=True)
    init_parser.add_argument("--resume", action="store_true")

    for command in ("next", "status", "export"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--run-id", required=True)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--run-id", required=True)
    record_parser.add_argument("--event-file", default="-")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        runner = AttributionRunner()
        if args.command == "verify-assets":
            result = runner.verify_assets()
        elif args.command == "init":
            result = runner.init_run(
                run_id=args.run_id,
                chain=args.chain,
                game_type=args.game_type,
                metric=args.metric,
                alert_date=args.alert_date,
                analysis_date=args.analysis_date,
                resume=args.resume,
            )
        elif args.command == "next":
            result = runner.next_action(args.run_id)
        elif args.command == "record":
            result = runner.record(args.run_id, _read_json_input(args.event_file))
        elif args.command == "status":
            result = runner.status(args.run_id)
        elif args.command == "export":
            result = runner.export(args.run_id)
        else:  # pragma: no cover - argparse prevents this branch
            raise RunnerError(f"unknown command: {args.command}")
    except (ContractError, QueryBuildError, RunnerError, OSError) as exc:
        print(
            json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
