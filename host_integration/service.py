from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from runtime.contracts import canonical_sha256
from runtime.final_assembler import FinalAssembler
from runtime.host_adapter import (
    DViewExecutionError,
    HostDViewAdapter,
    ProductionDViewExecutor,
)
from runtime.receipts import TrustedReceiptVerifier
from runtime.runner import AttributionRunner, RunnerError


ROOT = Path(__file__).resolve().parents[1]
_MODEL_PRIVATE_FIELDS = {
    "query_id",
    "raw_result",
    "raw_result_sha256",
    "receipt_id",
    "receipt_signature",
    "rendered_sql",
    "rendered_sql_sha256",
    "submitted_sql_sha256",
}


class ValidatedResultSink(Protocol):
    """Machine-only handoff for the complete validated analysis artifact."""

    def __call__(
        self,
        run_id: str,
        analysis: dict[str, Any],
        validation_receipt: dict[str, Any],
    ) -> None: ...


class PrimaryInvestigationHost:
    """Model-facing boundary for one trusted primary attribution run.

    The DView callable, raw rows, receipts, and authoritative analysis stay in this
    Host process. Only the three xuanji_* methods are intended to be registered as
    model tools. The validated_result_sink is invoked inside the Host and must not
    be exposed as another model tool.
    """

    def __init__(
        self,
        *,
        dview_query: Callable[..., Any],
        receipt_key_id: str,
        receipt_secret: bytes,
        runs_root: Path | str,
        validated_result_sink: ValidatedResultSink,
        repository_root: Path | str = ROOT,
        analysis_profile: str = "primary_v1",
    ):
        if not callable(validated_result_sink):
            raise RunnerError("validated_result_sink must be a Host-owned callable")
        signer = TrustedReceiptVerifier(
            key_id=receipt_key_id,
            secret=receipt_secret,
        )
        self._runner = AttributionRunner(
            repository_root,
            runs_root=runs_root,
            trusted_receipt_verifier=signer,
            analysis_profile=analysis_profile,
        )
        self._dview_query = dview_query
        self._adapter = HostDViewAdapter(
            runner=self._runner,
            executor=ProductionDViewExecutor(self._execute_dview_privately),
            receipt_signer=signer,
        )
        self._validated_result_sink = validated_result_sink

    def xuanji_run_investigation(
        self,
        *,
        run_id: str,
        chain: str,
        game_type: str,
        metric: str,
        alert_date: str,
        canonical_root_metric: dict[str, Any],
    ) -> dict[str, Any]:
        """Create or identically resume one schema-v4 run and execute its queue."""

        self._runner.init_run(
            run_id=run_id,
            chain=chain,
            game_type=game_type,
            metric=metric,
            alert_date=alert_date,
            canonical_root_metric=canonical_root_metric,
            receipt_mode="trusted_host",
            resume=True,
        )
        return self._execute_until_model_action(run_id)

    def xuanji_submit_repair(
        self,
        *,
        run_id: str,
        step_id: str,
        repair_attempt: int,
        repair_reason: str,
        error_evidence: str,
        repaired_sql: str,
    ) -> dict[str, Any]:
        """Submit the one model-visible exception path and resume Host execution."""

        current = self._runner.next_action(run_id)
        if current.get("action") != "repair_query":
            raise RunnerError("run is not awaiting a semantic SQL repair")
        self._runner.record(
            run_id,
            {
                "event": "repair_submitted",
                "step_id": step_id,
                "repair_attempt": repair_attempt,
                "repair_reason": repair_reason,
                "error_evidence": error_evidence,
                "repaired_sql": repaired_sql,
            },
        )
        return self._execute_until_model_action(run_id)

    def xuanji_finalize(
        self,
        *,
        run_id: str,
        writer_patch: dict[str, Any],
        analysis_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Assemble, validate, and internally hand off the authoritative result."""

        state = self._runner.load_state(run_id)
        if state["status"] == "finalized":
            analysis, receipt = self._load_finalized_artifacts(run_id)
            candidate = FinalAssembler().assemble(
                writer_pack=self._runner.build_writer_pack(run_id),
                machine_state=state,
                attribution_execution=self._runner.export(run_id),
                writer_patch=writer_patch,
                analysis_context=analysis_context,
            )
            if canonical_sha256(candidate) != canonical_sha256(analysis):
                raise RunnerError(
                    "finalized run cannot accept a different writer patch or context"
                )
        else:
            analysis = self._runner.assemble_final(
                run_id,
                writer_patch,
                analysis_context,
            )
            analysis_path = (
                self._runner.runs_root
                / run_id
                / "final"
                / "assembled-analysis.json"
            )
            receipt = self._runner.validate_final(run_id, analysis_path, 0)

        try:
            self._validated_result_sink(
                run_id,
                deepcopy(analysis),
                deepcopy(receipt),
            )
        except Exception as exc:
            raise RunnerError(
                f"validated result sink failed: {type(exc).__name__}"
            ) from None
        return {
            "action": "finalized",
            "run_id": run_id,
            "analysis_preview": self._model_visible_copy(analysis),
            "validation_receipt": self._validation_summary(receipt),
            "audit_detail": "retained_by_host",
        }

    def _execute_until_model_action(self, run_id: str) -> dict[str, Any]:
        result = self._adapter.execute_until_blocked(run_id)
        action = result.get("action")
        executed_query_count = result.get("executed_query_count", 0)
        if action == "queue_complete":
            return {
                "action": "write_conclusion",
                "run_id": run_id,
                "executed_query_count": executed_query_count,
                "writer_pack": self._runner.build_writer_pack(run_id),
            }
        if action == "repair_query":
            return {
                "action": "repair_required",
                "run_id": run_id,
                "executed_query_count": executed_query_count,
                "repair": {
                    "step_id": result["step_id"],
                    "repair_attempt": result["repair_attempt"],
                    "max_repairs": result["max_repairs"],
                    "raw_error": deepcopy(result["raw_error"]),
                    "original_sql": result["original_sql"],
                    "triage_text": result["triage_text"],
                    "required_submission": list(result["required_submission"]),
                },
            }
        raise RunnerError(f"Host stopped at an unsupported action: {action}")

    def _execute_dview_privately(self, **kwargs: Any) -> Any:
        try:
            return self._dview_query(**kwargs)
        except DViewExecutionError:
            raise
        except Exception as exc:
            raise RunnerError(
                f"DView Host call failed before a typed response: {type(exc).__name__}"
            ) from None

    def _load_finalized_artifacts(
        self,
        run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        analysis_path = (
            self._runner.runs_root / run_id / "final" / "assembled-analysis.json"
        )
        try:
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError(
                f"finalized analysis artifact cannot be read: {exc}"
            ) from exc
        receipt = self._runner.validate_final(run_id, analysis_path, 0)
        if not isinstance(analysis, dict) or not isinstance(receipt, dict):
            raise RunnerError("finalized run lacks its authoritative artifacts")
        return analysis, receipt

    def _model_visible_copy(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._model_visible_copy(child)
                for key, child in value.items()
                if key not in _MODEL_PRIVATE_FIELDS
            }
        if isinstance(value, list):
            return [self._model_visible_copy(child) for child in value]
        return deepcopy(value)

    def _validation_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "status",
            "investigation_status",
            "execution_mode",
            "validated_step_count",
            "analysis_sha256",
            "validation_receipt_sha256",
        )
        summary = {field: receipt[field] for field in fields}
        summary["authoritative_analysis_sha256"] = summary.pop("analysis_sha256")
        return summary
