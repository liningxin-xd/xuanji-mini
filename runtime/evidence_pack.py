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

        pack = {
            "analysis_profile": "primary_v1",
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
        root_metric = self.root_metric(steps)
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

    def root_metric(self, steps: list[dict[str, Any]]) -> dict[str, float] | None:
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
        if not roots:
            return None
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for root in roots
            for value in root
        ):
            raise EvidencePackError("successful candidate family lacks root metric facts")
        current, baseline, delta = (float(value) for value in roots[0])
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
