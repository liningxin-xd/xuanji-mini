from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from .alert_normalizer import AlertNormalizer
from .contracts import RepositoryContracts, canonical_sha256, sha256_bytes
from .root_preflight import RootPreflight, RootPreflightError
from .route_resolver import DqcRouteRegistry, RouteResolver
from .task_assembler import TaskAssembler, writer_pack_size


_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PRIVATE_FIELDS = {
    "query_id",
    "receipt_id",
    "raw_result",
    "raw_result_sha256",
    "receipt_signature",
    "rendered_sql",
    "rendered_sql_sha256",
    "submitted_sql_sha256",
    "private_queries",
    "root_snapshot_sha256",
    "root_snapshot_sha256s",
    "root_snapshot_reused",
    "root_query_count",
}


class TaskCoordinatorError(ValueError):
    pass


class InvestigationHost(Protocol):
    def xuanji_run_investigation(self, **kwargs: Any) -> dict[str, Any]: ...

    def xuanji_submit_repair(self, **kwargs: Any) -> dict[str, Any]: ...

    def xuanji_finalize(self, **kwargs: Any) -> dict[str, Any]: ...


class ResultArtifactStore(Protocol):
    def load(self, artifact_id: str) -> dict[str, Any]: ...


class TaskResultSink(Protocol):
    def __call__(
        self,
        task_id: str,
        analysis: dict[str, Any],
        validation_receipt: dict[str, Any],
    ) -> None: ...


