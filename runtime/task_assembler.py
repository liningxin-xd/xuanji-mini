from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import canonical_sha256


class TaskAssemblyError(ValueError):
    pass


_PATCH_FIELDS = {
    "summary",
    "finding_texts",
    "evidence_limits",
    "recommended_action",
}
_SUCCESS_STATUSES = {"completed", "no_dominant_slice"}


class TaskAssembler:
    def validate_writer_patch(self, patch: Any, *, allow_findings: bool) -> dict[str, Any]:
        if not isinstance(patch, dict) or set(patch) != _PATCH_FIELDS:
            raise TaskAssemblyError(
                "writer patch must contain only summary, finding_texts, "
                "evidence_limits, and recommended_action"
            )
        for field in ("summary", "recommended_action"):
            if not isinstance(patch[field], str) or not patch[field].strip():
                raise TaskAssemblyError(f"writer patch {field} must be non-empty")
        findings = patch["finding_texts"]
        if not isinstance(findings, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value.strip()
            for key, value in findings.items()
        ):
            raise TaskAssemblyError("writer patch finding_texts are invalid")
        if findings and not allow_findings:
            raise TaskAssemblyError("this investigation cannot accept candidate findings")
        limits = patch["evidence_limits"]
        if not isinstance(limits, list) or any(
            not isinstance(item, str) or not item.strip() for item in limits
        ):
            raise TaskAssemblyError("writer patch evidence_limits are invalid")
        return deepcopy(patch)

    def assemble_machine_investigation(
        self,
        investigation: dict[str, Any],
        writer_patch: dict[str, Any],
    ) -> dict[str, Any]:
        patch = self.validate_writer_patch(writer_patch, allow_findings=False)
        result_status = investigation.get("result_status")
        if result_status not in {
            "no_dominant_slice",
            "insufficient_definition",
            "insufficient_data",
            "query_blocked",
            "query_failed",
            "unsupported_drilldown",
        }:
            raise TaskAssemblyError("machine investigation status is invalid")
        result = {
            "status": result_status,
            "rule_indexes": list(investigation["rule_indexes"]),
            "metric_hint": investigation["metric_hint"],
            "alert_rules": deepcopy(investigation["alert_rules"]),
            "alert_partition": investigation.get("partition") or "unknown",
        }
        preflight = investigation.get("root_preflight")
        evidence_limits = list(
            dict.fromkeys(
                [
                    *investigation.get("profile_warnings", []),
                    *patch["evidence_limits"],
                ]
            )
        )
        if result_status == "no_dominant_slice":
            if not isinstance(preflight, dict) or preflight.get("mode") != (
                "existing_anomaly_stop"
            ):
                raise TaskAssemblyError("existing anomaly result lacks root preflight facts")
            result.update(
                {
                    "metric": preflight["metric"],
                    "analysis_date": preflight["analysis_date"],
                    "current_value": preflight["current_value"],
                    "baseline_value": preflight["baseline_value"],
                    "delta_bp": preflight["delta_bp"],
                    "summary": patch["summary"],
                    "evidence_limits": evidence_limits,
                    "recommended_action": patch["recommended_action"],
                    "attribution_execution": {
                        "mode": "existing_anomaly_stop",
                        "chain": preflight["chain"],
                        "game_type": preflight["game_type"],
                        "reason": (
                            "the previous day and pooled baseline remain on the "
                            "same alert side without 5bp of new adverse change"
                        ),
                        "steps": [],
                    },
                }
            )
        else:
            result.update(
                {
                    "reason": patch["summary"],
                    "action": patch["recommended_action"],
                }
            )
            if evidence_limits:
                result["evidence_limits"] = evidence_limits
            route = investigation.get("route")
            if isinstance(route, dict):
                result["attribution_execution"] = {
                    "mode": investigation.get(
                        "failure_mode", "root_precheck_failed"
                    ),
                    "chain": route["chain"],
                    "game_type": route["game_type"],
                    "reason": investigation.get("machine_reason") or result["reason"],
                    "steps": [],
                }
                analysis_date = investigation.get("analysis_date")
                if isinstance(analysis_date, str):
                    result["analysis_date"] = analysis_date
        return result

    def assemble_task(self, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        investigations = state.get("investigations")
        if not isinstance(investigations, list) or not investigations:
            raise TaskAssemblyError("task must contain at least one investigation")
        results = []
        actual_indexes = []
        for investigation in investigations:
            if investigation.get("status") != "completed":
                raise TaskAssemblyError("task contains an incomplete investigation")
            result = investigation.get("result")
            if not isinstance(result, dict):
                raise TaskAssemblyError("completed investigation lacks its result")
            results.append(deepcopy(result))
            actual_indexes.extend(result.get("rule_indexes", []))
        expected_indexes = list(range(len(state["normalized_alert"]["rules"])))
        if sorted(actual_indexes) != expected_indexes or len(actual_indexes) != len(
            set(actual_indexes)
        ):
            raise TaskAssemblyError("task result does not cover each rule index once")

        success_count = sum(
            result.get("status") in _SUCCESS_STATUSES for result in results
        )
        if success_count == len(results):
            overall_status = "completed"
        elif success_count:
            overall_status = "partial"
        else:
            overall_status = "failed"
        alert = state["normalized_alert"]
        analysis = {
            "source": "dataworks_dqc",
            "project": alert.get("project") or "unknown",
            "table": self._qualified_table(alert),
            "partition": alert.get("partition") or "unknown",
            "overall_status": overall_status,
            "investigations": results,
        }
        receipt_summaries = []
        for investigation in investigations:
            receipt = investigation.get("validation_receipt")
            if isinstance(receipt, dict):
                receipt_summaries.append(
                    {
                        key: receipt[key]
                        for key in (
                            "status",
                            "investigation_status",
                            "execution_mode",
                            "validated_step_count",
                            "analysis_sha256",
                            "validation_receipt_sha256",
                        )
                        if key in receipt
                    }
                )
            else:
                receipt_summaries.append(
                    {
                        "status": "machine_validated",
                        "investigation_status": investigation["result"]["status"],
                    }
                )
        receipt = {
            "status": "valid",
            "task_id": state["task_id"],
            "payload_sha256": state["payload_sha256"],
            "overall_status": overall_status,
            "investigation_count": len(results),
            "successful_investigation_count": success_count,
            "rule_indexes_sha256": canonical_sha256(actual_indexes),
            "analysis_sha256": canonical_sha256(analysis),
            "investigation_receipts": receipt_summaries,
        }
        receipt["validation_receipt_sha256"] = canonical_sha256(receipt)
        return analysis, receipt

    @staticmethod
    def _qualified_table(alert: dict[str, Any]) -> str:
        table = alert.get("table")
        project = alert.get("project")
        if not isinstance(table, str) or not table:
            return "unknown"
        if "." in table or not isinstance(project, str) or not project:
            return table
        return f"{project}.{table}"


def writer_pack_size(writer_pack: dict[str, Any]) -> int:
    import json

    return len(
        json.dumps(
            writer_pack,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
