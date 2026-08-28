from __future__ import annotations

import json
import math
from typing import Any

from .models import StepStatus, TERMINAL_STEP_STATUSES


class EvidencePackError(ValueError):
    pass


class EvidencePackBuilder:
    def __init__(
        self,
        *,
        max_candidates_per_family: int = 3,
        max_bytes: int = 12 * 1024,
    ):
        self.max_candidates_per_family = max_candidates_per_family
        self.max_bytes = max_bytes

    def build(self, state: dict[str, Any]) -> dict[str, Any]:
        steps = state.get("steps")
        if state.get("cursor") != len(steps or ()) or not isinstance(steps, list):
            raise EvidencePackError("writer pack requires a complete fixed queue")
        if any(step.get("status") not in TERMINAL_STEP_STATUSES for step in steps):
            raise EvidencePackError("writer pack cannot contain a non-terminal step")

        exposed_candidates: list[dict[str, Any]] = []
        exposed_ids: set[str] = set()
        writer_steps = []
        evidence_limits: list[str] = []
        for step in steps:
            status = step["status"]
            writer_step = {
                "step": step["id"],
                "status": status,
                "warning_codes": list(step["warning_codes"]),
            }
            if status == StepStatus.SUCCEEDED.value:
                writer_step["candidate_count"] = step["candidate_count"]
            elif status == StepStatus.FAILED.value:
                failure_code = step.get("failure_code")
                writer_step["failure_code"] = failure_code
                evidence_limits.append(f"{step['id']}:{failure_code}")
            writer_steps.append(writer_step)
            evidence_limits.extend(
                f"{step['id']}:{code}" for code in step["warning_codes"]
            )
            if status != StepStatus.SUCCEEDED.value or not step[
                "produces_candidates"
            ]:
                continue
            for candidate in step["candidates"][: self.max_candidates_per_family]:
                candidate_id = f"{step['id']}:{candidate['value']}"
                if candidate_id in exposed_ids:
                    raise EvidencePackError("writer candidate IDs are not unique")
                exposed_ids.add(candidate_id)
                exposed_candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "dimension": step["id"],
                        "value": candidate["value"],
                        "label": candidate["label"],
                        "current_rate": candidate["current_rate"],
                        "baseline_rate": candidate["baseline_rate"],
                        "adverse_impact_bp": candidate["adverse_impact_bp"],
                        "lifecycle": candidate["lifecycle"],
                    }
                )

        analysis_profile = state.get("analysis_profile", "primary_v1")
        pack = {
            "analysis_profile": analysis_profile,
            "run_id": state["run_id"],
            "metric": state["metric"],
            "analysis_date": state["analysis_date"],
            "game_type": state["game_type"],
            "execution_mode": state["execution_mode"],
            "result_status_hint": self._result_status_hint(steps),
            "steps": writer_steps,
            "candidates": exposed_candidates,
            "evidence_limits": evidence_limits,
        }
        if analysis_profile == "primary_v2":
            post_primary = state.get("post_primary")
            if not isinstance(post_primary, dict) or post_primary.get("status") != (
                "completed"
            ):
                raise EvidencePackError("primary_v2 writer pack requires calibration")
            pack["post_primary_steps"] = []
            for step in post_primary.get("steps", []):
                if step.get("reason") == "profile_step_disabled":
                    continue
                writer_step = {"step": step["id"], "status": step["status"]}
                if step["status"] == "skipped_by_policy" and isinstance(
                    step.get("reason"), str
                ):
                    writer_step["reason"] = step["reason"]
                for field in ("failure_code", "limit_code"):
                    if isinstance(step.get(field), str):
                        writer_step[field] = step[field]
                if step["id"] == "secondary" and step["status"] in {
                    "succeeded",
                    "failed",
                }:
                    writer_step.update(
                        {
                            "parent_dimension": step["parent_dimension"],
                            "parent_value": step["parent_value"],
                            "parent_label": step["parent_label"],
                            "child_dimension": step["child_dimension"],
                        }
                    )
                    if step["status"] == "succeeded":
                        writer_step["candidate_count"] = step["candidate_count"]
                pack["post_primary_steps"].append(writer_step)
                limit_code = step.get("limit_code")
                if isinstance(limit_code, str):
                    evidence_limits.append(limit_code)
                failure_code = step.get("failure_code")
                if isinstance(failure_code, str) and step["id"] != (
                    "game_background"
                ):
                    evidence_limits.append(f"{step['id']}:{failure_code}")
                if step["id"] == "counterfactual" and step["status"] == "succeeded":
                    result = step.get("result")
                    if not isinstance(result, dict):
                        raise EvidencePackError(
                            "succeeded counterfactual lacks its machine result"
                        )
                    pack["counterfactual"] = {
                        field: result[field]
                        for field in (
                            "dimension",
                            "value",
                            "label",
                            "current_without",
                            "baseline_without",
                            "removal_delta_bp",
                            "restoration_ratio",
                            "family_adverse_share",
                            "trigger_reasons",
                            "dominant",
                            "dominance_reasons",
                            "finding",
                        )
                    }
                if step["id"] == "secondary" and step["status"] == "succeeded":
                    secondary_candidates = step.get("candidates", [])
                    for candidate in secondary_candidates[
                        : self.max_candidates_per_family
                    ]:
                        candidate_id = (
                            f"secondary:{step['parent_dimension']}:"
                            f"{step['parent_value']}:{step['child_dimension']}:"
                            f"{candidate['value']}"
                        )
                        if candidate_id in exposed_ids:
                            raise EvidencePackError(
                                "writer candidate IDs are not unique"
                            )
                        exposed_ids.add(candidate_id)
                        exposed_candidates.append(
                            {
                                "candidate_id": candidate_id,
                                "attribution_level": "secondary",
                                "parent_dimension": step["parent_dimension"],
                                "parent_value": step["parent_value"],
                                "parent_label": step["parent_label"],
                                "dimension": step["child_dimension"],
                                "value": candidate["value"],
                                "label": candidate["label"],
                                "current_rate": candidate["current_rate"],
                                "baseline_rate": candidate["baseline_rate"],
                                "adverse_impact_bp": candidate[
                                    "adverse_impact_bp"
                                ],
                                "lifecycle": candidate["lifecycle"],
                            }
                        )
                if step["id"] == "game_background" and step["status"] in {
                    "succeeded",
                    "failed",
                }:
                    items = step.get("items")
                    if not isinstance(items, list):
                        raise EvidencePackError(
                            "terminal game background lacks its items"
                        )
                    pack["game_background"] = []
                    for item in items:
                        if not isinstance(item, dict):
                            raise EvidencePackError(
                                "game background item must be an object"
                            )
                        if item.get("status") == "succeeded":
                            facts = item.get("facts")
                            if not isinstance(facts, list):
                                raise EvidencePackError(
                                    "game background facts must be an array"
                                )
                            pack["game_background"].append(
                                {
                                    "candidate_id": item["candidate_id"],
                                    "game_id": item["game_id"],
                                    "label": item["label"],
                                    "facts": facts[:4],
                                }
                            )
                            for code in item.get("limit_codes", []):
                                evidence_limits.append(
                                    f"{item['candidate_id']}:{code}"
                                )
                        elif item.get("status") == "failed":
                            failure_code = item.get("failure_code")
                            if isinstance(failure_code, str):
                                evidence_limits.append(
                                    f"{item['candidate_id']}:{failure_code}"
                                )
                        else:
                            raise EvidencePackError(
                                "game background writer item is not terminal"
                            )
                if step["id"] == "error_code" and step["status"] in {
                    "succeeded",
                    "failed",
                }:
                    if step["status"] == "succeeded":
                        facts = step.get("facts")
                        if not isinstance(facts, list) or len(facts) > 5:
                            raise EvidencePackError(
                                "error-code facts exceed the writer contract"
                            )
                        pack["error_code_calibration"] = facts
                        for code in step.get("limit_codes", []):
                            evidence_limits.append(f"error_code:{code}")
        root_metric = self.root_metric(state)
        if root_metric is not None:
            pack["root_metric"] = root_metric
        encoded = json.dumps(
            pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise EvidencePackError(
                f"writer pack exceeds the {self.max_bytes}-byte context budget"
            )
        return pack

    def root_metric(self, state: dict[str, Any]) -> dict[str, float] | None:
        steps = state.get("steps")
        if not isinstance(steps, list):
            raise EvidencePackError("writer state steps are invalid")
        roots = [
            (
                step.get("root_current_value"),
                step.get("root_baseline_value"),
                step.get("root_delta"),
            )
            for step in steps
            if step.get("status") == StepStatus.SUCCEEDED.value
            and step.get("produces_candidates") is True
        ]
        canonical = state.get("canonical_root_metric")
        if canonical is None:
            return None
        if (
            not isinstance(canonical, dict)
            or set(canonical) != {"current_value", "baseline_value", "delta"}
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in canonical.values()
            )
        ):
            raise EvidencePackError("canonical root metric is invalid")
        current = float(canonical["current_value"])
        baseline = float(canonical["baseline_value"])
        delta = float(canonical["delta"])
        if not math.isclose(current - baseline, delta, rel_tol=0.0, abs_tol=1e-12):
            raise EvidencePackError("canonical root metric does not close")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for root in roots
            for value in root
        ):
            raise EvidencePackError("successful candidate family lacks root metric facts")
        if any(
            not math.isclose(
                float(actual),
                expected,
                rel_tol=0.0,
                abs_tol=0.000001,
            )
            for root in roots
            for actual, expected in zip(root, (current, baseline, delta), strict=True)
        ):
            raise EvidencePackError(
                "successful candidate families do not share one root metric"
            )
        return {
            "current_value": current,
            "baseline_value": baseline,
            "delta_bp": delta * 10000,
        }

    def _result_status_hint(self, steps: list[dict[str, Any]]) -> str:
        candidate_steps = [step for step in steps if step["produces_candidates"]]
        succeeded = [
            step
            for step in candidate_steps
            if step["status"] == StepStatus.SUCCEEDED.value
        ]
        if any(step["candidate_count"] > 0 for step in succeeded):
            return "completed"
        if succeeded:
            return "no_dominant_slice"
        failure_codes = {step.get("failure_code") for step in candidate_steps}
        if failure_codes == {"query_blocked"}:
            return "query_blocked"
        if "query_failed" in failure_codes:
            return "query_failed"
        return "insufficient_data"
