from __future__ import annotations

from copy import deepcopy
from typing import Any


class FinalAssemblyError(ValueError):
    pass


class FinalAssembler:
    PATCH_FIELDS = {
        "summary",
        "finding_texts",
        "evidence_limits",
        "recommended_action",
    }
    CONTEXT_FIELDS = {"source", "project", "table", "partition", "investigation"}
    INVESTIGATION_CONTEXT_FIELDS = {
        "rule_indexes",
        "metric_hint",
        "alert_partition",
        "alert_rules",
    }

    def assemble(
        self,
        *,
        writer_pack: dict[str, Any],
        attribution_execution: dict[str, Any],
        writer_patch: dict[str, Any],
        analysis_context: dict[str, Any],
    ) -> dict[str, Any]:
        context, investigation_context = self._validate_context(analysis_context)
        patch = self._validate_patch(writer_patch)
        status = writer_pack["result_status_hint"]
        candidate_by_id = {
            candidate["candidate_id"]: candidate
            for candidate in writer_pack["candidates"]
        }
        unknown_ids = set(patch["finding_texts"]) - set(candidate_by_id)
        if unknown_ids:
            raise FinalAssemblyError(
                f"writer patch references unknown candidates: {sorted(unknown_ids)}"
            )
        if status == "completed" and not patch["finding_texts"]:
            raise FinalAssemblyError("completed writer patch requires a finding text")
        if status != "completed" and patch["finding_texts"]:
            raise FinalAssemblyError(
                f"{status} writer patch cannot contain candidate findings"
            )

        investigation = {
            **investigation_context,
            "status": status,
            "metric": writer_pack["metric"],
            "analysis_date": writer_pack["analysis_date"],
            "attribution_execution": deepcopy(attribution_execution),
        }
        if status in {"completed", "no_dominant_slice"}:
            root_metric = writer_pack.get("root_metric")
            if not isinstance(root_metric, dict):
                raise FinalAssemblyError("successful writer pack lacks root metric facts")
            investigation.update(deepcopy(root_metric))
            investigation.update(
                {
                    "summary": patch["summary"],
                    "evidence_limits": list(patch["evidence_limits"]),
                    "recommended_action": patch["recommended_action"],
                }
            )
        else:
            investigation.update(
                {
                    "reason": patch["summary"],
                    "action": patch["recommended_action"],
                }
            )
            if patch["evidence_limits"]:
                investigation["evidence_limits"] = list(patch["evidence_limits"])

        findings = []
        for candidate in writer_pack["candidates"]:
            finding = patch["finding_texts"].get(candidate["candidate_id"])
            if finding is None:
                continue
            findings.append(
                {
                    "dimension": candidate["dimension"],
                    "value": candidate["value"],
                    "label": candidate["label"],
                    "attribution_level": "primary",
                    "adverse_impact_bp": candidate["adverse_impact_bp"],
                    "finding": finding,
                }
            )
        if findings:
            investigation["top_findings"] = findings

        return {
            "source": context["source"],
            "project": context["project"],
            "table": context["table"],
            "partition": context["partition"],
            "overall_status": (
                "completed"
                if status in {"completed", "no_dominant_slice"}
                else "failed"
            ),
            "investigations": [investigation],
        }

    def _validate_patch(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict) or set(patch) != self.PATCH_FIELDS:
            raise FinalAssemblyError(
                "writer patch must contain only summary, finding_texts, "
                "evidence_limits, and recommended_action"
            )
        for field in ("summary", "recommended_action"):
            if not isinstance(patch[field], str) or not patch[field].strip():
                raise FinalAssemblyError(f"writer patch {field} must be non-empty")
        findings = patch["finding_texts"]
        if not isinstance(findings, dict) or any(
            not isinstance(candidate_id, str)
            or not candidate_id
            or not isinstance(text, str)
            or not text.strip()
            for candidate_id, text in findings.items()
        ):
            raise FinalAssemblyError("writer patch finding_texts are invalid")
        limits = patch["evidence_limits"]
        if not isinstance(limits, list) or any(
            not isinstance(item, str) or not item.strip() for item in limits
        ):
            raise FinalAssemblyError("writer patch evidence_limits are invalid")
        return patch

    def _validate_context(
        self, context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(context, dict) or set(context) != self.CONTEXT_FIELDS:
            raise FinalAssemblyError("analysis context fields do not match the contract")
        if context.get("source") != "dataworks_dqc":
            raise FinalAssemblyError("analysis context source must be dataworks_dqc")
        for field in ("project", "table", "partition"):
            if not isinstance(context[field], str) or not context[field].strip():
                raise FinalAssemblyError(f"analysis context {field} must be non-empty")
        investigation = context["investigation"]
        if not isinstance(investigation, dict) or set(
            investigation
        ) != self.INVESTIGATION_CONTEXT_FIELDS:
            raise FinalAssemblyError("investigation context fields do not match")
        for field in ("metric_hint", "alert_partition"):
            if not isinstance(investigation[field], str) or not investigation[field].strip():
                raise FinalAssemblyError(f"investigation context {field} is invalid")
        indexes = investigation["rule_indexes"]
        if (
            not isinstance(indexes, list)
            or not indexes
            or any(isinstance(item, bool) or not isinstance(item, int) for item in indexes)
            or indexes != sorted(set(indexes))
        ):
            raise FinalAssemblyError("investigation rule_indexes are invalid")
        rules = investigation["alert_rules"]
        if not isinstance(rules, list) or len(rules) != len(indexes) or any(
            not isinstance(rule, dict)
            or not isinstance(rule.get("rule_name"), str)
            or not rule["rule_name"].strip()
            for rule in rules
        ):
            raise FinalAssemblyError("investigation alert_rules are invalid")
        return context, deepcopy(investigation)
