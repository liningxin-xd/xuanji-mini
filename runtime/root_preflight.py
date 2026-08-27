from __future__ import annotations

import math
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from .contracts import RepositoryContracts, canonical_sha256, sha256_bytes
from .host_adapter import DViewQueryExecutor


ROOT = Path(__file__).resolve().parents[1]
ROOT_QUERY_PATH = "references/queries/registered-monitor-root.yaml"
_FORBIDDEN_SQL = re.compile(
    r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|CALL)\b"
)


class RootPreflightError(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class RootPreflight:
    """Reproduce one registered root metric before the frozen attribution queue."""

    def __init__(
        self,
        *,
        executor: DViewQueryExecutor,
        repository_root: Path | str = ROOT,
    ):
        self.root = Path(repository_root).resolve()
        self.contracts = RepositoryContracts(self.root)
        self.executor = executor
        self.query_path = self.root / ROOT_QUERY_PATH
        self._query_spec = self._load_query_spec()

    def run(self, investigation: Any) -> dict[str, Any]:
        if not isinstance(investigation, dict):
            raise RootPreflightError("insufficient_definition", "investigation is invalid")
        route = investigation.get("route")
        if not isinstance(route, dict):
            raise RootPreflightError(
                "insufficient_definition", "investigation lacks a registered route"
            )
        alert_date = self._parse_date(investigation.get("alert_date"), "alert date")
        lag = route.get("analysis_lag_days")
        if isinstance(lag, bool) or lag not in {0, 2}:
            raise RootPreflightError(
                "insufficient_definition", "registered analysis lag is invalid"
            )
        analysis_date = alert_date - timedelta(days=lag)
        game_type = route.get("game_type")
        if game_type not in {"app", "sandbox"}:
            raise RootPreflightError(
                "insufficient_definition", "registered game type is invalid"
            )

        rows: list[dict[str, Any]] = []
        private_queries = []
        for offset in range(8):
            partition_date = alert_date - timedelta(days=offset)
            response = self.executor.execute_read_only(
                self._render_query(partition_date, game_type)
            )
            private_queries.append(
                {
                    "partition_date": partition_date.isoformat(),
                    "query_id": response.query_id,
                    "receipt_id": response.receipt_id,
                }
            )
            if response.raw_result is None:
                status = self._query_failure_status(response.error_class)
                raise RootPreflightError(
                    status,
                    f"registered root query failed for required day ({response.error_class})",
                )
            row = self._validate_one_row(
                response.raw_result,
                partition_date=partition_date,
                game_type=game_type,
            )
            private_queries[-1]["raw_result_sha256"] = canonical_sha256(
                response.raw_result
            )
            rows.append(row)

        root_profile = route.get("absolute_root")
        if not isinstance(root_profile, dict):
            raise RootPreflightError(
                "insufficient_definition", "route family lacks its absolute root profile"
            )
        numerator_field = root_profile.get("monitor_numerator_field")
        denominator_field = root_profile.get("monitor_denominator_field")
        materialized_field = root_profile.get("monitor_field")
        metric = route.get("canonical_metric")
        metric_contract = self.contracts.metric_result_contract(metric)
        daily = [
            self._root_day(
                row,
                numerator_field=numerator_field,
                denominator_field=denominator_field,
                materialized_field=materialized_field,
                numerator_subset=metric_contract["numerator_subset"],
            )
            for row in rows
        ]
        current = daily[0]
        previous = daily[1]
        baseline_numerator = sum(item["numerator"] for item in daily[1:])
        baseline_denominator = sum(item["denominator"] for item in daily[1:])
        if baseline_denominator <= 0:
            raise RootPreflightError(
                "insufficient_data", "registered root baseline denominator is not positive"
            )
        baseline_rate = baseline_numerator / baseline_denominator
        minimum_sample = float(self.contracts.result_defaults["minimum_sample"])
        if current["denominator"] < minimum_sample or (
            baseline_denominator / 7
        ) < minimum_sample:
            raise RootPreflightError(
                "insufficient_data", "registered root sample is below the machine threshold"
            )

        direction = metric_contract["direction"]
        root_delta = current["rate"] - baseline_rate
        root_adverse_delta = (
            baseline_rate - current["rate"]
            if direction == "higher_is_better"
            else current["rate"] - baseline_rate
        )
        registered_rules = route.get("rules")
        if not isinstance(registered_rules, list) or not registered_rules:
            raise RootPreflightError(
                "insufficient_definition", "investigation route rules are missing"
            )
        for registered_rule in registered_rules:
            monitor_field = registered_rule.get("monitor_field")
            operator = registered_rule.get("alert_operator")
            threshold = registered_rule.get("alert_threshold")
            if monitor_field not in rows[0] or not self._alert_side(
                rows[0][monitor_field], operator, threshold
            ):
                raise RootPreflightError(
                    "insufficient_definition",
                    "registered root value does not reproduce the DQC alert condition",
                )

        absolute_rules = [
            item for item in registered_rules if item.get("rule_kind") == "absolute_1d"
        ]
        absolute_continuation = bool(absolute_rules) and all(
            self._alert_side(value, item["alert_operator"], item["alert_threshold"])
            for item in absolute_rules
            for value in (current["rate"], previous["rate"], baseline_rate)
        )
        substantial_adverse = root_adverse_delta >= 0.0005 - 1e-12
        non_absolute_substantial = any(
            item.get("rule_kind") != "absolute_1d"
            and substantial_adverse
            for item in registered_rules
        )
        mode = (
            "existing_anomaly_stop"
            if absolute_continuation
            and not substantial_adverse
            and not non_absolute_substantial
            else "full_queue"
        )
        return {
            "status": "succeeded",
            "mode": mode,
            "metric": metric,
            "chain": route["chain"],
            "game_type": game_type,
            "alert_date": alert_date.isoformat(),
            "analysis_date": analysis_date.isoformat(),
            "direction": direction,
            "current_value": current["rate"],
            "previous_value": previous["rate"],
            "baseline_value": baseline_rate,
            "delta_bp": root_delta * 10000,
            "root_adverse_delta_bp": root_adverse_delta * 10000,
            "current_numerator": current["numerator"],
            "current_denominator": current["denominator"],
            "baseline_numerator": baseline_numerator,
            "baseline_denominator": baseline_denominator,
            "canonical_root_metric": {
                "current_value": current["rate"],
                "baseline_value": baseline_rate,
                "delta": root_delta,
            },
            "private_queries": private_queries,
        }

    def _load_query_spec(self) -> dict[str, Any]:
        self.contracts.verify_assets()
        expected_hash = self.contracts.asset_hashes.get(ROOT_QUERY_PATH)
        if expected_hash != sha256_bytes(self.query_path.read_bytes()):
            raise RootPreflightError(
                "insufficient_definition", "registered root QuerySpec is not locked"
            )
        try:
            spec = yaml.safe_load(self.query_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RootPreflightError(
                "insufficient_definition", "registered root QuerySpec cannot be loaded"
            ) from exc
        if (
            not isinstance(spec, dict)
            or not isinstance(spec.get("sql"), str)
            or not isinstance(spec.get("output"), dict)
            or not isinstance(spec.get("quality"), dict)
        ):
            raise RootPreflightError(
                "insufficient_definition", "registered root QuerySpec is malformed"
            )
        return spec

    def _render_query(self, partition_date: date, game_type: str) -> str:
        sql = self._query_spec["sql"]
        rendered = sql.replace("${business_date}", f"'{partition_date.isoformat()}'")
        rendered = rendered.replace("${game_type}", f"'{game_type}'")
        normalized = rendered.strip()
        if (
            "${" in normalized
            or _FORBIDDEN_SQL.search(normalized)
            or not normalized.upper().startswith("SELECT")
            or "tap_dw.ads_dmg_quality_platform_download_chain_monitor_1d" not in normalized
            or f"dt = '{partition_date.isoformat()}'" not in normalized
            or "platform = 'ANDROID'" not in normalized
            or f"game_type = '{game_type}'" not in normalized
        ):
            raise RootPreflightError(
                "insufficient_definition", "registered root SQL failed its fixed safety gate"
            )
        return rendered

    def _validate_one_row(
        self,
        raw_result: Any,
        *,
        partition_date: date,
        game_type: str,
    ) -> dict[str, Any]:
        columns = self._query_spec["output"].get("columns")
        if not isinstance(columns, dict):
            raise RootPreflightError(
                "insufficient_definition", "registered root output columns are missing"
            )
        if not isinstance(raw_result, dict) or raw_result.get("columns") != list(columns):
            raise RootPreflightError(
                "insufficient_data", "registered root columns do not match the QuerySpec"
            )
        raw_rows = raw_result.get("rows")
        if not isinstance(raw_rows, list) or len(raw_rows) != 1:
            raise RootPreflightError(
                "insufficient_data", "registered root must return exactly one row"
            )
        raw_row = raw_rows[0]
        if not isinstance(raw_row, list) or len(raw_row) != len(columns):
            raise RootPreflightError(
                "insufficient_data", "registered root row does not match its columns"
            )
        row = dict(zip(columns, raw_row, strict=True))
        if row.get("alert_date") != partition_date.isoformat():
            raise RootPreflightError(
                "insufficient_data", "registered root returned the wrong partition date"
            )
        if row.get("platform") != "ANDROID" or row.get("game_type") != game_type:
            raise RootPreflightError(
                "insufficient_definition", "registered root returned the wrong scope"
            )
        ranges = self._query_spec["quality"].get("ranges", {})
        for field, value_type in columns.items():
            value = row[field]
            contract = ranges.get(field, {})
            if value is None and contract.get("allow_null") is True:
                continue
            if value_type in {"number", "integer"}:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or (value_type == "integer" and not float(value).is_integer())
                ):
                    raise RootPreflightError(
                        "insufficient_data", f"registered root field is invalid: {field}"
                    )
            if isinstance(contract, dict) and isinstance(value, (int, float)):
                if "min" in contract and value < contract["min"]:
                    raise RootPreflightError(
                        "insufficient_data", f"registered root field is below range: {field}"
                    )
                if "max" in contract and value > contract["max"]:
                    raise RootPreflightError(
                        "insufficient_data", f"registered root field is above range: {field}"
                    )
        return row

    def _root_day(
        self,
        row: dict[str, Any],
        *,
        numerator_field: Any,
        denominator_field: Any,
        materialized_field: Any,
        numerator_subset: bool,
    ) -> dict[str, float]:
        for field in (numerator_field, denominator_field, materialized_field):
            if not isinstance(field, str) or field not in row:
                raise RootPreflightError(
                    "insufficient_definition", "registered root fields are unavailable"
                )
        numerator = float(row[numerator_field])
        denominator = float(row[denominator_field])
        materialized = float(row[materialized_field])
        if denominator <= 0 or numerator < 0:
            raise RootPreflightError(
                "insufficient_data", "registered root numerator or denominator is invalid"
            )
        if numerator_subset and numerator > denominator:
            raise RootPreflightError(
                "insufficient_data", "registered root subset numerator exceeds denominator"
            )
        rate = numerator / denominator
        if round(rate, 4) != round(materialized, 4):
            raise RootPreflightError(
                "insufficient_definition",
                "registered root materialized rate fails four-decimal reconciliation",
            )
        return {"numerator": numerator, "denominator": denominator, "rate": rate}

    @staticmethod
    def _alert_side(value: Any, operator: Any, threshold: Any) -> bool:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
        ):
            return False
        left = float(value)
        right = float(threshold)
        return {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
            "==": math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12),
            "!=": not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12),
        }.get(operator, False)

    @staticmethod
    def _parse_date(value: Any, name: str) -> date:
        if not isinstance(value, str):
            raise RootPreflightError("insufficient_definition", f"{name} is missing")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise RootPreflightError(
                "insufficient_definition", f"{name} is invalid"
            ) from exc

    @staticmethod
    def _query_failure_status(error_class: Any) -> str:
        normalized = str(error_class or "").lower()
        if any(
            marker in normalized
            for marker in ("permission", "access", "unauthorized", "forbidden")
        ):
            return "query_blocked"
        return "query_failed"
