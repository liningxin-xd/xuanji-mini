from __future__ import annotations

from collections import Counter
import math
from typing import Any

from .analysis_v5 import (
    ANALYSIS_SCHEMA_VERSION,
    NARRATIVE_SCHEMA_VERSION,
    AnalysisV5Error,
    build_public_facts,
    public_machine_projection,
)
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

    def __init__(self, *, metric_polarity: str | None = None):
        self.metric_polarity = metric_polarity

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
        if analysis.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
            raise FinalValidationError("analysis schema_version must be 5")
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

        secondary = self._validate_secondary_execution(state, execution)

        top_findings = investigation.get("top_findings", [])
        if not isinstance(top_findings, list):
            raise FinalValidationError("top_findings must be an array when present")
        finding_counts: Counter[str] = Counter()
        for finding in top_findings:
            if not isinstance(finding, dict):
                raise FinalValidationError("each top finding must be an object")
            self._require_text(finding, "finding")
            attribution_level = finding.get("attribution_level")
            if attribution_level not in {"primary", "secondary"}:
                raise FinalValidationError("finding attribution_level is invalid")
            dimension = finding.get("dimension")
            if not isinstance(dimension, str) or not dimension:
                raise FinalValidationError("primary finding lacks a dimension")
            if attribution_level == "primary":
                if dimension not in candidate_counts or candidate_counts[dimension] <= 0:
                    raise FinalValidationError(
                        "finding does not back-reference a positive candidate "
                        f"step: {dimension}"
                    )
                candidates = candidate_details.get(dimension, [])
                count_key = f"primary:{dimension}"
                count_limit = candidate_counts[dimension]
            else:
                if secondary is None or secondary.get("status") != "succeeded":
                    raise FinalValidationError(
                        "secondary finding lacks a succeeded secondary step"
                    )
                if dimension != secondary["child_dimension"]:
                    raise FinalValidationError(
                        "secondary finding dimension does not match its step"
                    )
                for field in (
                    "parent_dimension",
                    "parent_value",
                    "parent_label",
                ):
                    if finding.get(field) != secondary[field]:
                        raise FinalValidationError(
                            f"secondary finding {field} does not match its parent"
                        )
                candidates = secondary["candidates"]
                count_key = f"secondary:{dimension}"
                count_limit = secondary["candidate_count"]
            if not any(
                isinstance(finding.get(key), str) and finding[key].strip()
                for key in ("label", "value")
            ):
                raise FinalValidationError("primary finding lacks a slice identity")
            candidate = self._matching_candidate(
                finding, candidates
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
            finding_counts[count_key] += 1
        for key, count in finding_counts.items():
            if key.startswith("primary:"):
                limit = candidate_counts[key.removeprefix("primary:")]
            elif secondary is not None:
                limit = secondary["candidate_count"]
            else:  # pragma: no cover - rejected above
                limit = 0
            if count > limit:
                raise FinalValidationError(
                    f"findings exceed candidate_count for {key}"
                )

        self._validate_counterfactual(state, investigation)

        positive_candidate_count = sum(
            count for count in candidate_counts.values() if count > 0
        )
        if secondary is not None and secondary.get("status") == "succeeded":
            positive_candidate_count += secondary["candidate_count"]
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

        self._validate_public_facts(
            state,
            investigation,
            execution,
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

    def _validate_public_facts(
        self,
        state: dict[str, Any],
        investigation: dict[str, Any],
        execution: dict[str, Any],
    ) -> None:
        public_facts = investigation.get("public_facts")
        if not isinstance(public_facts, dict):
            raise FinalValidationError("schema-v5 investigation lacks public_facts")
        narrative = public_facts.get("user_narrative")
        if not isinstance(narrative, dict):
            raise FinalValidationError("public_facts.user_narrative must be an object")
        required_narrative = {
            "schema_version",
            "summary",
            "finding_texts",
            "evidence_limits",
            "recommended_action",
            "fallback_status",
        }
        if not required_narrative.issubset(narrative):
            raise FinalValidationError("public user narrative is incomplete")
        if narrative.get("schema_version") != NARRATIVE_SCHEMA_VERSION:
            raise FinalValidationError("public user narrative schema is invalid")
        for field in ("summary", "recommended_action"):
            self._require_text(narrative, field)
        self._validate_text_array(narrative, "evidence_limits")
        fallback_status = narrative.get("fallback_status")
        if fallback_status not in {"not_used", "partial", "used"}:
            raise FinalValidationError("public user narrative fallback_status is invalid")
        if fallback_status == "not_used" and (
            "fallback_reason" in narrative or "fallback_candidate_ids" in narrative
        ):
            raise FinalValidationError("non-fallback narrative contains fallback metadata")
        if fallback_status in {"partial", "used"}:
            self._require_text(narrative, "fallback_reason")

        finding_texts = narrative.get("finding_texts")
        if not isinstance(finding_texts, dict):
            raise FinalValidationError("public user narrative finding_texts is invalid")
        findings = public_facts.get("findings")
        if not isinstance(findings, list):
            raise FinalValidationError("public_facts.findings must be an array")
        expected_candidate_ids = []
        for finding in findings:
            if not isinstance(finding, dict):
                raise FinalValidationError("public finding must be an object")
            candidate_id = self._require_text(finding, "candidate_id")
            expected_candidate_ids.append(candidate_id)
            text = self._require_text(finding, "narrative_text")
            if finding_texts.get(candidate_id) != text:
                raise FinalValidationError(
                    "public finding narrative does not match user_narrative"
                )
        if set(finding_texts) != set(expected_candidate_ids) or any(
            not isinstance(value, str) or not value.strip()
            for value in finding_texts.values()
        ):
            raise FinalValidationError(
                "public user narrative must cover every frozen candidate exactly once"
            )
        if fallback_status == "partial":
            fallback_ids = narrative.get("fallback_candidate_ids")
            if (
                not isinstance(fallback_ids, list)
                or not fallback_ids
                or any(item not in expected_candidate_ids for item in fallback_ids)
                or len(fallback_ids) != len(set(fallback_ids))
            ):
                raise FinalValidationError("partial narrative fallback IDs are invalid")

        combined_text = "\n".join(
            [
                narrative["summary"],
                narrative["recommended_action"],
                *finding_texts.values(),
                *narrative["evidence_limits"],
            ]
        )
        if any(
            term in combined_text
            for term in ("CardKit", "主卡", "副卡", "回复卡", "本消息线程")
        ):
            raise FinalValidationError("public narrative contains channel-specific language")

        recommendations = public_facts.get("recommendations")
        if not isinstance(recommendations, list) or not recommendations:
            raise FinalValidationError("public recommendations must be a non-empty array")
        if any(
            not isinstance(item, dict)
            or item.get("display_text") != narrative["recommended_action"]
            for item in recommendations
        ):
            raise FinalValidationError(
                "public recommendation display text does not match user narrative"
            )

        metric_polarity = self.metric_polarity
        if metric_polarity is None:
            metric = public_facts.get("metric")
            metric_polarity = metric.get("polarity") if isinstance(metric, dict) else None
        try:
            pack = EvidencePackBuilder(metric_polarity=str(metric_polarity)).build(state)
            expected = build_public_facts(
                writer_pack=pack,
                machine_state=state,
                attribution_execution=execution,
                writer_patch={
                    "summary": narrative["summary"],
                    "finding_texts": finding_texts,
                    "evidence_limits": narrative["evidence_limits"],
                    "recommended_action": narrative["recommended_action"],
                },
            )
        except (AnalysisV5Error, EvidencePackError, KeyError, TypeError, ValueError) as exc:
            raise FinalValidationError(f"public facts cannot be reconstructed: {exc}") from exc
        if public_machine_projection(public_facts) != public_machine_projection(expected):
            raise FinalValidationError(
                "public machine facts do not match the frozen investigation state"
            )
        if investigation.get("status") in {"completed", "no_dominant_slice"}:
            if investigation.get("summary") != narrative["summary"]:
                raise FinalValidationError("legacy summary differs from the public narrative")
            if investigation.get("recommended_action") != narrative[
                "recommended_action"
            ]:
                raise FinalValidationError(
                    "legacy recommended_action differs from the public narrative"
                )
        elif (
            investigation.get("reason") != narrative["summary"]
            or investigation.get("action") != narrative["recommended_action"]
        ):
            raise FinalValidationError(
                "legacy failure narrative differs from public_facts"
            )

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
        query_ids = {
            attempt["query_id"]
            for step in state["steps"]
            for attempt in step["attempts"]
            if isinstance(attempt.get("query_id"), str) and attempt["query_id"]
        }
        post_primary = state.get("post_primary")
        if isinstance(post_primary, dict):
            for step in post_primary.get("steps", []):
                query_items = (
                    step.get("items", [])
                    if step.get("id") == "game_background"
                    else [step]
                )
                query_ids.update(
                    attempt["query_id"]
                    for item in query_items
                    for attempt in item.get("attempts", [])
                    if isinstance(attempt.get("query_id"), str)
                    and attempt["query_id"]
                )
        return query_ids

    def _validate_secondary_execution(
        self, state: dict[str, Any], execution: dict[str, Any]
    ) -> dict[str, Any] | None:
        post_primary = state.get("post_primary")
        secondary = None
        if isinstance(post_primary, dict):
            secondary = next(
                (
                    step
                    for step in post_primary.get("steps", [])
                    if step.get("id") == "secondary"
                ),
                None,
            )
        expected_steps = []
        normalized = None
        if isinstance(secondary, dict) and secondary.get("status") in {
            "succeeded",
            "failed",
        }:
            expected = {
                "parent_dimension": secondary["parent_dimension"],
                "parent_value": secondary["parent_value"],
                "parent_label": secondary["parent_label"],
                "step": secondary["child_dimension"],
                "status": secondary["status"],
            }
            if secondary["status"] == "succeeded":
                expected["candidate_count"] = secondary["candidate_count"]
            else:
                expected["reason"] = secondary["reason"]
            query_id = self._last_query_id(secondary)
            if query_id:
                expected["query_id"] = query_id
            if secondary.get("warning_codes"):
                expected["warning_codes"] = secondary["warning_codes"]
            expected_steps.append(expected)
            normalized = {
                "status": secondary["status"],
                "parent_dimension": secondary["parent_dimension"],
                "parent_value": secondary["parent_value"],
                "parent_label": secondary["parent_label"],
                "child_dimension": secondary["child_dimension"],
                "candidate_count": secondary.get("candidate_count", 0),
                "candidates": secondary.get("candidates", []),
            }
        actual = execution.get("secondary_steps", [])
        if not isinstance(actual, list) or len(actual) > 1:
            raise FinalValidationError("at most one secondary step is allowed")
        if actual != expected_steps:
            raise FinalValidationError(
                "secondary_steps do not match the frozen post-primary result"
            )
        return normalized

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

    def _validate_counterfactual(
        self, state: dict[str, Any], investigation: dict[str, Any]
    ) -> None:
        expected = None
        post_primary = state.get("post_primary")
        if isinstance(post_primary, dict):
            step = next(
                (
                    item
                    for item in post_primary.get("steps", [])
                    if isinstance(item, dict) and item.get("id") == "counterfactual"
                ),
                None,
            )
            if isinstance(step, dict) and step.get("status") == "succeeded":
                expected = step.get("result")
        actual = investigation.get("counterfactual")
        if expected is None:
            if "counterfactual" in investigation:
                raise FinalValidationError(
                    "counterfactual was not produced by post-primary calibration"
                )
            return
        if investigation.get("status") != "completed" or not isinstance(actual, dict):
            raise FinalValidationError(
                "completed calibrated investigation requires counterfactual"
            )
        fields = {
            "dimension",
            "value",
            "label",
            "removal_delta_bp",
            "restoration_ratio",
            "finding",
        }
        if set(actual) != fields:
            raise FinalValidationError("counterfactual fields do not match the contract")
        for field in ("dimension", "value", "label", "finding"):
            if actual[field] != expected.get(field):
                raise FinalValidationError(
                    f"counterfactual {field} does not match machine evidence"
                )
        for field in ("removal_delta_bp", "restoration_ratio"):
            value = actual[field]
            expected_value = expected.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not math.isclose(
                    float(value),
                    float(expected_value),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                raise FinalValidationError(
                    f"counterfactual {field} does not match machine evidence"
                )

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
