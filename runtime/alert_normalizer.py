from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from typing import Any


_PARTITION_DATE = re.compile(r"(?:^|[,/\s])dt\s*=\s*(\d{4}-\d{2}-\d{2}|\d{8})(?:$|[,/\s])")
_PAYLOAD_FIELDS = {
    "projectName",
    "dqcEntityQuality",
    "ruleChecks",
}
_ENTITY_FIELDS = {
    "projectName",
    "entityName",
    "actualExpression",
}
_RULE_FIELDS = {
    "ruleName",
    "tableName",
    "actualExpression",
    "op",
    "operator",
    "expectValue",
    "criticalThreshold",
    "warningThreshold",
    "checkResult",
}


class AlertNormalizationError(ValueError):
    pass


class AlertNormalizer:
    """Convert one raw DataWorks DQC payload into machine-owned identity facts."""

    def normalize(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AlertNormalizationError("dqc_payload must be an object")
        entity = payload.get("dqcEntityQuality")
        if not isinstance(entity, dict):
            entity = {}
        raw_rules = payload.get("ruleChecks")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise AlertNormalizationError("dqc_payload.ruleChecks must be a non-empty array")

        project = self._text(entity.get("projectName")) or self._text(
            payload.get("projectName")
        )
        root_table = self._text(entity.get("entityName"))
        root_partition = self._text(entity.get("actualExpression"))
        rules = []
        for rule_index, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, dict):
                rules.append(
                    {
                        "rule_index": rule_index,
                        "rule_name": None,
                        "table": root_table,
                        "partition": root_partition,
                        "alert_date": self.extract_date(root_partition),
                        "operator": None,
                        "threshold": None,
                        "check_result": None,
                        "unknown_fields": {"raw_value": deepcopy(raw_rule)},
                    }
                )
                continue
            table = self._text(raw_rule.get("tableName")) or root_table
            partition = self._text(raw_rule.get("actualExpression")) or root_partition
            rules.append(
                {
                    "rule_index": rule_index,
                    "rule_name": self._text(raw_rule.get("ruleName")),
                    "table": table,
                    "partition": partition,
                    "alert_date": self.extract_date(partition),
                    "operator": self._text(
                        raw_rule.get("op", raw_rule.get("operator"))
                    ),
                    "threshold": self._threshold(raw_rule),
                    "check_result": deepcopy(raw_rule.get("checkResult")),
                    "unknown_fields": {
                        key: deepcopy(value)
                        for key, value in raw_rule.items()
                        if key not in _RULE_FIELDS
                    },
                }
            )

        table = root_table or self._common_text(rules, "table")
        partition = root_partition or self._common_text(rules, "partition")
        return {
            "source": "dataworks_dqc",
            "project": project,
            "table": table,
            "partition": partition,
            "alert_date": self.extract_date(partition),
            "rules": rules,
            "unknown_fields": {
                key: deepcopy(value)
                for key, value in payload.items()
                if key not in _PAYLOAD_FIELDS
            },
            "entity_unknown_fields": {
                key: deepcopy(value)
                for key, value in entity.items()
                if key not in _ENTITY_FIELDS
            },
        }

    @staticmethod
    def extract_date(partition: Any) -> str | None:
        if not isinstance(partition, str):
            return None
        matches = _PARTITION_DATE.findall(f" {partition.strip()} ")
        if len(set(matches)) != 1:
            return None
        raw = matches[0]
        candidate = raw if "-" in raw else f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _threshold(rule: dict[str, Any]) -> Any:
        for field in ("expectValue", "criticalThreshold", "warningThreshold"):
            if field in rule:
                return deepcopy(rule[field])
        return None

    @staticmethod
    def _common_text(rules: list[dict[str, Any]], field: str) -> str | None:
        values = {
            rule[field]
            for rule in rules
            if isinstance(rule.get(field), str) and rule[field]
        }
        return next(iter(values)) if len(values) == 1 else None


def normalize_dqc_payload(payload: Any) -> dict[str, Any]:
    return AlertNormalizer().normalize(payload)
