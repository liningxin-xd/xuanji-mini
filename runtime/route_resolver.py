from __future__ import annotations

import math
import unicodedata
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .contracts import ContractError, RepositoryContracts


ROOT = Path(__file__).resolve().parents[1]
_ROUTE_FIELDS = {
    "route_id",
    "metric_hint",
    "observed_rule_names",
    "object_table",
    "chain",
    "game_type",
    "canonical_metric",
    "rule_kind",
    "alert_operator",
    "alert_threshold",
    "monitor_field",
    "monitor_numerator_field",
    "monitor_denominator_field",
    "analysis_lag_days",
}
_PASS_OPERATORS = {
    "<": ">=",
    "<=": ">",
    ">": "<=",
    ">=": "<",
    "==": "!=",
    "!=": "==",
}


class RouteContractError(ValueError):
    pass


@dataclass(frozen=True)
class DqcRoute:
    route_id: str
    metric_hint: str
    observed_rule_names: tuple[str, ...]
    object_table: str
    chain: str
    game_type: str
    canonical_metric: str
    rule_kind: str
    alert_operator: str
    alert_threshold: float
    monitor_field: str
    monitor_numerator_field: str
    monitor_denominator_field: str
    analysis_lag_days: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_rule_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower().replace("→", "->")
    return "".join(normalized.split())


def extract_metric_hint(rule_name: str) -> str | None:
    normalized = normalize_rule_name(rule_name)
    opening = normalized.find("【")
    closing = normalized.find("】", opening + 1)
    if opening < 0 or closing <= opening + 1:
        return None
    return normalized[opening + 1 : closing]


def infer_rule_kind(rule_name: str) -> str | None:
    normalized = normalize_rule_name(rule_name)
    closing = normalized.find("】")
    if closing < 0:
        return None
    suffix = normalized[closing + 1 :]
    if "连续3周下降" in suffix:
        return "trend_3w"
    if "对比过去7天均值" in suffix:
        return "relative_7d"
    if "最近1天" in suffix:
        return "absolute_1d"
    return None


def rule_binding_identity(rule_name: str) -> tuple[str, str] | None:
    """Extract the configured metric hint and generic DQC rule kind."""

    metric_hint = extract_metric_hint(rule_name)
    rule_kind = infer_rule_kind(rule_name)
    if metric_hint is None or rule_kind is None:
        return None
    return metric_hint, rule_kind


