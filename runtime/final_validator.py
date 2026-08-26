from __future__ import annotations

from collections import Counter
from typing import Any

from .models import StepStatus, TERMINAL_STEP_STATUSES


class FinalValidationError(ValueError):
    pass


class FinalEvidenceValidator:
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

        execution = investigation.get("attribution_execution")
        if not isinstance(execution, dict):
            raise FinalValidationError("investigation lacks attribution_execution")
        if execution.get("mode") != "full_queue":
            raise FinalValidationError("attribution_execution.mode must be full_queue")
        if execution.get("chain") != state["chain"]:
            raise FinalValidationError("attribution chain does not match the run")
        if execution.get("game_type") != state["game_type"]:
            raise FinalValidationError("attribution game_type does not match the run")

        actual_steps = execution.get("steps")
        if not isinstance(actual_steps, list) or len(actual_steps) != len(
            state["steps"]
        ):
            raise FinalValidationError("attribution step count does not match the fixed queue")
        known_query_ids = self._known_query_ids(state)
        candidate_counts: dict[str, int] = {}
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
            query_id = actual.get("query_id")
            if query_id is not None and query_id not in known_query_ids:
                raise FinalValidationError(
                    f"unknown query_id for {expected['id']}: {query_id}"
                )
            if "warning_codes" in actual and actual["warning_codes"] != expected[
                "warning_codes"
            ]:
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
            finding_counts[dimension] += 1
        for dimension, count in finding_counts.items():
            if count > candidate_counts[dimension]:
                raise FinalValidationError(
                    f"findings exceed candidate_count for {dimension}"
                )

        investigation_status = investigation.get("status")
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
        if candidate_failures and not candidate_successes and investigation_status in {
            "completed",
            "no_dominant_slice",
        }:
            raise FinalValidationError(
                "all candidate families failed; a successful investigation status is illegal"
            )

        return {
            "status": "valid",
            "run_id": state["run_id"],
            "investigation_index": investigation_index,
            "validated_step_count": len(actual_steps),
            "validated_query_id_count": len(self._query_ids_in(investigation)),
        }

    def _known_query_ids(self, state: dict[str, Any]) -> set[str]:
        return {
            attempt["query_id"]
            for step in state["steps"]
            for attempt in step["attempts"]
            if isinstance(attempt.get("query_id"), str) and attempt["query_id"]
        }

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
