from __future__ import annotations

from collections import Counter
import math
from typing import Any

from .contracts import canonical_sha256
from .evidence_pack import EvidencePackBuilder, EvidencePackError
from .models import StepStatus, TERMINAL_STEP_STATUSES


class FinalValidationError(ValueError):
    pass


class FinalEvidenceValidator:
    ALLOWED_FULL_QUEUE_STATUSES = {
        "completed",
        "no_dominant_slice",
        "insufficient_data",
        "query_blocked",
        "query_failed",
        "unsupported_drilldown",
    }

    def validate(
        self,
        state: dict[str, Any],
        analysis: dict[str, Any],
        investigation_index: int,
    ) -> dict[str, Any]:
        if state.get("cursor") != len(state.get("steps", [])):
            raise FinalValidationError("fixed attribution queue is not complete")
        if any(
            step.get("status") not in TERMINAL_STEP_STATUSES
            for step in state["steps"]
        ):
            raise FinalValidationError("run contains a non-terminal queue step")
        if not isinstance(analysis, dict):
            raise FinalValidationError("analysis JSON must contain an object")
        investigations = analysis.get("investigations")
        if not isinstance(investigations, list):
            raise FinalValidationError("analysis.investigations must be an array")
        if (
            isinstance(investigation_index, bool)
            or not isinstance(investigation_index, int)
            or not 0 <= investigation_index < len(investigations)
        ):
            raise FinalValidationError("investigation_index is out of range")
        investigation = investigations[investigation_index]
        if not isinstance(investigation, dict):
            raise FinalValidationError("selected investigation must be an object")
        investigation_status = investigation.get("status")
        if investigation_status not in self.ALLOWED_FULL_QUEUE_STATUSES:
            raise FinalValidationError(
                f"unknown full_queue investigation status: {investigation_status}"
            )
        if investigation.get("metric") != state["metric"]:
            raise FinalValidationError("investigation metric does not match the run")
        if investigation.get("analysis_date") != state["analysis_date"]:
            raise FinalValidationError(
                "investigation analysis_date does not match the run"
            )
        if investigation_status in {"completed", "no_dominant_slice"}:
            try:
                root_metric = EvidencePackBuilder().root_metric(state)
            except EvidencePackError as exc:
                raise FinalValidationError(str(exc)) from exc
            if root_metric is None:
                raise FinalValidationError("successful investigation lacks root facts")
            for field, expected in root_metric.items():
                actual = investigation.get(field)
                if (
                    isinstance(actual, bool)
                    or not isinstance(actual, (int, float))
                    or not math.isfinite(float(actual))
                    or not math.isclose(
                        float(actual),
                        float(expected),
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                ):
                    raise FinalValidationError(
                        f"investigation {field} does not match frozen root facts"
                    )
            self._require_text(investigation, "summary")
            self._require_text(investigation, "recommended_action")
            self._validate_text_array(investigation, "evidence_limits")
        else:
            self._require_text(investigation, "reason")
            self._require_text(investigation, "action")
            if "evidence_limits" in investigation:
                self._validate_text_array(investigation, "evidence_limits")

        execution = investigation.get("attribution_execution")
        if not isinstance(execution, dict):
            raise FinalValidationError("investigation lacks attribution_execution")
        if execution.get("mode") != "full_queue":
            raise FinalValidationError("attribution_execution.mode must be full_queue")
        if execution.get("chain") != state["chain"]:
            raise FinalValidationError("attribution chain does not match the run")
        if execution.get("game_type") != state["game_type"]:
            raise FinalValidationError("attribution game_type does not match the run")
        if execution.get("execution_mode") != state["execution_mode"]:
            raise FinalValidationError(
                "attribution execution_mode does not match the run"
            )

        actual_steps = execution.get("steps")
        if not isinstance(actual_steps, list) or len(actual_steps) != len(
            state["steps"]
        ):
            raise FinalValidationError("attribution step count does not match the fixed queue")
        known_query_ids = self._known_query_ids(state)
        candidate_counts: dict[str, int] = {}
        candidate_details: dict[str, list[dict[str, Any]]] = {}
        candidate_successes: set[str] = set()
        candidate_failures: set[str] = set()

        for index, (actual, expected) in enumerate(
            zip(actual_steps, state["steps"], strict=True)
        ):
            if not isinstance(actual, dict):
                raise FinalValidationError(f"attribution step {index} must be an object")
            if actual.get("step") != expected["id"]:
                raise FinalValidationError("attribution steps are missing or reordered")
            if actual.get("status") != expected["status"]:
                raise FinalValidationError(
                    f"step status mismatch for {expected['id']}"
                )
            status = expected["status"]
            if status == StepStatus.SUCCEEDED.value:
                if actual.get("candidate_count") != expected["candidate_count"]:
                    raise FinalValidationError(
                        f"candidate_count mismatch for {expected['id']}"
                    )
                if "reason" in actual:
                    raise FinalValidationError(
                        f"succeeded step cannot contain reason: {expected['id']}"
                    )
                if expected["produces_candidates"]:
                    candidate_counts[expected["id"]] = expected["candidate_count"]
                    candidate_details[expected["id"]] = expected["candidates"]
                    candidate_successes.add(expected["id"])
            else:
                if "candidate_count" in actual:
                    raise FinalValidationError(
                        f"non-succeeded step cannot contain candidate_count: {expected['id']}"
                    )
                if actual.get("reason") != expected["reason"]:
                    raise FinalValidationError(f"reason mismatch for {expected['id']}")
                if expected["produces_candidates"]:
                    candidate_failures.add(expected["id"])
            expected_query_id = self._last_query_id(expected)
            query_id = actual.get("query_id")
            if query_id != expected_query_id:
                raise FinalValidationError(
                    f"query_id mismatch for {expected['id']}: {query_id}"
                )
            if actual.get("warning_codes", []) != expected["warning_codes"]:
                raise FinalValidationError(
                    f"warning_codes mismatch for {expected['id']}"
                )

        for query_id in self._query_ids_in(investigation):
            if query_id not in known_query_ids:
                raise FinalValidationError(f"query_id is not recorded by this run: {query_id}")

        top_findings = investigation.get("top_findings", [])
        if not isinstance(top_findings, list):
            raise FinalValidationError("top_findings must be an array when present")
        finding_counts: Counter[str] = Counter()
        for finding in top_findings:
            if not isinstance(finding, dict):
                raise FinalValidationError("each top finding must be an object")
            self._require_text(finding, "finding")
            if finding.get("attribution_level") != "primary":
                raise FinalValidationError(
                    "V1 fixed-queue validation accepts only primary findings"
                )
            dimension = finding.get("dimension")
            if not isinstance(dimension, str) or not dimension:
                raise FinalValidationError("primary finding lacks a dimension")
            if dimension not in candidate_counts or candidate_counts[dimension] <= 0:
                raise FinalValidationError(
                    f"finding does not back-reference a positive candidate step: {dimension}"
                )
            if not any(
                isinstance(finding.get(key), str) and finding[key].strip()
                for key in ("label", "value")
            ):
                raise FinalValidationError("primary finding lacks a slice identity")
            candidate = self._matching_candidate(
                finding, candidate_details.get(dimension, [])
            )
            if candidate is None:
                raise FinalValidationError(
                    f"finding slice is not a validated candidate: {dimension}"
                )
            adverse_impact = finding.get("adverse_impact_bp")
            if (
                isinstance(adverse_impact, bool)
                or not isinstance(adverse_impact, (int, float))
                or not math.isfinite(float(adverse_impact))
                or not math.isclose(
                    float(adverse_impact),
                    float(candidate["adverse_impact_bp"]),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                raise FinalValidationError(
                    f"finding adverse_impact_bp does not match candidate: {dimension}"
                )
            finding_counts[dimension] += 1
        for dimension, count in finding_counts.items():
            if count > candidate_counts[dimension]:
                raise FinalValidationError(
                    f"findings exceed candidate_count for {dimension}"
                )

        positive_candidate_count = sum(
            count for count in candidate_counts.values() if count > 0
        )
        if investigation_status == "completed":
            if positive_candidate_count == 0:
                raise FinalValidationError(
                    "completed requires at least one positive candidate family"
                )
            if not top_findings:
                raise FinalValidationError("completed requires at least one top finding")
        elif investigation_status == "no_dominant_slice":
            if not candidate_successes:
                raise FinalValidationError(
                    "no_dominant_slice requires at least one succeeded candidate family"
                )
            if positive_candidate_count > 0:
                raise FinalValidationError(
                    "no_dominant_slice cannot contain a positive candidate count"
                )
            if top_findings:
                raise FinalValidationError(
                    "no_dominant_slice cannot contain top findings"
                )
            if "counterfactual" in investigation:
                raise FinalValidationError(
                    "no_dominant_slice cannot contain counterfactual"
                )
        else:
            if candidate_successes or candidate_counts:
                raise FinalValidationError(
                    f"{investigation_status} requires every candidate family to fail"
                )
            if top_findings or "counterfactual" in investigation:
                raise FinalValidationError(
                    f"{investigation_status} cannot contain findings or counterfactual"
                )
        if candidate_failures and not candidate_successes and investigation_status in {
            "completed",
            "no_dominant_slice",
        }:
            raise FinalValidationError(
                "all candidate families failed; a successful investigation status is illegal"
            )

        evidence_hash = state.get("evidence_export_sha256")
        if isinstance(evidence_hash, str) and canonical_sha256(execution) != evidence_hash:
            raise FinalValidationError(
                "attribution_execution does not match the exported run evidence"
            )

        return {
            "status": "valid",
            "run_id": state["run_id"],
            "investigation_index": investigation_index,
            "investigation_status": investigation_status,
            "execution_mode": state["execution_mode"],
            "validated_step_count": len(actual_steps),
            "validated_query_id_count": len(self._query_ids_in(investigation)),
        }

    def _require_text(self, value: dict[str, Any], field: str) -> str:
        text = value.get(field)
        if not isinstance(text, str) or not text.strip():
            raise FinalValidationError(f"{field} must be a non-empty string")
        return text

    def _validate_text_array(self, value: dict[str, Any], field: str) -> None:
        items = value.get(field)
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise FinalValidationError(f"{field} must be an array of non-empty strings")

    def _known_query_ids(self, state: dict[str, Any]) -> set[str]:
        return {
            attempt["query_id"]
            for step in state["steps"]
            for attempt in step["attempts"]
            if isinstance(attempt.get("query_id"), str) and attempt["query_id"]
        }

    def _last_query_id(self, step: dict[str, Any]) -> str | None:
        for attempt in reversed(step["attempts"]):
            query_id = attempt.get("query_id")
            if isinstance(query_id, str) and query_id:
                return query_id
        return None

    def _matching_candidate(
        self, finding: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        finding_value = finding.get("value")
        finding_label = finding.get("label")
        for candidate in candidates:
            if isinstance(finding_value, str) and finding_value.strip() and (
                finding_value != candidate.get("value")
            ):
                continue
            if isinstance(finding_label, str) and finding_label.strip() and (
                finding_label != candidate.get("label")
            ):
                continue
            return candidate
        return None

    def _query_ids_in(self, value: Any) -> list[str]:
        result: list[str] = []
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "query_id":
                    if not isinstance(child, str) or not child:
                        raise FinalValidationError("query_id must be a non-empty string")
                    result.append(child)
                else:
                    result.extend(self._query_ids_in(child))
        elif isinstance(value, list):
            for child in value:
                result.extend(self._query_ids_in(child))
        return result