class DqcRouteRegistry:
    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root).resolve()
        self.path = self.root / "contracts" / "dqc-routes.yaml"
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RouteContractError(f"cannot load DQC route contract: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 2:
            raise RouteContractError("DQC route contract version must be 2")
        object_tables = raw.get("object_tables")
        routes = raw.get("routes")
        if (
            not isinstance(object_tables, list)
            or not object_tables
            or not all(isinstance(item, str) and item for item in object_tables)
            or not isinstance(routes, list)
            or not routes
        ):
            raise RouteContractError("DQC route contract must register routes")
        self.object_tables = tuple(object_tables)
        self.routes = tuple(self._parse_route(item) for item in routes)
        self._by_identity: dict[tuple[str, str, str], DqcRoute] = {}
        self._by_observed_name: dict[tuple[str, str], DqcRoute] = {}
        route_ids: set[str] = set()
        for route in self.routes:
            if route.route_id in route_ids:
                raise RouteContractError(f"duplicate DQC route id: {route.route_id}")
            route_ids.add(route.route_id)
            identity = (route.object_table, route.metric_hint, route.rule_kind)
            if identity in self._by_identity:
                raise RouteContractError(f"duplicate DQC route identity: {identity}")
            self._by_identity[identity] = route
            for rule_name in route.observed_rule_names:
                observed_identity = (route.object_table, rule_name)
                if observed_identity in self._by_observed_name:
                    raise RouteContractError(
                        f"duplicate observed DQC rule name: {observed_identity}"
                    )
                self._by_observed_name[observed_identity] = route
        self._validate_families()
        self._validate_knowledge_base_bindings()

    def resolve(self, table: str | None, rule_name: str | None) -> DqcRoute | None:
        if not isinstance(table, str) or not isinstance(rule_name, str):
            return None
        normalized_rule_name = normalize_rule_name(rule_name)
        observed = self._by_observed_name.get((table, normalized_rule_name))
        if observed is not None:
            return observed
        binding = rule_binding_identity(normalized_rule_name)
        if binding is None:
            return None
        metric_hint, rule_kind = binding
        return self._by_identity.get((table, metric_hint, rule_kind))

    @staticmethod
    def is_observed_rule_name(route: DqcRoute, rule_name: Any) -> bool:
        return isinstance(rule_name, str) and normalize_rule_name(rule_name) in (
            route.observed_rule_names
        )

    def absolute_route_for(self, route: DqcRoute) -> DqcRoute:
        matches = [
            item
            for item in self.routes
            if item.object_table == route.object_table
            and item.chain == route.chain
            and item.game_type == route.game_type
            and item.canonical_metric == route.canonical_metric
            and item.rule_kind == "absolute_1d"
        ]
        if len(matches) != 1:
            raise RouteContractError("route family lacks one absolute root profile")
        return matches[0]

    def _parse_route(self, raw: Any) -> DqcRoute:
        if not isinstance(raw, dict) or set(raw) != _ROUTE_FIELDS:
            raise RouteContractError("each DQC route must contain the exact fields")
        for field in _ROUTE_FIELDS - {
            "alert_threshold",
            "analysis_lag_days",
            "observed_rule_names",
        }:
            if not isinstance(raw[field], str) or not raw[field]:
                raise RouteContractError(f"DQC route {field} must be non-empty")
        if normalize_rule_name(raw["metric_hint"]) != raw["metric_hint"]:
            raise RouteContractError("DQC route metric_hint is not canonical")
        observed_rule_names = raw["observed_rule_names"]
        if (
            not isinstance(observed_rule_names, list)
            or not observed_rule_names
            or not all(isinstance(item, str) and item for item in observed_rule_names)
        ):
            raise RouteContractError("DQC route observed_rule_names must be non-empty")
        normalized_observed_names = tuple(
            normalize_rule_name(item) for item in observed_rule_names
        )
        if len(set(normalized_observed_names)) != len(normalized_observed_names):
            raise RouteContractError("DQC route observed_rule_names contain duplicates")
        for observed_name in normalized_observed_names:
            observed_metric_hint = extract_metric_hint(observed_name)
            if (
                observed_metric_hint is not None
                and observed_metric_hint != raw["metric_hint"]
            ):
                raise RouteContractError(
                    "observed DQC rule name does not match its metric binding"
                )
        if raw["object_table"] not in self.object_tables:
            raise RouteContractError("DQC route uses an unregistered object table")
        if raw["chain"] not in {"download", "install"}:
            raise RouteContractError("DQC route chain is invalid")
        if raw["game_type"] not in {"app", "sandbox"}:
            raise RouteContractError("DQC route game_type is invalid")
        if raw["rule_kind"] not in {"absolute_1d", "relative_7d", "trend_3w"}:
            raise RouteContractError("DQC route rule_kind is invalid")
        if raw["alert_operator"] not in _PASS_OPERATORS:
            raise RouteContractError("DQC route alert_operator is invalid")
        threshold = raw["alert_threshold"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
        ):
            raise RouteContractError("DQC route alert_threshold is invalid")
        lag = raw["analysis_lag_days"]
        if isinstance(lag, bool) or lag not in {0, 2}:
            raise RouteContractError("DQC route analysis_lag_days is invalid")
        if (raw["chain"] == "install") != (lag == 2):
            raise RouteContractError("DQC route lag does not match its chain")
        return DqcRoute(
            **{
                **raw,
                "observed_rule_names": normalized_observed_names,
            }
        )

    def _validate_families(self) -> None:
        for route in self.routes:
            absolute = self.absolute_route_for(route)
            if (
                route.monitor_numerator_field != absolute.monitor_numerator_field
                or route.monitor_denominator_field != absolute.monitor_denominator_field
                or route.analysis_lag_days != absolute.analysis_lag_days
            ):
                raise RouteContractError("DQC route family root fields are inconsistent")

    def _validate_knowledge_base_bindings(self) -> None:
        try:
            contracts = RepositoryContracts(self.root)
            for route in self.routes:
                contracts.metric_definition(route.canonical_metric)
                contracts.select_plan(
                    route.chain,
                    route.game_type,
                    route.canonical_metric,
                )
        except ContractError as exc:
            raise RouteContractError(
                f"DQC route knowledge-base binding is invalid: {exc}"
            ) from exc


class RouteResolver:
    def __init__(self, registry: DqcRouteRegistry | None = None):
        self.registry = registry or DqcRouteRegistry()

    def resolve(self, alert: Any) -> list[dict[str, Any]]:
        if not isinstance(alert, dict) or not isinstance(alert.get("rules"), list):
            raise RouteContractError("normalized alert is invalid")
        groups: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        project = alert.get("project")
        for rule in alert["rules"]:
            index = rule.get("rule_index")
            table = self._qualified_table(project, rule.get("table"))
            partition = rule.get("partition")
            route = self.registry.resolve(table, rule.get("rule_name"))
            if route is None:
                key = ("unknown", index)
                groups[key] = self._unknown_investigation(rule, project, table)
                continue
            key = (
                project,
                table,
                partition,
                route.canonical_metric,
                route.chain,
                route.game_type,
            )
            group = groups.setdefault(
                key,
                {
                    "status": "pending",
                    "rule_indexes": [],
                    "project": project,
                    "table": table,
                    "partition": partition,
                    "alert_date": rule.get("alert_date"),
                    "metric_hint": route.canonical_metric,
                    "alert_rules": [],
                    "route": {
                        "object_table": route.object_table,
                        "chain": route.chain,
                        "game_type": route.game_type,
                        "canonical_metric": route.canonical_metric,
                        "analysis_lag_days": route.analysis_lag_days,
                        "rules": [],
                        "absolute_root": self.registry.absolute_route_for(
                            route
                        ).as_dict(),
                    },
                    "profile_warnings": [],
                },
            )
            if (
                not isinstance(project, str)
                or not project
                or not isinstance(partition, str)
                or not partition
                or rule.get("alert_date") is None
            ):
                group["status"] = "insufficient_definition"
                group["reason"] = "DQC project, table, partition, or alert date is missing"
            elif group["alert_date"] != rule.get("alert_date"):
                group["status"] = "insufficient_definition"
                group["reason"] = "grouped DQC rules do not share one alert date"
            group["rule_indexes"].append(index)
            group["alert_rules"].append({"rule_name": rule.get("rule_name") or ""})
            group["route"]["rules"].append(route.as_dict())
            group["profile_warnings"].extend(self._profile_warnings(rule, route))

        investigations = list(groups.values())
        expected = list(range(len(alert["rules"])))
        actual = sorted(
            index for investigation in investigations for index in investigation["rule_indexes"]
        )
        if actual != expected:
            raise RouteContractError("route grouping does not cover each rule index once")
        for investigation in investigations:
            investigation["rule_indexes"] = sorted(investigation["rule_indexes"])
            investigation["profile_warnings"] = sorted(
                set(investigation.get("profile_warnings", []))
            )
        return investigations

    def _unknown_investigation(
        self, rule: dict[str, Any], project: Any, table: str | None
    ) -> dict[str, Any]:
        return {
            "status": "insufficient_definition",
            "reason": (
                "DQC table, metric hint, and rule kind do not match a registered "
                "knowledge-base binding"
            ),
            "rule_indexes": [rule.get("rule_index")],
            "project": project,
            "table": table,
            "partition": rule.get("partition"),
            "alert_date": rule.get("alert_date"),
            "metric_hint": rule.get("rule_name") or "unregistered_rule",
            "alert_rules": [{"rule_name": rule.get("rule_name") or ""}],
            "route": None,
            "profile_warnings": [],
        }

    @staticmethod
    def _qualified_table(project: Any, table: Any) -> str | None:
        if not isinstance(table, str) or not table.strip():
            return None
        value = table.strip()
        if "." in value:
            return value
        if isinstance(project, str) and project.strip():
            return f"{project.strip()}.{value}"
        return value

    @staticmethod
    def _profile_warnings(rule: dict[str, Any], route: DqcRoute) -> list[str]:
        warnings = []
        if not DqcRouteRegistry.is_observed_rule_name(
            route, rule.get("rule_name")
        ):
            warnings.append("route_rule_name_profile_mismatch")
        operator = rule.get("operator")
        if operator is not None and operator != _PASS_OPERATORS[route.alert_operator]:
            warnings.append("route_operator_profile_mismatch")
        monitor_field = rule.get("monitor_field")
        if monitor_field is not None and monitor_field != route.monitor_field:
            warnings.append("route_monitor_field_profile_mismatch")
        threshold = rule.get("threshold")
        if threshold is not None and (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isclose(
                float(threshold),
                float(route.alert_threshold),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            warnings.append("route_threshold_profile_mismatch")
        return warnings


def resolve_alert_routes(alert: Any) -> list[dict[str, Any]]:
    return RouteResolver().resolve(deepcopy(alert))