class RegisteredAlertCoordinator:
    """Serial machine coordinator from one raw DQC payload to one task result."""

    def __init__(
        self,
        *,
        investigation_host: InvestigationHost,
        root_preflight: RootPreflight,
        run_result_store: ResultArtifactStore,
        task_result_sink: TaskResultSink,
        task_result_store: ResultArtifactStore,
        tasks_root: Path | str,
        repository_root: Path | str,
    ):
        self.investigation_host = investigation_host
        self.root_preflight = root_preflight
        self.run_result_store = run_result_store
        self.task_result_sink = task_result_sink
        self.task_result_store = task_result_store
        self.tasks_root = Path(tasks_root).resolve()
        self.repository_root = Path(repository_root).resolve()
        self.contracts = RepositoryContracts(self.repository_root)
        self.normalizer = AlertNormalizer()
        self.resolver = RouteResolver(DqcRouteRegistry(self.repository_root))
        self.assembler = TaskAssembler()
        if (
            self.root_preflight.contracts.definition_bundle_sha256
            != self.contracts.definition_bundle_sha256
        ):
            raise TaskCoordinatorError(
                "root preflight uses a different metric definition bundle"
            )

    def run_task(self, *, task_id: str, dqc_payload: Any) -> dict[str, Any]:
        self._validate_task_id(task_id)
        payload_sha256 = self._payload_sha256(dqc_payload)
        state_path = self._state_path(task_id)
        if state_path.exists():
            state = self._load_state(task_id)
            if state["payload_sha256"] != payload_sha256:
                raise TaskCoordinatorError(
                    "task_id cannot resume with a different immutable DQC payload"
                )
        else:
            state = self._initialize_task(task_id, dqc_payload, payload_sha256)
        return self._advance(state)

    def submit_repair(
        self,
        *,
        task_id: str,
        investigation_id: str,
        run_id: str,
        step_id: str,
        repair_attempt: int,
        repair_reason: str,
        error_evidence: str,
        repaired_sql: str,
    ) -> dict[str, Any]:
        state = self._load_state(task_id)
        investigation = self._current_investigation(state)
        if (
            investigation.get("investigation_id") != investigation_id
            or investigation.get("run_id") != run_id
            or investigation.get("status") != "awaiting_repair"
        ):
            raise TaskCoordinatorError("task is not awaiting this investigation repair")
        result = self.investigation_host.xuanji_submit_repair(
            run_id=run_id,
            step_id=step_id,
            repair_attempt=repair_attempt,
            repair_reason=repair_reason,
            error_evidence=error_evidence,
            repaired_sql=repaired_sql,
        )
        return self._record_host_action(state, investigation, result)

    def finalize(
        self,
        *,
        task_id: str,
        investigation_id: str,
        writer_patch: dict[str, Any],
    ) -> dict[str, Any]:
        state = self._load_state(task_id)
        patch_sha256 = canonical_sha256(writer_patch)
        existing = next(
            (
                item
                for item in state["investigations"]
                if item["investigation_id"] == investigation_id
            ),
            None,
        )
        if existing is None:
            raise TaskCoordinatorError("investigation_id does not belong to this task")
        if existing.get("status") == "completed":
            if existing.get("writer_patch_sha256") != patch_sha256:
                raise TaskCoordinatorError(
                    "completed investigation cannot accept a different writer patch"
                )
            return self._advance(state)
        current = self._current_investigation(state)
        if current is not existing or current.get("status") != "awaiting_writer":
            raise TaskCoordinatorError("task is not awaiting this investigation writer")

        mode = current.get("machine_mode")
        if mode == "full_queue":
            run_id = current.get("run_id")
            if not isinstance(run_id, str):
                raise TaskCoordinatorError("full queue investigation lacks its run ID")
            self.investigation_host.xuanji_finalize(
                run_id=run_id,
                writer_patch=writer_patch,
                analysis_context=self._analysis_context(current),
            )
            artifact = self.run_result_store.load(run_id)
            analysis = artifact.get("analysis")
            receipt = artifact.get("validation_receipt")
            if (
                not isinstance(analysis, dict)
                or not isinstance(analysis.get("investigations"), list)
                or len(analysis["investigations"]) != 1
                or not isinstance(receipt, dict)
            ):
                raise TaskCoordinatorError(
                    "authoritative investigation sink artifact is invalid"
                )
            result = deepcopy(analysis["investigations"][0])
            if result.get("rule_indexes") != current["rule_indexes"]:
                raise TaskCoordinatorError(
                    "authoritative investigation changed frozen rule indexes"
                )
            current["validation_receipt"] = deepcopy(receipt)
        else:
            result = self.assembler.assemble_machine_investigation(
                current, writer_patch
            )
            current["validation_receipt"] = None
        current["result"] = result
        current["writer_patch_sha256"] = patch_sha256
        current["status"] = "completed"
        current.pop("pending_response", None)
        state["current_investigation_index"] += 1
        self._write_state(state)
        return self._advance(state)

    def _advance(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("status") == "completed":
            return self._task_complete_response(state)
        while state["current_investigation_index"] < len(state["investigations"]):
            investigation = self._current_investigation(state)
            if investigation["status"] == "completed":
                state["current_investigation_index"] += 1
                self._write_state(state)
                continue
            if investigation["status"] in {"awaiting_writer", "awaiting_repair"}:
                return deepcopy(investigation["pending_response"])
            if investigation.get("route_resolution_status") == (
                "insufficient_definition"
            ):
                investigation["result_status"] = "insufficient_definition"
                investigation["machine_reason"] = investigation.get("reason")
                investigation["machine_mode"] = "definition_failed"
                return self._await_machine_writer(state, investigation)

            try:
                preflight = self.root_preflight.run(
                    investigation,
                    snapshot_root=self._task_root(state["task_id"])
                    / "root-snapshots",
                )
            except RootPreflightError as exc:
                route = investigation["route"]
                investigation["root_preflight"] = {
                    "status": exc.status,
                    "reason": str(exc),
                }
                investigation["result_status"] = exc.status
                investigation["machine_reason"] = str(exc)
                investigation["machine_mode"] = "root_precheck_failed"
                investigation["analysis_date"] = self._analysis_date(
                    investigation["alert_date"], route["analysis_lag_days"]
                )
                return self._await_machine_writer(state, investigation)

            investigation["root_preflight"] = preflight
            investigation["analysis_date"] = preflight["analysis_date"]
            if preflight["mode"] == "existing_anomaly_stop":
                investigation["result_status"] = "no_dominant_slice"
                investigation["machine_mode"] = "existing_anomaly_stop"
                return self._await_machine_writer(state, investigation)

            investigation["machine_mode"] = "full_queue"
            investigation["run_id"] = self._run_id(state, investigation)
            result = self.investigation_host.xuanji_run_investigation(
                run_id=investigation["run_id"],
                chain=preflight["chain"],
                game_type=preflight["game_type"],
                metric=preflight["metric"],
                alert_date=preflight["alert_date"],
                canonical_root_metric=preflight["canonical_root_metric"],
            )
            return self._record_host_action(state, investigation, result)

        analysis, receipt = self.assembler.assemble_task(state)
        self.task_result_sink(
            state["task_id"],
            deepcopy(analysis),
            deepcopy(receipt),
        )
        state["status"] = "completed"
        state["overall_status"] = analysis["overall_status"]
        state["task_analysis_sha256"] = receipt["analysis_sha256"]
        state["task_validation_receipt_sha256"] = receipt[
            "validation_receipt_sha256"
        ]
        self._write_state(state)
        return self._task_complete_response(state)

    def _record_host_action(
        self,
        state: dict[str, Any],
        investigation: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise TaskCoordinatorError("investigation Host returned an invalid envelope")
        action = result.get("action")
        if action == "repair_required":
            response = {
                "action": "repair_required",
                "task_id": state["task_id"],
                "investigation_id": investigation["investigation_id"],
                "run_id": investigation["run_id"],
                "repair": deepcopy(result.get("repair")),
            }
            investigation["status"] = "awaiting_repair"
        elif action == "write_conclusion":
            writer_pack = deepcopy(result.get("writer_pack"))
            if not isinstance(writer_pack, dict):
                raise TaskCoordinatorError("investigation Host omitted its writer pack")
            writer_pack["task_id"] = state["task_id"]
            writer_pack["investigation_id"] = investigation["investigation_id"]
            self._require_writer_budget(writer_pack)
            response = {
                "action": "write_conclusion",
                "task_id": state["task_id"],
                "investigation_id": investigation["investigation_id"],
                "run_id": investigation["run_id"],
                "writer_pack": writer_pack,
            }
            investigation["status"] = "awaiting_writer"
        else:
            raise TaskCoordinatorError(f"unsupported investigation Host action: {action}")
        investigation["pending_response"] = response
        self._write_state(state)
        return deepcopy(response)

    def _await_machine_writer(
        self,
        state: dict[str, Any],
        investigation: dict[str, Any],
    ) -> dict[str, Any]:
        preflight = investigation.get("root_preflight")
        writer_pack = {
            "analysis_profile": "primary_v1",
            "task_id": state["task_id"],
            "investigation_id": investigation["investigation_id"],
            "metric": investigation["metric_hint"],
            "analysis_date": investigation.get("analysis_date"),
            "game_type": (
                investigation["route"]["game_type"]
                if isinstance(investigation.get("route"), dict)
                else None
            ),
            "result_status_hint": investigation["result_status"],
            "rule_names": [
                item["rule_name"] for item in investigation["alert_rules"]
            ],
            "steps": [],
            "candidates": [],
            "evidence_limits": list(investigation.get("profile_warnings", [])),
        }
        if (
            investigation.get("machine_mode") == "existing_anomaly_stop"
            and isinstance(preflight, dict)
        ):
            writer_pack["root_metric"] = {
                "current_value": preflight["current_value"],
                "baseline_value": preflight["baseline_value"],
                "delta_bp": preflight["delta_bp"],
            }
            writer_pack["previous_value"] = preflight["previous_value"]
            writer_pack["root_adverse_delta_bp"] = preflight[
                "root_adverse_delta_bp"
            ]
        self._require_writer_budget(writer_pack)
        response = {
            "action": "write_conclusion",
            "task_id": state["task_id"],
            "investigation_id": investigation["investigation_id"],
            "writer_pack": writer_pack,
        }
        investigation["status"] = "awaiting_writer"
        investigation["pending_response"] = response
        self._write_state(state)
        return deepcopy(response)

    def _initialize_task(
        self,
        task_id: str,
        dqc_payload: Any,
        payload_sha256: str,
    ) -> dict[str, Any]:
        normalized = self.normalizer.normalize(dqc_payload)
        resolved = self.resolver.resolve(normalized)
        investigations = []
        for index, item in enumerate(resolved):
            route_status = item.pop("status")
            identity_hash = canonical_sha256(
                {
                    "payload_sha256": payload_sha256,
                    "rule_indexes": item["rule_indexes"],
                    "route": item.get("route"),
                }
            )
            investigations.append(
                {
                    **item,
                    "investigation_id": f"inv-{index:02d}-{identity_hash[:12]}",
                    "route_resolution_status": route_status,
                    "root_preflight": None,
                    "run_id": None,
                    "status": "pending",
                    "result": None,
                    "validation_receipt": None,
                    "writer_patch_sha256": None,
                }
            )
        state = {
            "schema_version": 1,
            "task_id": task_id,
            "payload_sha256": payload_sha256,
            "definition_bundle_sha256": self.contracts.definition_bundle_sha256,
            "status": "executing",
            "overall_status": None,
            "current_investigation_index": 0,
            "normalized_alert": normalized,
            "investigations": investigations,
            "task_analysis_sha256": None,
            "task_validation_receipt_sha256": None,
        }
        task_root = self._task_root(task_id)
        try:
            task_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError as exc:
            raise TaskCoordinatorError(f"task already exists without state: {task_id}") from exc
        self._write_state(state)
        return state

    def _task_complete_response(self, state: dict[str, Any]) -> dict[str, Any]:
        artifact = self.task_result_store.load(state["task_id"])
        analysis = artifact.get("analysis")
        receipt = artifact.get("validation_receipt")
        if not isinstance(analysis, dict) or not isinstance(receipt, dict):
            raise TaskCoordinatorError("authoritative task sink artifact is invalid")
        return {
            "action": "task_complete",
            "task_id": state["task_id"],
            "overall_status": analysis["overall_status"],
            "analysis_preview": self._model_visible_copy(analysis),
            "validation_receipt": {
                key: receipt[key]
                for key in (
                    "status",
                    "overall_status",
                    "investigation_count",
                    "successful_investigation_count",
                    "analysis_sha256",
                    "validation_receipt_sha256",
                )
            },
            "audit_detail": "retained_by_host",
        }

    def _analysis_context(self, investigation: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "dataworks_dqc",
            "project": investigation["project"],
            "table": investigation["table"],
            "partition": investigation["partition"],
            "investigation": {
                "rule_indexes": list(investigation["rule_indexes"]),
                "metric_hint": investigation["metric_hint"],
                "alert_partition": investigation["partition"],
                "alert_rules": deepcopy(investigation["alert_rules"]),
            },
        }

    def _load_state(self, task_id: str) -> dict[str, Any]:
        self._validate_task_id(task_id)
        try:
            state = json.loads(self._state_path(task_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskCoordinatorError(f"task state cannot be loaded: {task_id}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise TaskCoordinatorError("task state schema is invalid")
        expected = state.get("integrity_sha256")
        unsigned = dict(state)
        unsigned.pop("integrity_sha256", None)
        if expected != canonical_sha256(unsigned):
            raise TaskCoordinatorError("task state integrity check failed")
        if state.get("task_id") != task_id:
            raise TaskCoordinatorError("task state identity changed")
        if (
            state.get("definition_bundle_sha256")
            != self.contracts.definition_bundle_sha256
        ):
            raise TaskCoordinatorError("task metric definition bundle changed")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        state.pop("integrity_sha256", None)
        state["integrity_sha256"] = canonical_sha256(state)
        target = self._state_path(state["task_id"])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        encoded = (
            json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _current_investigation(self, state: dict[str, Any]) -> dict[str, Any]:
        index = state.get("current_investigation_index")
        investigations = state.get("investigations")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not isinstance(investigations, list)
            or not 0 <= index < len(investigations)
        ):
            raise TaskCoordinatorError("task has no current investigation")
        return investigations[index]

    def _run_id(
        self, state: dict[str, Any], investigation: dict[str, Any]
    ) -> str:
        task_id = state["task_id"]
        prefix = task_id[:80]
        task_hash = canonical_sha256(task_id)[:12]
        suffix = investigation["investigation_id"].replace("inv-", "")
        return f"{prefix}-{task_hash}-{suffix}"[:128]

    def _task_root(self, task_id: str) -> Path:
        return self.tasks_root / task_id

    def _state_path(self, task_id: str) -> Path:
        return self._task_root(task_id) / "state.json"

    @staticmethod
    def _validate_task_id(task_id: Any) -> None:
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise TaskCoordinatorError("task_id is invalid")

    @staticmethod
    def _payload_sha256(payload: Any) -> str:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TaskCoordinatorError("dqc_payload is not canonical JSON") from exc
        return sha256_bytes(encoded)

    @staticmethod
    def _analysis_date(alert_date: str, lag_days: int) -> str:
        return (date.fromisoformat(alert_date) - timedelta(days=lag_days)).isoformat()

    @staticmethod
    def _require_writer_budget(writer_pack: dict[str, Any]) -> None:
        size = writer_pack_size(writer_pack)
        if size > 12 * 1024:
            raise TaskCoordinatorError(
                f"task writer pack exceeds the 12 KB context budget: {size} bytes"
            )

    @classmethod
    def _model_visible_copy(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._model_visible_copy(child)
                for key, child in value.items()
                if key not in _PRIVATE_FIELDS
            }
        if isinstance(value, list):
            return [cls._model_visible_copy(child) for child in value]
        return deepcopy(value)
