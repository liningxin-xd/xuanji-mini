from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    RepositoryContracts,
    canonical_sha256,
    sha256_bytes,
    sha256_text,
)
from .calibration_runner import CalibrationError, CalibrationRunner
from .evidence_pack import EvidencePackBuilder, EvidencePackError
from .final_assembler import FinalAssembler, FinalAssemblyError
from .final_validator import FinalEvidenceValidator, FinalValidationError
from .game_background_validator import (
    GameBackgroundValidationError,
    GameBackgroundValidator,
)
from .models import QueryBinding, RunStatus, StepStatus, TERMINAL_STEP_STATUSES
from .query_builder import QueryBuildError, QueryBuilder
from .receipts import ReceiptVerificationError, TrustedReceiptVerifier
from .result_validator import ResultValidationError, ResultValidator
from .secondary_query_builder import SecondaryQueryBuilder
from .secondary_result_validator import SecondaryResultValidator


ROOT = Path(__file__).resolve().parents[1]
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RunnerError(ValueError):
    pass


class AttributionRunner:
    def __init__(
        self,
        root: Path | str = ROOT,
        runs_root: Path | str | None = None,
        trusted_receipt_verifier: TrustedReceiptVerifier | None = None,
        analysis_profile: str | None = None,
    ):
        self.root = Path(root).resolve()
        self.contracts = RepositoryContracts(self.root)
        self.query_builder = QueryBuilder(self.contracts)
        self.result_validator = ResultValidator(self.contracts)
        self.secondary_query_builder = SecondaryQueryBuilder(self.contracts)
        self.secondary_result_validator = SecondaryResultValidator(self.contracts)
        self.game_background_validator = GameBackgroundValidator(self.contracts)
        configured_profile = os.environ.get("XUANJI_ANALYSIS_PROFILE")
        self.analysis_profile = (
            analysis_profile
            or configured_profile
            or self.contracts.default_analysis_profile
        )
        self.contracts.analysis_profile(self.analysis_profile)
        self.calibration_runner = CalibrationRunner(self.contracts)
        self.trusted_receipt_verifier = trusted_receipt_verifier
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
        analysis_date: str | None = None,
        canonical_root_metric: dict[str, Any] | None = None,
        receipt_mode: str = "trusted_host",
        resume: bool = False,
    ) -> dict[str, Any]:
        self._validate_run_id(run_id)
        self._validate_date(alert_date, "alert_date")
        expected_analysis_date = self._derive_analysis_date(chain, alert_date)
        if analysis_date is not None:
            self._validate_date(analysis_date, "analysis_date")
            if analysis_date != expected_analysis_date:
                raise RunnerError(
                    "analysis_date does not match the registered chain mapping: "
                    f"expected {expected_analysis_date}"
                )
        analysis_date = expected_analysis_date
        if receipt_mode not in {"trusted_host", "self_reported"}:
            raise RunnerError("receipt_mode must be trusted_host or self_reported")
        if canonical_root_metric is not None and not self._valid_root_metric(
            canonical_root_metric
        ):
            raise RunnerError("canonical_root_metric must contain finite root facts")
        if receipt_mode == "trusted_host" and canonical_root_metric is None:
            raise RunnerError(
                "trusted_host runs require a root preflight canonical metric"
            )
        frozen_root = (
            {
                "current_value": float(canonical_root_metric["current_value"]),
                "baseline_value": float(canonical_root_metric["baseline_value"]),
                "delta": float(canonical_root_metric["delta"]),
            }
            if canonical_root_metric is not None
            else None
        )
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
                "receipt_mode": receipt_mode,
                "plan_id": plan.id,
                "plan_contract_sha256": plan.sha256,
                "execution_plan_sha256": self.contracts.execution_plan_sha256,
                "query_registry_sha256": self.contracts.query_registry_sha256,
                "triage_sha256": self.contracts.triage_sha256,
                "result_schemas_sha256": self.contracts.result_schemas_sha256,
                "secondary_relations_sha256": (
                    self.contracts.secondary_relations_sha256
                    if self.analysis_profile == "primary_v2"
                    else None
                ),
            }
            mismatches = {
                key: {"expected": value, "actual": state.get(key)}
                for key, value in expected.items()
                if state.get(key) != value
            }
            if frozen_root is not None and not self._root_metrics_match(
                state.get("canonical_root_metric"), frozen_root
            ):
                mismatches["canonical_root_metric"] = {
                    "expected": frozen_root,
                    "actual": state.get("canonical_root_metric"),
                }
            state_profile = state.get("analysis_profile", "primary_v1")
            if state_profile != self.analysis_profile:
                mismatches["analysis_profile"] = {
                    "expected": self.analysis_profile,
                    "actual": state_profile,
                }
            if mismatches:
                raise RunnerError(
                    f"resume arguments do not match immutable run state: {mismatches}"
                )
            return {"resumed": True, **self._status_payload(state)}

        if receipt_mode == "trusted_host" and self.trusted_receipt_verifier is None:
            raise RunnerError(
                "trusted_host runs must be initialized by a Host adapter with a "
                "trusted receipt verifier"
            )

        steps = []
        for plan_step in plan.steps:
            binding = self.contracts.binding_for(
                plan, plan_step.id, metric, game_type
            )
            binding_snapshot = self._binding_snapshot(binding)
            steps.append(
                {
                    "id": plan_step.id,
                    "kind": plan_step.kind,
                    "produces_candidates": plan_step.produces_candidates,
                    "failure_scope": plan_step.failure_scope,
                    "automatic_status": plan_step.automatic_status,
                    "automatic_reason": plan_step.automatic_reason,
                    "status": StepStatus.PENDING.value,
                    "binding": binding_snapshot,
                    "binding_sha256": canonical_sha256(binding_snapshot),
                    "attempts": [],
                    "candidate_count": None,
                    "candidates": [],
                    "root_current_value": None,
                    "root_baseline_value": None,
                    "root_delta": None,
                    "root_current_numerator": None,
                    "root_current_denominator": None,
                    "root_baseline_numerator": None,
                    "root_baseline_denominator": None,
                    "family_adverse_impact_bp": None,
                    "failure_code": None,
                    "reason": None,
                    "warning_codes": [],
                }
            )

        state = {
            "schema_version": 4,
            "run_id": run_id,
            "revision": 0,
            "status": RunStatus.ACTIVE.value,
            "execution_mode": (
                "trusted_host_adapter"
                if receipt_mode == "trusted_host"
                else "self_reported_development"
            ),
            "receipt_mode": receipt_mode,
            "trusted_receipt_key_id": (
                self.trusted_receipt_verifier.key_id
                if receipt_mode == "trusted_host"
                else None
            ),
            "plan_id": plan.id,
            "plan_contract_sha256": plan.sha256,
            "execution_plan_sha256": self.contracts.execution_plan_sha256,
            "query_registry_sha256": self.contracts.query_registry_sha256,
            "triage_sha256": self.contracts.triage_sha256,
            "result_schemas_sha256": self.contracts.result_schemas_sha256,
            "secondary_relations_sha256": (
                self.contracts.secondary_relations_sha256
                if self.analysis_profile == "primary_v2"
                else None
            ),
            "analysis_profile": self.analysis_profile,
            "analysis_profile_sha256": self.contracts.analysis_profile_sha256(
                self.analysis_profile
            ),
            "post_primary_plan_sha256": (
                self.contracts.post_primary_plan_contract_sha256("post_primary_v1")
                if self.analysis_profile == "primary_v2"
                else None
            ),
            "chain": chain,
            "game_type": game_type,
            "metric": metric,
            "alert_date": alert_date,
            "analysis_date": analysis_date,
            "canonical_root_metric": frozen_root,
            "cursor": 0,
            "ready_for_final_validation": False,
            "evidence_export_sha256": None,
            "final_analysis_sha256": None,
            "validation_receipt": None,
            "post_primary": None,
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
            return self._next_post_primary_action(state)

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
            return self._next_post_primary_action(state)

        step = state["steps"][state["cursor"]]
        if step["status"] == StepStatus.REPAIR_REQUIRED.value:
            if changed:
                state["revision"] += 1
                self._write_state(state)
            return self._repair_ticket(state, step)
        if step["status"] == StepStatus.PENDING.value:
            binding = self._binding_from_step(step)
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
                    "raw_result_sha256": None,
                    "validation": None,
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
        is_post_primary = state["cursor"] >= len(state["steps"])
        post_primary_step = None
        if is_post_primary:
            active = self._active_post_primary_query(state)
            if active is None:
                raise RunnerError(
                    "queue and post-primary analysis are complete; "
                    "no further events are accepted"
                )
            post_primary_step, step = active
        else:
            step = state["steps"][state["cursor"]]
        if event.get("step_id") != step["id"]:
            raise RunnerError(
                f"event targets non-current step {event.get('step_id')}; "
                f"current step is {step['id']}"
            )
        event_type = event.get("event")
        if event_type == "repair_submitted":
            return self._record_repair(state, step, event, event_hash)
        if event_type not in {"query_returned", "query_error"}:
            raise RunnerError(f"unsupported record event: {event_type}")
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
        self._validate_record_event_fields(event_type, event, state)
        self._require_submitted_hash(event, attempt)
        self._verify_receipt(state, event)
        query_id = self._required_non_empty_string(event, "query_id")
        self._ensure_unique_query_id(state, step["id"], query_id)
        if event_type == "query_returned":
            raw_result = event.get("raw_result")
            if not isinstance(raw_result, dict):
                raise RunnerError("raw_result must be an object")
            raw_result_sha256 = self._required_sha256(event, "raw_result_sha256")
            if canonical_sha256(raw_result) != raw_result_sha256:
                raise RunnerError("raw_result_sha256 does not match raw_result")
        else:
            raw_result = None
            raw_result_sha256 = None

        event_path = Path("events") / f"{event_hash}.json"
        self._atomic_write_json(self._run_dir(run_id) / event_path, event)
        attempt["event_path"] = str(event_path)
        attempt["query_id"] = query_id
        attempt["raw_result_sha256"] = raw_result_sha256

        advance_cursor = True
        if event_type == "query_returned":
            binding = self._binding_from_step(step)
            if binding is None:  # pragma: no cover - automatic steps issue no ticket
                raise RunnerError("query result has no immutable binding")
            try:
                if is_post_primary and post_primary_step["id"] == "secondary":
                    outcome = self.secondary_result_validator.validate(
                        raw_result=raw_result,
                        binding=binding,
                        chain=state["chain"],
                        metric=state["metric"],
                        analysis_date=state["analysis_date"],
                        game_type=state["game_type"],
                        parent_value=step["parent_value"],
                        parent_counts=self._secondary_parent_counts(state, step),
                        root_counts=self._secondary_root_counts(state),
                    )
                elif is_post_primary and post_primary_step["id"] == (
                    "game_background"
                ):
                    outcome = self.game_background_validator.validate(
                        raw_result=raw_result,
                        binding=binding,
                        analysis_date=state["analysis_date"],
                        game_id=int(step["game_id"]),
                    )
                else:
                    outcome = self.result_validator.validate(
                        raw_result=raw_result,
                        binding=binding,
                        step_id=step["id"],
                        metric=state["metric"],
                        analysis_date=state["analysis_date"],
                        game_type=state["game_type"],
                        produces_candidates=step["produces_candidates"],
                    )
                    self._validate_canonical_root_metric(state, step, outcome)
            except (ResultValidationError, GameBackgroundValidationError) as exc:
                attempt["status"] = "failed"
                attempt["validation"] = {
                    "status": "failed",
                    "failure_code": exc.code,
                    "reason": str(exc),
                }
                step["status"] = StepStatus.FAILED.value
                step["failure_code"] = exc.code
                step["reason"] = f"{exc.code}: {exc}"
            else:
                attempt["status"] = "succeeded"
                if is_post_primary and post_primary_step["id"] == (
                    "game_background"
                ):
                    attempt["validation"] = {
                        "status": "succeeded",
                        "fact_count": len(outcome.facts),
                        "limit_codes": list(outcome.limit_codes),
                    }
                    step["status"] = StepStatus.SUCCEEDED.value
                    step["facts"] = list(outcome.facts)
                    step["limit_codes"] = list(outcome.limit_codes)
                    step["failure_code"] = None
                    step["reason"] = None
                else:
                    attempt["validation"] = {
                        "status": "succeeded",
                        "candidate_count": outcome.candidate_count,
                        "warning_codes": list(outcome.warning_codes),
                        "root_current_value": outcome.root_current_value,
                        "root_baseline_value": outcome.root_baseline_value,
                        "root_delta": outcome.root_delta,
                    }
                    step["status"] = StepStatus.SUCCEEDED.value
                    step["candidate_count"] = outcome.candidate_count
                    step["candidates"] = list(outcome.candidates)
                    step["root_current_value"] = outcome.root_current_value
                    step["root_baseline_value"] = outcome.root_baseline_value
                    step["root_delta"] = outcome.root_delta
                    step["root_current_numerator"] = outcome.root_current_numerator
                    step["root_current_denominator"] = outcome.root_current_denominator
                    step["root_baseline_numerator"] = outcome.root_baseline_numerator
                    step["root_baseline_denominator"] = outcome.root_baseline_denominator
                    step["family_adverse_impact_bp"] = outcome.family_adverse_impact_bp
                    step["warning_codes"] = list(outcome.warning_codes)
        else:
            raw_error = {
                "class": self._required_non_empty_string(event, "error_class"),
                "code": self._required_non_empty_string(event, "error_code"),
                "message": self._required_non_empty_string(event, "error_message"),
            }
            attempt["status"] = "error"
            attempt["error"] = raw_error
            if raw_error["class"] == "semantic_analysis" and attempt_no < 2:
                step["status"] = StepStatus.REPAIR_REQUIRED.value
                advance_cursor = False
            else:
                step["status"] = StepStatus.FAILED.value
                step["failure_code"] = self._query_failure_code(raw_error)
                repair_suffix = (
                    " after two evidence-based repairs"
                    if raw_error["class"] == "semantic_analysis" and attempt_no == 2
                    else ""
                )
                step["reason"] = (
                    f"{raw_error['class']} {raw_error['code']}{repair_suffix}: "
                    f"{raw_error['message']}"
                )
                attempt["validation"] = {
                    "status": "failed",
                    "failure_code": step["failure_code"],
                    "reason": step["reason"],
                }

        state["processed_events"][event_hash] = {
            "event": event_type,
            "step_id": step["id"],
            "event_path": str(event_path),
        }
        if advance_cursor and not is_post_primary:
            state["cursor"] += 1
            if state["cursor"] >= len(state["steps"]):
                self._mark_queue_complete(state)
        elif advance_cursor:
            if post_primary_step["id"] == "game_background":
                self._advance_post_primary_cursor(post_primary_step, step)
            try:
                post_primary = self.calibration_runner.execute(state)
            except CalibrationError as exc:
                raise RunnerError(str(exc)) from exc
            state["post_primary"] = post_primary
            state["ready_for_final_validation"] = bool(
                isinstance(post_primary, dict)
                and post_primary.get("status") == "completed"
            )
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
        state = self._ensure_post_primary(state)
        if state.get("analysis_profile", "primary_v1") == "primary_v2" and (
            not isinstance(state.get("post_primary"), dict)
            or state["post_primary"].get("status") != "completed"
        ):
            raise RunnerError(
                "cannot export before post-primary analysis is terminal"
            )
        evidence = self._export_evidence(state)
        evidence_sha256 = canonical_sha256(evidence)
        export_path = self._run_dir(run_id) / "exports/attribution-execution.json"
        if state["evidence_export_sha256"] is None:
            self._atomic_write_json(export_path, evidence)
            state["evidence_export_sha256"] = evidence_sha256
            state["revision"] += 1
            self._write_state(state)
        elif state["evidence_export_sha256"] != evidence_sha256:
            raise RunnerError("exported evidence no longer matches immutable run state")
        elif not export_path.is_file() or canonical_sha256(
            json.loads(export_path.read_text(encoding="utf-8"))
        ) != evidence_sha256:
            raise RunnerError("stored attribution evidence is missing or changed")
        return evidence

    def build_writer_pack(self, run_id: str) -> dict[str, Any]:
        self.export(run_id)
        state = self._load_state(run_id)
        pack = EvidencePackBuilder().build(state)
        self._atomic_write_json(
            self._run_dir(run_id) / "exports/writer-pack.json", pack
        )
        return pack

    def assemble_final(
        self,
        run_id: str,
        writer_patch: dict[str, Any],
        analysis_context: dict[str, Any],
    ) -> dict[str, Any]:
        execution = self.export(run_id)
        pack = self.build_writer_pack(run_id)
        analysis = FinalAssembler().assemble(
            writer_pack=pack,
            attribution_execution=execution,
            writer_patch=writer_patch,
            analysis_context=analysis_context,
        )
        self._atomic_write_json(
            self._run_dir(run_id) / "final/assembled-analysis.json", analysis
        )
        return analysis

    def validate_final(
        self,
        run_id: str,
        analysis_path: Path | str,
        investigation_index: int,
    ) -> dict[str, Any]:
        state = self._load_state(run_id)
        path = Path(analysis_path)
        try:
            content = path.read_bytes()
            analysis = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerError(f"analysis JSON cannot be read: {exc}") from exc
        analysis_sha256 = sha256_bytes(content)
        if state["status"] == RunStatus.FINALIZED.value:
            receipt = state.get("validation_receipt")
            if (
                state.get("final_analysis_sha256") == analysis_sha256
                and isinstance(receipt, dict)
                and receipt.get("investigation_index") == investigation_index
            ):
                return dict(receipt)
            raise RunnerError("finalized run cannot validate different final evidence")
        if state["status"] != RunStatus.QUEUE_COMPLETE.value:
            raise RunnerError("run must be queue_complete before final validation")
        if not isinstance(state.get("evidence_export_sha256"), str):
            raise RunnerError("export attribution evidence before final validation")
        validation = FinalEvidenceValidator().validate(
            state, analysis, investigation_index
        )
        receipt = {
            **validation,
            "analysis_sha256": analysis_sha256,
            "attribution_evidence_sha256": state["evidence_export_sha256"],
        }
        receipt["validation_receipt_sha256"] = canonical_sha256(receipt)
        self._atomic_write_json(
            self._run_dir(run_id) / "final/validation-receipt.json", receipt
        )
        state["final_analysis_sha256"] = analysis_sha256
        state["validation_receipt"] = receipt
        state["status"] = RunStatus.FINALIZED.value
        state["revision"] += 1
        self._write_state(state)
        return receipt

    def _export_evidence(self, state: dict[str, Any]) -> dict[str, Any]:
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
        evidence = {
            "mode": "full_queue",
            "chain": state["chain"],
            "game_type": state["game_type"],
            "execution_mode": state["execution_mode"],
            "steps": exported_steps,
        }
        if state.get("analysis_profile", "primary_v1") == "primary_v2":
            post_primary = state.get("post_primary")
            if not isinstance(post_primary, dict) or post_primary.get("status") != (
                "completed"
            ):
                raise RunnerError("primary_v2 post-primary analysis is incomplete")
            post_steps = []
            secondary_steps = []
            for step in post_primary["steps"]:
                audit_step = {"step": step["id"], "status": step["status"]}
                for field in ("reason", "failure_code", "limit_code"):
                    if isinstance(step.get(field), str):
                        audit_step[field] = step[field]
                post_steps.append(audit_step)
                if step["id"] != "secondary" or step["status"] not in {
                    "succeeded",
                    "failed",
                }:
                    continue
                secondary_step = {
                    "parent_dimension": step["parent_dimension"],
                    "parent_value": step["parent_value"],
                    "parent_label": step["parent_label"],
                    "step": step["child_dimension"],
                    "status": step["status"],
                }
                if step["status"] == "succeeded":
                    secondary_step["candidate_count"] = step["candidate_count"]
                else:
                    secondary_step["reason"] = step["reason"]
                query_id = self._last_query_id(step)
                if query_id:
                    secondary_step["query_id"] = query_id
                if step.get("warning_codes"):
                    secondary_step["warning_codes"] = list(step["warning_codes"])
                secondary_steps.append(secondary_step)
            evidence.update(
                {
                    "analysis_profile": "primary_v2",
                    "primary_evidence_sha256": post_primary[
                        "primary_evidence_sha256"
                    ],
                    "post_primary": {
                        "status": post_primary["status"],
                        "steps": post_steps,
                    },
                }
            )
            if secondary_steps:
                evidence["secondary_steps"] = secondary_steps
        return evidence

    def _ensure_post_primary(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("analysis_profile", "primary_v1") == "primary_v1":
            return state
        changed = False
        if state.get("post_primary") is None:
            plan = self.calibration_runner.create_plan(state)
            if plan is None:
                raise RunnerError("primary_v2 lacks its post-primary plan")
            state["post_primary"] = plan
            state["ready_for_final_validation"] = False
            changed = True
        try:
            result = self.calibration_runner.execute(state)
        except CalibrationError as exc:
            raise RunnerError(str(exc)) from exc
        if result != state.get("post_primary"):
            state["post_primary"] = result
            changed = True
        ready = bool(
            isinstance(result, dict) and result.get("status") == "completed"
        )
        if state.get("ready_for_final_validation") != ready:
            state["ready_for_final_validation"] = ready
            changed = True
        if changed:
            state["revision"] += 1
            self._write_state(state)
        return state

    def _next_post_primary_action(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("analysis_profile", "primary_v1") == "primary_v1":
            return self._queue_complete_ticket(state)
        state = self._ensure_post_primary(state)
        post_primary = state.get("post_primary")
        if not isinstance(post_primary, dict):
            raise RunnerError("primary_v2 lacks post-primary state")
        if post_primary.get("status") == "completed":
            return self._queue_complete_ticket(state)
        current = self._current_post_primary_query(state)
        if current is None:
            raise RunnerError("post-primary plan lacks an executable current query")
        post_step, query_item = current
        if query_item["status"] == "repair_required":
            return self._repair_ticket(state, query_item)
        if query_item["status"] == "planned":
            self._build_current_post_primary_query(state, post_step, query_item)
        if query_item["status"] != "in_progress":
            raise RunnerError(
                f"post-primary query cannot execute: {query_item['status']}"
            )
        return self._execution_ticket(state, query_item)

    def _build_current_post_primary_query(
        self,
        state: dict[str, Any],
        post_step: dict[str, Any],
        query_item: dict[str, Any],
    ) -> None:
        if post_step["id"] == "secondary":
            built = self.secondary_query_builder.build(
                chain=state["chain"],
                metric=state["metric"],
                business_date=state["analysis_date"],
                game_type=state["game_type"],
                parent_dimension=query_item["parent_dimension"],
                parent_value=query_item["parent_value"],
                child_dimension=query_item["child_dimension"],
            )
        elif post_step["id"] == "game_background":
            binding = self.contracts.game_background_binding(
                game_id=int(query_item["game_id"])
            )
            built = self.query_builder.build(
                binding,
                {
                    "business_date": state["analysis_date"],
                    "game_id": int(query_item["game_id"]),
                },
            )
        else:  # pragma: no cover - guarded by the plan contract
            raise RunnerError(
                f"unsupported post-primary query step: {post_step['id']}"
            )

        if post_step["id"] == "secondary":
            binding_snapshot = self._binding_snapshot(built.binding)
            query_item.update(
                {
                    "binding": binding_snapshot,
                    "binding_sha256": canonical_sha256(binding_snapshot),
                    "attempts": [],
                    "candidate_count": None,
                    "candidates": [],
                    "root_current_value": None,
                    "root_baseline_value": None,
                    "root_delta": None,
                    "root_current_numerator": None,
                    "root_current_denominator": None,
                    "root_baseline_numerator": None,
                    "root_baseline_denominator": None,
                    "family_adverse_impact_bp": None,
                    "failure_code": None,
                    "reason": None,
                    "warning_codes": [],
                }
            )
        else:
            binding_snapshot = self._binding_snapshot(built.binding)
            query_item["binding"] = binding_snapshot
            query_item["binding_sha256"] = canonical_sha256(binding_snapshot)

        path_id = self._post_primary_path_id(query_item)
        sql_path = self._sql_relative_path(len(state["steps"]), path_id, 0)
        self._atomic_write_text(
            self._run_dir(state["run_id"]) / sql_path, built.sql
        )
        query_item["attempts"].append(
            {
                "attempt_no": 0,
                "status": "issued",
                "sql_sha256": built.sha256,
                "sql_path": sql_path,
                "query_id": None,
                "error": None,
                "event_path": None,
                "raw_result_sha256": None,
                "validation": None,
            }
        )
        query_item["status"] = "in_progress"
        post_step["status"] = "in_progress"
        state["revision"] += 1
        self._write_state(state)

    def _current_post_primary_query(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        post_primary = state.get("post_primary")
        if not isinstance(post_primary, dict) or post_primary.get("status") != "executing":
            return None
        for step in post_primary.get("steps", []):
            if not isinstance(step, dict):
                continue
            if step.get("id") == "secondary" and step.get("status") in {
                "planned",
                "in_progress",
                "repair_required",
            }:
                return step, step
            if step.get("id") != "game_background" or step.get("status") not in {
                "planned",
                "in_progress",
            }:
                continue
            items = step.get("items")
            cursor = step.get("cursor")
            if (
                isinstance(items, list)
                and isinstance(cursor, int)
                and 0 <= cursor < len(items)
                and isinstance(items[cursor], dict)
                and items[cursor].get("status")
                in {"planned", "in_progress", "repair_required"}
            ):
                return step, items[cursor]
        return None

    def _active_post_primary_query(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        current = self._current_post_primary_query(state)
        if current is None or current[1].get("status") not in {
            "in_progress",
            "repair_required",
        }:
            return None
        return current

    def _advance_post_primary_cursor(
        self, post_step: dict[str, Any], query_item: dict[str, Any]
    ) -> None:
        items = post_step.get("items")
        cursor = post_step.get("cursor")
        if (
            post_step.get("id") != "game_background"
            or not isinstance(items, list)
            or not isinstance(cursor, int)
            or cursor >= len(items)
            or items[cursor] is not query_item
            or query_item.get("status") not in {"succeeded", "failed"}
        ):
            raise RunnerError("game background cursor cannot advance")
        post_step["cursor"] += 1
        if post_step["cursor"] < len(items):
            post_step["status"] = "in_progress"
            return
        if any(item.get("status") == "succeeded" for item in items):
            post_step["status"] = "succeeded"
            post_step.pop("failure_code", None)
            post_step.pop("reason", None)
        else:
            post_step["status"] = "failed"
            post_step["failure_code"] = "game_background_unavailable"
            post_step["reason"] = "all selected game background queries failed"

    def _secondary_parent_counts(
        self, state: dict[str, Any], secondary: dict[str, Any]
    ) -> dict[str, Any]:
        game_step = next(step for step in state["steps"] if step["id"] == "game_id")
        candidate = next(
            (
                item
                for item in game_step["candidates"]
                if item.get("value") == secondary["parent_value"]
                and item.get("label") == secondary["parent_label"]
            ),
            None,
        )
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("private_counts"), dict
        ):
            raise RunnerError("secondary parent no longer rehooks its game candidate")
        return dict(candidate["private_counts"])

    def _secondary_root_counts(self, state: dict[str, Any]) -> dict[str, Any]:
        game_step = next(step for step in state["steps"] if step["id"] == "game_id")
        return {
            "current_numerator": game_step["root_current_numerator"],
            "current_denominator": game_step["root_current_denominator"],
            "baseline_numerator": game_step["root_baseline_numerator"],
            "baseline_denominator": game_step["root_baseline_denominator"],
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
        binding = self._binding_from_step(step)
        if binding is None:  # pragma: no cover - automatic steps issue no ticket
            raise RunnerError("query ticket has no immutable binding")
        parameters = self._query_parameters(state, step)
        ticket = {
            "action": "execute_query",
            "run_id": state["run_id"],
            "revision": state["revision"],
            "step_id": step["id"],
            "attempt_no": attempt["attempt_no"],
            "query_asset_path": binding.asset_path,
            "query_asset_sha256": binding.asset_sha256,
            "binding_sha256": step["binding_sha256"],
            "result_schema_id": binding.result_schema_id,
            "rendered_sql_sha256": attempt["sql_sha256"],
            "parameters": parameters,
            "rendered_sql": sql,
            "receipt_mode": state["receipt_mode"],
            "allowed_outcomes": ["query_returned", "query_error"],
        }
        self._atomic_write_json(
            self._run_dir(state["run_id"])
            / "tickets"
            / (
                f"{state['cursor']:02d}-{self._post_primary_path_id(step)}-"
                f"attempt-{attempt['attempt_no']}.json"
            ),
            ticket,
        )
        return ticket

    def _repair_ticket(
        self, state: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        if not step["attempts"]:
            raise RunnerError("repair_required step has no failed attempt")
        attempt = step["attempts"][-1]
        if attempt.get("status") != "error" or not attempt.get("error"):
            raise RunnerError("repair_required step has no raw SQL error")
        repair_attempt = attempt["attempt_no"] + 1
        if repair_attempt > 2:
            raise RunnerError("maximum SQL repairs already exhausted")
        original_sql = (
            self._run_dir(state["run_id"]) / attempt["sql_path"]
        ).read_text(encoding="utf-8")
        baseline_attempt = step["attempts"][0]
        ticket = {
            "action": "repair_query",
            "run_id": state["run_id"],
            "revision": state["revision"],
            "step_id": step["id"],
            "repair_attempt": repair_attempt,
            "max_repairs": 2,
            "cursor_locked": True,
            "raw_error": dict(attempt["error"]),
            "original_sql_sha256": attempt["sql_sha256"],
            "attempt_0_sql_sha256": baseline_attempt["sql_sha256"],
            "binding_sha256": step["binding_sha256"],
            "triage_sha256": state["triage_sha256"],
            "original_sql": original_sql,
            "triage_text": self.contracts.triage_text(),
            "required_submission": [
                "repair_reason",
                "error_evidence",
                "repaired_sql",
            ],
        }
        self._atomic_write_json(
            self._run_dir(state["run_id"])
            / "tickets"
            / (
                f"{state['cursor']:02d}-{self._post_primary_path_id(step)}-"
                f"repair-{repair_attempt}.json"
            ),
            ticket,
        )
        return ticket

    def _record_repair(
        self,
        state: dict[str, Any],
        step: dict[str, Any],
        event: dict[str, Any],
        event_hash: str,
    ) -> dict[str, Any]:
        allowed_fields = {
            "event",
            "step_id",
            "repair_attempt",
            "repair_reason",
            "error_evidence",
            "repaired_sql",
        }
        if set(event) != allowed_fields:
            raise RunnerError(
                "repair event fields do not match the repair contract; "
                f"missing={sorted(allowed_fields - set(event))}, "
                f"unknown={sorted(set(event) - allowed_fields)}"
            )
        if step["status"] != StepStatus.REPAIR_REQUIRED.value:
            raise RunnerError(f"current step does not accept a repair: {step['status']}")
        if not step["attempts"]:
            raise RunnerError("repair_required step has no failed attempt")
        previous_attempt = step["attempts"][-1]
        if previous_attempt.get("status") != "error" or not previous_attempt.get(
            "error"
        ):
            raise RunnerError("repair requires a preserved raw SQL error")
        repair_attempt = event.get("repair_attempt")
        expected_attempt = previous_attempt["attempt_no"] + 1
        if (
            isinstance(repair_attempt, bool)
            or not isinstance(repair_attempt, int)
            or repair_attempt != expected_attempt
        ):
            raise RunnerError(f"repair_attempt must equal {expected_attempt}")
        if repair_attempt > 2:
            raise RunnerError("at most two SQL repairs are allowed")
        repair_reason = self._required_non_empty_string(event, "repair_reason")
        error_evidence = self._required_non_empty_string(event, "error_evidence")
        repaired_sql = self._required_non_empty_string(event, "repaired_sql")
        failed_sql = (
            self._run_dir(state["run_id"]) / previous_attempt["sql_path"]
        ).read_text(encoding="utf-8")
        baseline_attempt = step["attempts"][0]
        baseline_sql = (
            self._run_dir(state["run_id"]) / baseline_attempt["sql_path"]
        ).read_text(encoding="utf-8")
        binding = self._binding_from_step(step)
        if binding is None:
            raise RunnerError("automatic step cannot accept a SQL repair")
        parameters = self._query_parameters(state, step)
        diff = self.query_builder.validate_repair(
            baseline_sql, failed_sql, repaired_sql, binding, parameters
        )
        sql_path = self._sql_relative_path(
            state["cursor"], self._post_primary_path_id(step), repair_attempt
        )
        diff_path = str(
            Path("sql")
            / (
                f"{state['cursor']:02d}-{self._post_primary_path_id(step)}-"
                f"repair-{repair_attempt}.diff"
            )
        )
        event_path = Path("events") / f"{event_hash}.json"
        run_dir = self._run_dir(state["run_id"])
        self._atomic_write_text(run_dir / sql_path, repaired_sql)
        self._atomic_write_text(run_dir / diff_path, diff)
        self._atomic_write_json(run_dir / event_path, event)
        step["attempts"].append(
            {
                "attempt_no": repair_attempt,
                "status": "issued",
                "sql_sha256": sha256_text(repaired_sql),
                "sql_path": sql_path,
                "query_id": None,
                "error": None,
                "event_path": None,
                "raw_result_sha256": None,
                "validation": None,
                "repair": {
                    "source_attempt_no": previous_attempt["attempt_no"],
                    "repair_reason": repair_reason,
                    "error_evidence": error_evidence,
                    "diff_path": diff_path,
                    "event_path": str(event_path),
                },
            }
        )
        step["status"] = StepStatus.IN_PROGRESS.value
        state["processed_events"][event_hash] = {
            "event": "repair_submitted",
            "step_id": step["id"],
            "event_path": str(event_path),
        }
        state["revision"] += 1
        self._write_state(state)
        return {
            "idempotent_replay": False,
            "event_sha256": event_hash,
            **self._status_payload(state),
        }

    def _queue_complete_ticket(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "queue_complete",
            "run_id": state["run_id"],
            "revision": state["revision"],
            "ready_for_final_validation": state["ready_for_final_validation"],
        }

    def _status_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        cursor = state["cursor"]
        current = state["steps"][cursor] if cursor < len(state["steps"]) else None
        post_query_items = []
        post_primary = state.get("post_primary")
        if isinstance(post_primary, dict):
            for post_step in post_primary.get("steps", []):
                if not isinstance(post_step, dict):
                    continue
                if post_step.get("id") == "secondary" and isinstance(
                    post_step.get("attempts"), list
                ):
                    post_query_items.append(post_step)
                elif post_step.get("id") == "game_background" and isinstance(
                    post_step.get("items"), list
                ):
                    post_query_items.extend(post_step["items"])
        if current is None:
            post_current = self._current_post_primary_query(state)
            current = post_current[1] if post_current is not None else None
        repair_count = sum(
            1
            for step in [*state["steps"], *post_query_items]
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
            "remaining_steps": (
                [step["id"] for step in state["steps"][cursor:]]
                if cursor < len(state["steps"])
                else ([current["id"]] if current is not None else [])
            ),
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
        if state.get("schema_version") != 4:
            raise RunnerError("unsupported state schema version")
        self._validate_run_id(state.get("run_id"))
        analysis_profile = state.get("analysis_profile", "primary_v1")
        expected_contract_hashes = {
            "execution_plan_sha256": self.contracts.execution_plan_sha256,
            "query_registry_sha256": self.contracts.query_registry_sha256,
            "triage_sha256": self.contracts.triage_sha256,
            "result_schemas_sha256": self.contracts.result_schemas_sha256,
            "secondary_relations_sha256": (
                self.contracts.secondary_relations_sha256
                if analysis_profile == "primary_v2"
                else None
            ),
        }
        for field, expected_hash in expected_contract_hashes.items():
            if state.get(field) != expected_hash:
                raise RunnerError(f"state {field} no longer matches the contract")
        if analysis_profile != self.analysis_profile:
            raise RunnerError("state analysis profile does not match the Host profile")
        try:
            self.contracts.analysis_profile(analysis_profile)
        except ContractError as exc:
            raise RunnerError(str(exc)) from exc
        profile_hashes = {
            "analysis_profile_sha256": self.contracts.analysis_profile_sha256(
                analysis_profile
            ),
            "post_primary_plan_sha256": (
                self.contracts.post_primary_plan_contract_sha256("post_primary_v1")
                if analysis_profile == "primary_v2"
                else None
            ),
        }
        for field, expected_hash in profile_hashes.items():
            actual = state.get(field)
            if field == "analysis_profile_sha256" and actual is None and (
                analysis_profile == "primary_v1"
            ):
                continue
            if actual != expected_hash:
                raise RunnerError(f"state {field} no longer matches the contract")
        expected_analysis_date = self._derive_analysis_date(
            state.get("chain"), state.get("alert_date")
        )
        if state.get("analysis_date") != expected_analysis_date:
            raise RunnerError("state analysis_date violates the registered date mapping")
        receipt_mode = state.get("receipt_mode")
        expected_execution_mode = {
            "trusted_host": "trusted_host_adapter",
            "self_reported": "self_reported_development",
        }.get(receipt_mode)
        if state.get("execution_mode") != expected_execution_mode:
            raise RunnerError("state receipt/execution mode is invalid")
        if receipt_mode == "trusted_host":
            if not isinstance(state.get("trusted_receipt_key_id"), str):
                raise RunnerError("trusted run lacks its receipt key identity")
        elif state.get("trusted_receipt_key_id") is not None:
            raise RunnerError("self-reported run cannot bind a trusted receipt key")
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

        known_query_ids: set[str] = set()
        successful_candidate_roots: list[dict[str, float]] = []
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
            expected_binding = self._binding_snapshot(binding)
            if step.get("binding") != expected_binding:
                raise RunnerError(f"state binding snapshot changed: {plan_step.id}")
            if step.get("binding_sha256") != canonical_sha256(expected_binding):
                raise RunnerError(f"state binding hash changed: {plan_step.id}")
            status = step.get("status")
            if status not in {member.value for member in StepStatus}:
                raise RunnerError(f"invalid step status: {status}")
            attempts = step.get("attempts")
            if not isinstance(attempts, list):
                raise RunnerError(f"step attempts must be a list: {plan_step.id}")
            for attempt_index, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    raise RunnerError(f"invalid attempt record: {plan_step.id}")
                if attempt.get("attempt_no") != attempt_index or attempt_index > 2:
                    raise RunnerError(f"attempt sequence changed: {plan_step.id}")
                if attempt.get("status") not in {
                    "issued",
                    "succeeded",
                    "failed",
                    "error",
                }:
                    raise RunnerError(f"invalid attempt status: {plan_step.id}")
                digest = attempt.get("sql_sha256")
                relative_sql_path = attempt.get("sql_path")
                if not isinstance(digest, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", digest
                ):
                    raise RunnerError(f"invalid attempt SQL hash: {plan_step.id}")
                if not isinstance(relative_sql_path, str):
                    raise RunnerError(f"invalid attempt SQL path: {plan_step.id}")
                run_dir = self._run_dir(state["run_id"]).resolve()
                sql_path = (run_dir / relative_sql_path).resolve()
                try:
                    sql_path.relative_to(run_dir)
                except ValueError as exc:
                    raise RunnerError("attempt SQL path escapes its run directory") from exc
                try:
                    sql_text = sql_path.read_text(encoding="utf-8")
                except FileNotFoundError as exc:
                    raise RunnerError(f"attempt SQL file is missing: {relative_sql_path}") from exc
                if sha256_text(sql_text) != digest:
                    raise RunnerError(f"attempt SQL file hash mismatch: {plan_step.id}")
                query_id = attempt.get("query_id")
                if query_id is not None:
                    if not isinstance(query_id, str) or not query_id.strip():
                        raise RunnerError(f"invalid query_id: {plan_step.id}")
                    if query_id in known_query_ids:
                        raise RunnerError("query_id is reused across attempts or steps")
                    known_query_ids.add(query_id)
                raw_result_hash = attempt.get("raw_result_sha256")
                if raw_result_hash is not None and (
                    not isinstance(raw_result_hash, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", raw_result_hash)
                ):
                    raise RunnerError(f"invalid raw result hash: {plan_step.id}")
                attempt_status = attempt["status"]
                if attempt_status in {"succeeded", "failed", "error"} and query_id is None:
                    raise RunnerError("completed query attempt lacks a query_id")
                if attempt_status in {"succeeded", "failed"} and raw_result_hash is None:
                    raise RunnerError("returned query attempt lacks a raw result hash")
                if attempt_status in {"issued", "error"} and raw_result_hash is not None:
                    raise RunnerError("non-returned query attempt has a raw result hash")
                if attempt_index == 0 and attempt.get("repair") is not None:
                    raise RunnerError("initial SQL attempt cannot be marked as a repair")
                if attempt_index > 0:
                    repair = attempt.get("repair")
                    if not isinstance(repair, dict) or repair.get(
                        "source_attempt_no"
                    ) != attempt_index - 1:
                        raise RunnerError(f"repair lineage changed: {plan_step.id}")
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
                candidates = step.get("candidates")
                if not isinstance(candidates, list) or len(candidates) != candidate_count:
                    raise RunnerError("succeeded step candidate details do not close")
                if not plan_step.produces_candidates and candidates:
                    raise RunnerError("diagnostic step has candidate details")
                if step.get("failure_code") is not None or step.get("reason") is not None:
                    raise RunnerError("succeeded step retains a failure classification")
                root_values = (
                    step.get("root_current_value"),
                    step.get("root_baseline_value"),
                    step.get("root_delta"),
                )
                if plan_step.produces_candidates:
                    if any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        for value in root_values
                    ):
                        raise RunnerError("candidate step lacks finite root metric facts")
                    if not math.isclose(
                        float(root_values[0]) - float(root_values[1]),
                        float(root_values[2]),
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    ):
                        raise RunnerError("candidate step root metric facts do not close")
                    successful_candidate_roots.append(
                        {
                            "current_value": float(root_values[0]),
                            "baseline_value": float(root_values[1]),
                            "delta": float(root_values[2]),
                        }
                    )
                    self._validate_private_primary_counts(
                        step,
                        required=analysis_profile == "primary_v2",
                    )
                elif any(value is not None for value in root_values):
                    raise RunnerError("diagnostic step cannot contain root metric facts")
                elif any(
                    step.get(field) is not None
                    for field in (
                        "root_current_numerator",
                        "root_current_denominator",
                        "root_baseline_numerator",
                        "root_baseline_denominator",
                        "family_adverse_impact_bp",
                    )
                ):
                    raise RunnerError("diagnostic step cannot contain private root counts")
            elif candidate_count is not None:
                raise RunnerError("non-succeeded step cannot have candidate_count")
            elif step.get("candidates") != []:
                raise RunnerError("non-succeeded step cannot have candidate details")
            elif any(
                step.get(field) is not None
                for field in (
                    "root_current_value",
                    "root_baseline_value",
                    "root_delta",
                    "root_current_numerator",
                    "root_current_denominator",
                    "root_baseline_numerator",
                    "root_baseline_denominator",
                    "family_adverse_impact_bp",
                )
            ):
                raise RunnerError("non-succeeded step cannot contain root metric facts")
            warning_codes = step.get("warning_codes")
            if not isinstance(warning_codes, list) or any(
                not isinstance(code, str) or not code.strip() for code in warning_codes
            ) or len(warning_codes) != len(set(warning_codes)):
                raise RunnerError("step warning_codes are invalid")
            if status in {
                StepStatus.FAILED.value,
                StepStatus.SKIPPED_NOT_APPLICABLE.value,
            } and (
                not isinstance(step.get("reason"), str)
                or not step["reason"].strip()
            ):
                raise RunnerError("failed/skipped step requires a reason")
            if status == StepStatus.FAILED.value and step.get("failure_code") not in {
                "schema_invalid",
                "result_incomplete",
                "contribution_not_closed",
                "quality_gate_failed",
                "query_failed",
                "query_blocked",
            }:
                raise RunnerError("failed step lacks a runner failure classification")
            if status != StepStatus.FAILED.value and step.get("failure_code") is not None:
                raise RunnerError("non-failed step has a failure classification")
            if status == StepStatus.SKIPPED_NOT_APPLICABLE.value and not (
                plan.id == "install_sandbox" and plan_step.id == "install_stage"
            ):
                raise RunnerError("illegal skipped_not_applicable step")
            if status == StepStatus.PENDING.value and attempts:
                raise RunnerError("pending step cannot already contain attempts")
            if status == StepStatus.IN_PROGRESS.value and (
                not attempts or attempts[-1]["status"] != "issued"
            ):
                raise RunnerError("in_progress step must have one issued current attempt")
            if status == StepStatus.REPAIR_REQUIRED.value and (
                not attempts
                or attempts[-1]["status"] != "error"
                or (attempts[-1].get("error") or {}).get("class")
                != "semantic_analysis"
                or attempts[-1]["attempt_no"] >= 2
            ):
                raise RunnerError("repair_required step lacks a repairable semantic error")
            if status == StepStatus.SUCCEEDED.value and (
                not attempts
                or attempts[-1]["status"] != "succeeded"
                or not isinstance(attempts[-1].get("validation"), dict)
            ):
                raise RunnerError("succeeded step lacks a succeeded current attempt")
            if status == StepStatus.FAILED.value and (
                not attempts
                or attempts[-1]["status"] not in {"failed", "error"}
                or not isinstance(attempts[-1].get("validation"), dict)
            ):
                raise RunnerError("failed step lacks a failed current attempt")
            if status == StepStatus.SKIPPED_NOT_APPLICABLE.value and attempts:
                raise RunnerError("automatic skipped step cannot contain query attempts")

        post_primary = state.get("post_primary")
        if analysis_profile == "primary_v2" and isinstance(post_primary, dict):
            self._validate_secondary_runtime_state(
                state, post_primary, known_query_ids
            )
            self._validate_game_background_runtime_state(
                state, post_primary, known_query_ids
            )

        canonical_root = state.get("canonical_root_metric")
        if state.get("receipt_mode") == "trusted_host" and not self._valid_root_metric(
            canonical_root
        ):
            raise RunnerError("trusted state lacks its root preflight canonical metric")
        if canonical_root is not None and not self._valid_root_metric(canonical_root):
            raise RunnerError("state canonical_root_metric is invalid")
        if successful_candidate_roots and canonical_root is None:
            raise RunnerError("successful family lacks a canonical root metric")
        if any(
            not self._root_metrics_match(canonical_root, root)
            for root in successful_candidate_roots
        ):
            raise RunnerError(
                "successful family does not rehook state canonical_root_metric"
            )

        if cursor < len(steps):
            if state.get("status") != RunStatus.ACTIVE.value:
                raise RunnerError("incomplete queue must remain active")
            if state.get("ready_for_final_validation") is not False:
                raise RunnerError("incomplete queue cannot be ready for final validation")
            if any(
                state.get(field) is not None
                for field in (
                    "evidence_export_sha256",
                    "final_analysis_sha256",
                    "validation_receipt",
                )
            ):
                raise RunnerError("active run contains final evidence metadata")
            if state.get("post_primary") is not None:
                raise RunnerError("active run cannot contain post-primary state")
        else:
            if state.get("status") not in {
                RunStatus.QUEUE_COMPLETE.value,
                RunStatus.FINALIZED.value,
            }:
                raise RunnerError("complete queue has an invalid run status")
            post_primary = state.get("post_primary")
            expected_ready = analysis_profile == "primary_v1" or (
                isinstance(post_primary, dict)
                and post_primary.get("status") == "completed"
            )
            if state.get("ready_for_final_validation") is not expected_ready:
                raise RunnerError(
                    "complete queue final-validation readiness is inconsistent"
                )
            evidence_hash = state.get("evidence_export_sha256")
            if evidence_hash is not None and (
                not isinstance(evidence_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash)
            ):
                raise RunnerError("invalid exported evidence hash")
            if state.get("status") == RunStatus.QUEUE_COMPLETE.value and any(
                state.get(field) is not None
                for field in ("final_analysis_sha256", "validation_receipt")
            ):
                raise RunnerError("queue_complete run contains final validation state")
            if state.get("status") == RunStatus.FINALIZED.value:
                analysis_hash = state.get("final_analysis_sha256")
                receipt = state.get("validation_receipt")
                if (
                    not isinstance(evidence_hash, str)
                    or not isinstance(analysis_hash, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", analysis_hash)
                    or not isinstance(receipt, dict)
                ):
                    raise RunnerError("finalized run lacks immutable validation evidence")
                receipt_without_hash = dict(receipt)
                receipt_hash = receipt_without_hash.pop(
                    "validation_receipt_sha256", None
                )
                if receipt_hash != canonical_sha256(receipt_without_hash):
                    raise RunnerError("validation receipt integrity check failed")
            if analysis_profile == "primary_v1":
                if post_primary is not None:
                    raise RunnerError("primary_v1 cannot contain post-primary state")
            elif post_primary is not None:
                try:
                    self.calibration_runner.planner.validate_identity(
                        state, post_primary
                    )
                    if post_primary.get("status") == "completed":
                        self.calibration_runner.execute(state)
                except (CalibrationError, ValueError) as exc:
                    raise RunnerError(str(exc)) from exc
            if isinstance(state.get("evidence_export_sha256"), str) and (
                analysis_profile == "primary_v2"
                and (
                    not isinstance(post_primary, dict)
                    or post_primary.get("status") != "completed"
                )
            ):
                raise RunnerError("exported primary_v2 run lacks completed calibration")

    def _validate_private_primary_counts(
        self, step: dict[str, Any], *, required: bool
    ) -> None:
        fields = (
            "root_current_numerator",
            "root_current_denominator",
            "root_baseline_numerator",
            "root_baseline_denominator",
            "family_adverse_impact_bp",
        )
        values = [step.get(field) for field in fields]
        if all(value is None for value in values):
            if required:
                raise RunnerError("primary_v2 step lacks private root counts")
            return
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in values
        ):
            raise RunnerError("primary step private root counts are invalid")
        normalized = list(map(float, values))
        (
            current_numerator,
            current_denominator,
            baseline_numerator,
            baseline_denominator,
            _,
        ) = normalized
        if current_denominator <= 0 or baseline_denominator <= 0:
            raise RunnerError("primary step private root denominators must be positive")
        if not math.isclose(
            current_numerator / current_denominator,
            float(step["root_current_value"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ) or not math.isclose(
            baseline_numerator / baseline_denominator,
            float(step["root_baseline_value"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RunnerError("primary step private root counts do not match root rates")
        for candidate in step["candidates"]:
            counts = candidate.get("private_counts")
            if not isinstance(counts, dict) or set(counts) != {
                "current_numerator",
                "current_denominator",
                "baseline_numerator",
                "baseline_denominator",
            }:
                raise RunnerError("primary candidate private counts are invalid")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
                for value in counts.values()
            ):
                raise RunnerError("primary candidate private counts are invalid")

    def _validate_secondary_runtime_state(
        self,
        state: dict[str, Any],
        post_primary: dict[str, Any],
        known_query_ids: set[str],
    ) -> None:
        secondary = next(
            (
                step
                for step in post_primary.get("steps", [])
                if step.get("id") == "secondary"
            ),
            None,
        )
        if not isinstance(secondary, dict):
            raise RunnerError("post-primary state lacks secondary")
        counterfactual = post_primary.get("steps", [None])[0]
        if not isinstance(counterfactual, dict) or counterfactual.get("status") == (
            "planned"
        ):
            if secondary != {"id": "secondary", "status": "planned"}:
                raise RunnerError(
                    "secondary cannot advance before counterfactual is frozen"
                )
            return
        try:
            expected_selection = self.calibration_runner.secondary_selector.select(
                state, post_primary
            )
        except ValueError as exc:
            raise RunnerError(str(exc)) from exc
        if expected_selection["status"] == "skipped_by_policy":
            if secondary != {"id": "secondary", **expected_selection}:
                raise RunnerError("secondary skip no longer matches primary evidence")
            return
        selection_fields = (
            "parent_dimension",
            "parent_value",
            "parent_label",
            "parent_candidate_id",
            "child_dimension",
            "parent_selection_reason",
            "child_selection_reason",
        )
        if any(
            secondary.get(field) != expected_selection.get(field)
            for field in selection_fields
        ):
            raise RunnerError("secondary selection no longer matches primary evidence")
        if secondary.get("status") == "planned":
            allowed = {"id", "status", *selection_fields}
            if set(secondary) != allowed:
                raise RunnerError("planned secondary contains runtime evidence")
            return

        expected_binding = self.contracts.secondary_binding(
            chain=state["chain"],
            metric=state["metric"],
            parent_dimension=secondary["parent_dimension"],
            parent_value=secondary["parent_value"],
            child_dimension=secondary["child_dimension"],
        )
        expected_snapshot = self._binding_snapshot(expected_binding)
        if secondary.get("binding") != expected_snapshot or secondary.get(
            "binding_sha256"
        ) != canonical_sha256(expected_snapshot):
            raise RunnerError("secondary binding no longer matches the registry")
        attempts = secondary.get("attempts")
        if not isinstance(attempts, list) or not attempts or len(attempts) > 3:
            raise RunnerError("secondary must contain one query with at most two repairs")
        run_dir = self._run_dir(state["run_id"]).resolve()
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict) or attempt.get("attempt_no") != index:
                raise RunnerError("secondary attempt sequence changed")
            sql_path_value = attempt.get("sql_path")
            digest = attempt.get("sql_sha256")
            if (
                not isinstance(sql_path_value, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise RunnerError("secondary attempt SQL identity is invalid")
            sql_path = (run_dir / sql_path_value).resolve()
            try:
                sql_path.relative_to(run_dir)
                sql = sql_path.read_text(encoding="utf-8")
            except (ValueError, FileNotFoundError) as exc:
                raise RunnerError("secondary attempt SQL is missing") from exc
            if sha256_text(sql) != digest:
                raise RunnerError("secondary attempt SQL hash changed")
            self.query_builder.validate_sql(
                sql,
                expected_binding,
                {
                    "business_date": state["analysis_date"],
                    "game_type": state["game_type"],
                },
            )
            query_id = attempt.get("query_id")
            if query_id is not None:
                if not isinstance(query_id, str) or not query_id.strip():
                    raise RunnerError("secondary query_id is invalid")
                if query_id in known_query_ids:
                    raise RunnerError("query_id is reused by secondary")
                known_query_ids.add(query_id)
            event_path_value = attempt.get("event_path")
            if query_id is None:
                if event_path_value is not None:
                    raise RunnerError("unexecuted secondary attempt has an event")
                continue
            if not isinstance(event_path_value, str):
                raise RunnerError("executed secondary attempt lacks its event")
            event_path = (run_dir / event_path_value).resolve()
            try:
                event_path.relative_to(run_dir)
                event = json.loads(event_path.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise RunnerError("secondary receipt event is unavailable") from exc
            event_hash = canonical_sha256(event)
            if event_path.name != f"{event_hash}.json" or state[
                "processed_events"
            ].get(event_hash, {}).get("event_path") != event_path_value:
                raise RunnerError("secondary receipt event identity changed")
            if event.get("query_id") != query_id or event.get(
                "submitted_sql_sha256"
            ) != digest:
                raise RunnerError("secondary receipt does not match its attempt")
            self._verify_receipt(state, event)
            if event.get("event") == "query_returned":
                raw_result = event.get("raw_result")
                raw_hash = event.get("raw_result_sha256")
                if (
                    not isinstance(raw_result, dict)
                    or raw_hash != canonical_sha256(raw_result)
                    or attempt.get("raw_result_sha256") != raw_hash
                ):
                    raise RunnerError("secondary raw result evidence changed")
            elif event.get("event") == "query_error":
                event_error = {
                    "class": event.get("error_class"),
                    "code": event.get("error_code"),
                    "message": event.get("error_message"),
                }
                if attempt.get("error") != event_error:
                    raise RunnerError("secondary query error evidence changed")
            else:
                raise RunnerError("secondary receipt event type is invalid")

        status = secondary.get("status")
        latest = attempts[-1]
        if status == "in_progress":
            if latest.get("status") != "issued" or latest.get("query_id") is not None:
                raise RunnerError("in_progress secondary is not awaiting a query")
            return
        if status == "repair_required":
            error = latest.get("error") or {}
            if (
                latest.get("status") != "error"
                or error.get("class") != "semantic_analysis"
                or latest["attempt_no"] >= 2
            ):
                raise RunnerError("secondary repair state is invalid")
            return
        if status not in {"succeeded", "failed"}:
            raise RunnerError(f"secondary runtime status is invalid: {status}")

        if latest.get("status") in {"succeeded", "failed"}:
            event = json.loads(
                (run_dir / latest["event_path"]).read_text(encoding="utf-8")
            )
            try:
                outcome = self.secondary_result_validator.validate(
                    raw_result=event["raw_result"],
                    binding=expected_binding,
                    chain=state["chain"],
                    metric=state["metric"],
                    analysis_date=state["analysis_date"],
                    game_type=state["game_type"],
                    parent_value=secondary["parent_value"],
                    parent_counts=self._secondary_parent_counts(state, secondary),
                    root_counts=self._secondary_root_counts(state),
                )
            except ResultValidationError as exc:
                if status != "failed" or latest.get("validation") != {
                    "status": "failed",
                    "failure_code": exc.code,
                    "reason": str(exc),
                }:
                    raise RunnerError(
                        "secondary validation failure no longer matches raw evidence"
                    ) from exc
                if secondary.get("failure_code") != exc.code or secondary.get(
                    "reason"
                ) != f"{exc.code}: {exc}":
                    raise RunnerError(
                        "secondary failure classification changed"
                    ) from exc
                return
            if status != "succeeded" or latest.get("status") != "succeeded":
                raise RunnerError("secondary status contradicts valid raw evidence")
            expected_fields = {
                "candidate_count": outcome.candidate_count,
                "candidates": list(outcome.candidates),
                "root_current_value": outcome.root_current_value,
                "root_baseline_value": outcome.root_baseline_value,
                "root_delta": outcome.root_delta,
                "root_current_numerator": outcome.root_current_numerator,
                "root_current_denominator": outcome.root_current_denominator,
                "root_baseline_numerator": outcome.root_baseline_numerator,
                "root_baseline_denominator": outcome.root_baseline_denominator,
                "family_adverse_impact_bp": outcome.family_adverse_impact_bp,
                "warning_codes": list(outcome.warning_codes),
                "failure_code": None,
                "reason": None,
            }
            if any(
                canonical_sha256(secondary.get(field))
                != canonical_sha256(expected)
                for field, expected in expected_fields.items()
            ):
                raise RunnerError(
                    "secondary result no longer matches its raw evidence"
                )
            return

        if latest.get("status") != "error" or status != "failed":
            raise RunnerError("terminal secondary query error state is invalid")
        raw_error = latest.get("error")
        repair_suffix = (
            " after two evidence-based repairs"
            if isinstance(raw_error, dict)
            and raw_error.get("class") == "semantic_analysis"
            and latest["attempt_no"] == 2
            else ""
        )
        expected_reason = (
            f"{raw_error['class']} {raw_error['code']}{repair_suffix}: "
            f"{raw_error['message']}"
            if isinstance(raw_error, dict)
            and all(key in raw_error for key in ("class", "code", "message"))
            else None
        )
        if (
            not isinstance(raw_error, dict)
            or secondary.get("failure_code") != self._query_failure_code(raw_error)
            or secondary.get("reason") != expected_reason
            or latest.get("validation")
            != {
                "status": "failed",
                "failure_code": self._query_failure_code(raw_error),
                "reason": expected_reason,
            }
        ):
            raise RunnerError("secondary query failure classification changed")

    def _validate_game_background_runtime_state(
        self,
        state: dict[str, Any],
        post_primary: dict[str, Any],
        known_query_ids: set[str],
    ) -> None:
        background = next(
            (
                step
                for step in post_primary.get("steps", [])
                if step.get("id") == "game_background"
            ),
            None,
        )
        secondary = next(
            (
                step
                for step in post_primary.get("steps", [])
                if step.get("id") == "secondary"
            ),
            None,
        )
        if not isinstance(background, dict) or not isinstance(secondary, dict):
            raise RunnerError("post-primary state lacks game background")
        if secondary.get("status") in {
            "planned",
            "in_progress",
            "repair_required",
        }:
            if background != {"id": "game_background", "status": "planned"}:
                raise RunnerError(
                    "game background cannot advance before secondary is terminal"
                )
            return
        try:
            selection = self.calibration_runner.game_background_selector.select(
                state, post_primary
            )
        except ValueError as exc:
            raise RunnerError(str(exc)) from exc
        if selection["status"] == "skipped_by_policy":
            if background != {"id": "game_background", **selection}:
                raise RunnerError(
                    "game background skip no longer matches primary evidence"
                )
            return
        if background.get("selected_games") != selection["selected_games"]:
            raise RunnerError(
                "game background selection no longer matches primary evidence"
            )
        items = background.get("items")
        cursor = background.get("cursor")
        if (
            not isinstance(items, list)
            or len(items) != len(selection["items"])
            or isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not 0 <= cursor <= len(items)
        ):
            raise RunnerError("game background cursor or items changed")

        for index, (item, expected_item) in enumerate(
            zip(items, selection["items"], strict=True)
        ):
            if not isinstance(item, dict):
                raise RunnerError("game background item must be an object")
            for field in (
                "id",
                "item_index",
                "candidate_id",
                "game_id",
                "label",
                "selection_reason",
            ):
                if item.get(field) != expected_item[field]:
                    raise RunnerError(
                        f"game background item {field} changed"
                    )
            status = item.get("status")
            if status not in {
                "planned",
                "in_progress",
                "repair_required",
                "succeeded",
                "failed",
            }:
                raise RunnerError("game background item status is invalid")
            if index < cursor and status not in {"succeeded", "failed"}:
                raise RunnerError("completed game background item is not terminal")
            if index > cursor and status != "planned":
                raise RunnerError("future game background item advanced early")
            if index == cursor and cursor < len(items) and status not in {
                "planned",
                "in_progress",
                "repair_required",
            }:
                raise RunnerError("current game background item is terminal")
            self._validate_game_background_item(
                state, item, known_query_ids
            )

        if cursor < len(items):
            expected_status = "planned" if cursor == 0 and items[0][
                "status"
            ] == "planned" else "in_progress"
            if background.get("status") != expected_status:
                raise RunnerError("game background execution status is inconsistent")
            if any(field in background for field in ("failure_code", "reason")):
                raise RunnerError(
                    "executing game background contains aggregate failure fields"
                )
            return

        has_success = any(item["status"] == "succeeded" for item in items)
        expected_status = "succeeded" if has_success else "failed"
        if background.get("status") != expected_status:
            raise RunnerError("game background aggregate status is inconsistent")
        if has_success:
            if any(field in background for field in ("failure_code", "reason")):
                raise RunnerError(
                    "succeeded game background contains aggregate failure fields"
                )
        elif (
            background.get("failure_code") != "game_background_unavailable"
            or background.get("reason")
            != "all selected game background queries failed"
        ):
            raise RunnerError("game background aggregate failure changed")

    def _validate_game_background_item(
        self,
        state: dict[str, Any],
        item: dict[str, Any],
        known_query_ids: set[str],
    ) -> None:
        status = item["status"]
        attempts = item.get("attempts")
        if status == "planned":
            if item.get("binding") is not None or item.get(
                "binding_sha256"
            ) is not None or attempts != []:
                raise RunnerError("planned game background item has query evidence")
            if (
                item.get("facts") != []
                or item.get("limit_codes") != []
                or item.get("failure_code") is not None
                or item.get("reason") is not None
            ):
                raise RunnerError("planned game background item has result evidence")
            return

        expected_binding = self.contracts.game_background_binding(
            game_id=int(item["game_id"])
        )
        expected_snapshot = self._binding_snapshot(expected_binding)
        if item.get("binding") != expected_snapshot or item.get(
            "binding_sha256"
        ) != canonical_sha256(expected_snapshot):
            raise RunnerError("game background binding changed")
        if not isinstance(attempts, list) or not attempts or len(attempts) > 3:
            raise RunnerError(
                "game background item must contain one query with at most two repairs"
            )
        run_dir = self._run_dir(state["run_id"]).resolve()
        for attempt_index, attempt in enumerate(attempts):
            if (
                not isinstance(attempt, dict)
                or attempt.get("attempt_no") != attempt_index
            ):
                raise RunnerError("game background attempt sequence changed")
            sql_path_value = attempt.get("sql_path")
            digest = attempt.get("sql_sha256")
            if (
                not isinstance(sql_path_value, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise RunnerError("game background SQL identity is invalid")
            sql_path = (run_dir / sql_path_value).resolve()
            try:
                sql_path.relative_to(run_dir)
                sql = sql_path.read_text(encoding="utf-8")
            except (ValueError, OSError) as exc:
                raise RunnerError("game background SQL is missing") from exc
            if sha256_text(sql) != digest:
                raise RunnerError("game background SQL hash changed")
            self.query_builder.validate_sql(
                sql,
                expected_binding,
                {
                    "business_date": state["analysis_date"],
                    "game_id": int(item["game_id"]),
                },
            )
            query_id = attempt.get("query_id")
            if query_id is not None:
                if not isinstance(query_id, str) or not query_id.strip():
                    raise RunnerError("game background query_id is invalid")
                if query_id in known_query_ids:
                    raise RunnerError("query_id is reused by game background")
                known_query_ids.add(query_id)
            event_path_value = attempt.get("event_path")
            if query_id is None:
                if event_path_value is not None:
                    raise RunnerError(
                        "unexecuted game background attempt has an event"
                    )
                continue
            if not isinstance(event_path_value, str):
                raise RunnerError(
                    "executed game background attempt lacks its event"
                )
            event_path = (run_dir / event_path_value).resolve()
            try:
                event_path.relative_to(run_dir)
                event = json.loads(event_path.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise RunnerError(
                    "game background receipt event is unavailable"
                ) from exc
            event_hash = canonical_sha256(event)
            if event_path.name != f"{event_hash}.json" or state[
                "processed_events"
            ].get(event_hash, {}).get("event_path") != event_path_value:
                raise RunnerError("game background receipt identity changed")
            if event.get("query_id") != query_id or event.get(
                "submitted_sql_sha256"
            ) != digest:
                raise RunnerError(
                    "game background receipt does not match its attempt"
                )
            self._verify_receipt(state, event)
            if event.get("event") == "query_returned":
                raw_result = event.get("raw_result")
                raw_hash = event.get("raw_result_sha256")
                if (
                    not isinstance(raw_result, dict)
                    or raw_hash != canonical_sha256(raw_result)
                    or attempt.get("raw_result_sha256") != raw_hash
                ):
                    raise RunnerError(
                        "game background raw result evidence changed"
                    )
            elif event.get("event") == "query_error":
                event_error = {
                    "class": event.get("error_class"),
                    "code": event.get("error_code"),
                    "message": event.get("error_message"),
                }
                if attempt.get("error") != event_error:
                    raise RunnerError(
                        "game background query error evidence changed"
                    )
            else:
                raise RunnerError("game background receipt event type is invalid")

        latest = attempts[-1]
        if status == "in_progress":
            if latest.get("status") != "issued" or latest.get("query_id") is not None:
                raise RunnerError(
                    "in_progress game background item is not awaiting a query"
                )
            return
        if status == "repair_required":
            error = latest.get("error") or {}
            if (
                latest.get("status") != "error"
                or error.get("class") != "semantic_analysis"
                or latest["attempt_no"] >= 2
            ):
                raise RunnerError("game background repair state is invalid")
            return
        if status not in {"succeeded", "failed"}:
            raise RunnerError("game background terminal status is invalid")

        if latest.get("status") in {"succeeded", "failed"}:
            event = json.loads(
                (run_dir / latest["event_path"]).read_text(encoding="utf-8")
            )
            try:
                outcome = self.game_background_validator.validate(
                    raw_result=event["raw_result"],
                    binding=expected_binding,
                    analysis_date=state["analysis_date"],
                    game_id=int(item["game_id"]),
                )
            except GameBackgroundValidationError as exc:
                expected_reason = f"{exc.code}: {exc}"
                if (
                    status != "failed"
                    or item.get("facts") != []
                    or item.get("limit_codes") != []
                    or item.get("failure_code") != exc.code
                    or item.get("reason") != expected_reason
                    or latest.get("validation")
                    != {
                        "status": "failed",
                        "failure_code": exc.code,
                        "reason": str(exc),
                    }
                ):
                    raise RunnerError(
                        "game background validation failure changed"
                    ) from exc
                return
            expected_validation = {
                "status": "succeeded",
                "fact_count": len(outcome.facts),
                "limit_codes": list(outcome.limit_codes),
            }
            if (
                status != "succeeded"
                or latest.get("status") != "succeeded"
                or latest.get("validation") != expected_validation
                or item.get("facts") != list(outcome.facts)
                or item.get("limit_codes") != list(outcome.limit_codes)
                or item.get("failure_code") is not None
                or item.get("reason") is not None
            ):
                raise RunnerError(
                    "game background result no longer matches raw evidence"
                )
            return

        if latest.get("status") != "error" or status != "failed":
            raise RunnerError("game background query error state is invalid")
        raw_error = latest.get("error")
        repair_suffix = (
            " after two evidence-based repairs"
            if isinstance(raw_error, dict)
            and raw_error.get("class") == "semantic_analysis"
            and latest["attempt_no"] == 2
            else ""
        )
        expected_reason = (
            f"{raw_error['class']} {raw_error['code']}{repair_suffix}: "
            f"{raw_error['message']}"
            if isinstance(raw_error, dict)
            and all(key in raw_error for key in ("class", "code", "message"))
            else None
        )
        if (
            not isinstance(raw_error, dict)
            or item.get("facts") != []
            or item.get("limit_codes") != []
            or item.get("failure_code") != self._query_failure_code(raw_error)
            or item.get("reason") != expected_reason
            or latest.get("validation")
            != {
                "status": "failed",
                "failure_code": self._query_failure_code(raw_error),
                "reason": expected_reason,
            }
        ):
            raise RunnerError(
                "game background query failure classification changed"
            )

    def _write_state(self, state: dict[str, Any]) -> None:
        state.pop("integrity_sha256", None)
        state["integrity_sha256"] = canonical_sha256(state)
        self._atomic_write_json(self._state_path(state["run_id"]), state)

    def _mark_queue_complete(self, state: dict[str, Any]) -> None:
        state["status"] = RunStatus.QUEUE_COMPLETE.value
        state["ready_for_final_validation"] = (
            state.get("analysis_profile", "primary_v1") == "primary_v1"
        )

    def _require_submitted_hash(
        self, event: dict[str, Any], attempt: dict[str, Any]
    ) -> None:
        submitted_hash = self._required_non_empty_string(
            event, "submitted_sql_sha256"
        )
        if submitted_hash != attempt["sql_sha256"]:
            raise RunnerError("submitted SQL hash does not match the issued ticket")

    def _validate_record_event_fields(
        self, event_type: str, event: dict[str, Any], state: dict[str, Any]
    ) -> None:
        common = {
            "event",
            "step_id",
            "attempt_no",
            "receipt_type",
            "submitted_sql_sha256",
            "query_id",
        }
        if event_type == "query_returned":
            allowed = common | {"raw_result", "raw_result_sha256"}
        else:
            allowed = common | {"error_class", "error_code", "error_message"}
        if state["receipt_mode"] == "trusted_host":
            allowed |= {"receipt_key_id", "receipt_id", "receipt_signature"}
        unknown = sorted(set(event) - allowed)
        missing = sorted(allowed - set(event))
        if unknown or missing:
            raise RunnerError(
                f"record event fields do not match the receipt contract; "
                f"missing={missing}, unknown={unknown}"
            )

    def _verify_receipt(
        self, state: dict[str, Any], event: dict[str, Any]
    ) -> None:
        expected_type = {
            "trusted_host": "trusted_host_receipt",
            "self_reported": "self_reported_receipt",
        }[state["receipt_mode"]]
        if event.get("receipt_type") != expected_type:
            raise RunnerError(f"receipt_type must be {expected_type}")
        if state["receipt_mode"] == "self_reported":
            return
        verifier = self.trusted_receipt_verifier
        if verifier is None or verifier.key_id != state["trusted_receipt_key_id"]:
            raise RunnerError("trusted Host receipt verifier is unavailable")
        try:
            verifier.verify(state["run_id"], event)
        except ReceiptVerificationError as exc:
            raise RunnerError(str(exc)) from exc

    def _ensure_unique_query_id(
        self, state: dict[str, Any], step_id: str, query_id: str
    ) -> None:
        for step in state["steps"]:
            for attempt in step["attempts"]:
                if attempt.get("query_id") == query_id:
                    raise RunnerError(
                        f"query_id is already bound to step {step['id']}; "
                        f"cannot bind it to {step_id}"
                    )
        post_primary = state.get("post_primary")
        if isinstance(post_primary, dict):
            for post_step in post_primary.get("steps", []):
                query_items = (
                    post_step.get("items", [])
                    if post_step.get("id") == "game_background"
                    else [post_step]
                )
                for attempt in (
                    attempt
                    for item in query_items
                    for attempt in item.get("attempts", [])
                ):
                    if attempt.get("query_id") == query_id:
                        raise RunnerError(
                            f"query_id is already bound to post-primary step "
                            f"{post_step['id']}; cannot bind it to {step_id}"
                        )

    def _required_sha256(self, value: dict[str, Any], key: str) -> str:
        result = self._required_non_empty_string(value, key)
        if not re.fullmatch(r"[0-9a-f]{64}", result):
            raise RunnerError(f"{key} must be a lowercase SHA-256")
        return result

    def _query_failure_code(self, raw_error: dict[str, str]) -> str:
        combined = " ".join(raw_error.values()).lower()
        if any(
            marker in combined
            for marker in ("permission", "access denied", "unauthorized", "forbidden")
        ):
            return "query_blocked"
        return "query_failed"

    def _validate_canonical_root_metric(
        self, state: dict[str, Any], step: dict[str, Any], outcome: Any
    ) -> None:
        if not step["produces_candidates"]:
            return
        root = {
            "current_value": outcome.root_current_value,
            "baseline_value": outcome.root_baseline_value,
            "delta": outcome.root_delta,
        }
        if not self._valid_root_metric(root):
            raise RunnerError("validated candidate family lacks finite root metric facts")
        canonical = state.get("canonical_root_metric")
        if canonical is None:
            if state.get("receipt_mode") == "trusted_host":
                raise RunnerError("trusted run cannot infer its root from a family")
            state["canonical_root_metric"] = root
        elif not self._root_metrics_match(canonical, root):
            raise ResultValidationError(
                "result_incomplete",
                "root metric does not rehook the canonical investigation root",
            )

    def _valid_root_metric(self, root: Any) -> bool:
        return (
            isinstance(root, dict)
            and set(root) == {"current_value", "baseline_value", "delta"}
            and all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                for value in root.values()
            )
        )

    def _root_metrics_match(
        self, canonical: dict[str, Any], candidate: dict[str, Any]
    ) -> bool:
        if not self._valid_root_metric(canonical) or not self._valid_root_metric(
            candidate
        ):
            return False
        tolerance = float(self.contracts.result_defaults["contribution_tolerance"])
        return all(
            math.isclose(
                float(canonical[field]),
                float(candidate[field]),
                rel_tol=0.0,
                abs_tol=tolerance,
            )
            for field in ("current_value", "baseline_value", "delta")
        )

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

    def _binding_snapshot(self, binding: QueryBinding | None) -> dict[str, Any] | None:
        if binding is None:
            return None
        return {
            "asset_path": binding.asset_path,
            "asset_sha256": binding.asset_sha256,
            "asset_kind": binding.asset_kind,
            "data_sources": list(binding.data_sources),
            "protected_tokens": list(binding.protected_tokens),
            "required_predicates": list(binding.required_predicates),
            "result_schema_id": binding.result_schema_id,
            "dimension": binding.dimension,
            "dimension_config": binding.dimension_config,
        }

    def _binding_from_step(self, step: dict[str, Any]) -> QueryBinding | None:
        snapshot = step.get("binding")
        if snapshot is None:
            if step.get("binding_sha256") != canonical_sha256(None):
                raise RunnerError("automatic step binding hash changed")
            return None
        if not isinstance(snapshot, dict) or step.get(
            "binding_sha256"
        ) != canonical_sha256(snapshot):
            raise RunnerError("immutable step binding failed its checksum")
        try:
            return QueryBinding(
                asset_path=snapshot["asset_path"],
                asset_sha256=snapshot["asset_sha256"],
                asset_kind=snapshot["asset_kind"],
                data_sources=tuple(snapshot["data_sources"]),
                protected_tokens=tuple(snapshot["protected_tokens"]),
                required_predicates=tuple(snapshot["required_predicates"]),
                result_schema_id=snapshot["result_schema_id"],
                dimension=snapshot.get("dimension"),
                dimension_config=snapshot.get("dimension_config"),
            )
        except (KeyError, TypeError) as exc:
            raise RunnerError("immutable step binding is malformed") from exc

    def _query_parameters(
        self, state: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        binding = self._binding_from_step(step)
        if binding is None:
            raise RunnerError("query parameters require an immutable binding")
        config = binding.dimension_config or {}
        if config.get("post_primary") == "game_background":
            game_id = config.get("game_id")
            step_game_id = step.get("game_id")
            if (
                not isinstance(step_game_id, str)
                or not step_game_id.isdigit()
                or game_id != int(step_game_id)
            ):
                raise RunnerError("game background binding changed its game identity")
            return {
                "business_date": state["analysis_date"],
                "game_id": game_id,
            }
        parameters = {
            "business_date": state["analysis_date"],
            "game_type": state["game_type"],
        }
        if config.get("secondary") is True:
            parameters.update(
                {
                    "parent_dimension": config["parent_dimension"],
                    "parent_value": config["parent_value"],
                    "child_dimension": config["child_dimension"],
                }
            )
        return parameters

    @staticmethod
    def _post_primary_path_id(step: dict[str, Any]) -> str:
        item_index = step.get("item_index")
        if isinstance(item_index, int):
            return f"{step['id']}-{item_index:02d}"
        return step["id"]

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
        if not isinstance(value, str):
            raise RunnerError(f"{field} must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise RunnerError(f"{field} must use YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise RunnerError(f"{field} must use YYYY-MM-DD")

    def _derive_analysis_date(self, chain: Any, alert_date: Any) -> str:
        self._validate_date(alert_date, "alert_date")
        parsed = date.fromisoformat(alert_date)
        if chain == "download":
            return parsed.isoformat()
        if chain == "install":
            return (parsed - timedelta(days=2)).isoformat()
        raise RunnerError(f"unsupported chain for date mapping: {chain}")

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
        raise RunnerError(f"input is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError("input must contain a JSON object")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m runtime.runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("verify-assets")

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--chain", required=True, choices=("download", "install"))
    init_parser.add_argument("--game-type", required=True, choices=("app", "sandbox"))
    init_parser.add_argument("--metric", required=True)
    init_parser.add_argument("--alert-date", required=True)
    init_parser.add_argument("--analysis-date")
    init_parser.add_argument(
        "--receipt-mode",
        choices=("trusted_host", "self_reported"),
        default="trusted_host",
    )
    init_parser.add_argument("--resume", action="store_true")

    for command in ("next", "status", "export", "writer-pack"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--run-id", required=True)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--run-id", required=True)
    record_parser.add_argument("--event-file", default="-")
    validate_parser = subparsers.add_parser("validate-final")
    validate_parser.add_argument("--run-id", required=True)
    validate_parser.add_argument("--analysis-json", required=True)
    validate_parser.add_argument("--investigation-index", type=int, required=True)
    assemble_parser = subparsers.add_parser("assemble-final")
    assemble_parser.add_argument("--run-id", required=True)
    assemble_parser.add_argument("--writer-patch", required=True)
    assemble_parser.add_argument("--analysis-context", required=True)
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
                receipt_mode=args.receipt_mode,
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
        elif args.command == "writer-pack":
            result = runner.build_writer_pack(args.run_id)
        elif args.command == "assemble-final":
            result = runner.assemble_final(
                args.run_id,
                _read_json_input(args.writer_patch),
                _read_json_input(args.analysis_context),
            )
        elif args.command == "validate-final":
            result = runner.validate_final(
                args.run_id,
                args.analysis_json,
                args.investigation_index,
            )
        else:  # pragma: no cover - argparse prevents this branch
            raise RunnerError(f"unknown command: {args.command}")
    except (
        ContractError,
        EvidencePackError,
        FinalAssemblyError,
        FinalValidationError,
        QueryBuildError,
        RunnerError,
        OSError,
    ) as exc:
        print(
            json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
