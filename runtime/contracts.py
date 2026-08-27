from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .models import ExecutionPlan, PlanStep, QueryBinding


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_PLAN_STEPS = {
    "download": (
        "game_id",
        "is_reserve_auto_download",
        "device_brand",
        "channel_group",
        "app_major_version",
        "os_major_version",
        "apk_size_tier",
    ),
    "install_app": (
        "game_id",
        "install_stage",
        "device_brand",
        "storage_headroom_tier",
        "os_major_version",
        "apk_size_tier",
    ),
    "install_sandbox": (
        "game_id",
        "install_stage",
        "device_brand",
        "storage_headroom_tier",
        "os_major_version",
        "apk_size_tier",
    ),
}

EXPECTED_ANALYSIS_PROFILES = {"primary_v1", "primary_v2"}
EXPECTED_POST_PRIMARY_STEPS = (
    "counterfactual",
    "secondary",
    "game_background",
    "error_code",
)

EXPECTED_LOCKED_ASSETS = {
    "references/queries/registered-monitor-root.yaml",
    "references/queries/download-game-attribution.yaml",
    "references/queries/download-failed-rate-game-attribution.yaml",
    "references/queries/download-failed-pv-rate-game-attribution.yaml",
    "references/queries/download-stop-rate-game-attribution.yaml",
    "references/queries/download-primary-attribution-template.md",
    "references/queries/download-failed-rate-primary-attribution-template.md",
    "references/queries/download-failed-pv-rate-primary-attribution-template.md",
    "references/queries/download-stop-rate-primary-attribution-template.md",
    "references/queries/install-game-attribution.yaml",
    "references/queries/install-stage-loss-decomposition.yaml",
    "references/queries/install-primary-attribution-template.md",
    "references/queries/install-post-start-version-template.md",
    "references/queries/secondary-attribution-template.md",
    "references/queries/game-operation-events.yaml",
    "references/queries/primary-attribution-dimensions.md",
}


class ContractError(ValueError):
    pass


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode("utf-8"))


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _safe_path(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ContractError("asset path must be a non-empty string")
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractError(f"path escapes repository root: {relative_path}") from exc
    return candidate


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"cannot load YAML contract {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"YAML contract must contain a mapping: {path}")
    return value


def _load_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load JSON contract {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON contract must contain an object: {path}")
    return value


def verify_asset_lock(root: Path, lock_path: Path) -> dict[str, Any]:
    lock = _load_json_mapping(lock_path)
    if lock.get("version") != 1:
        raise ContractError("query asset lock version must be 1")
    assets = lock.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ContractError("query asset lock must contain a non-empty assets list")

    verified: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(assets):
        if not isinstance(item, dict):
            raise ContractError(f"asset lock item {index} must be an object")
        relative_path = item.get("path")
        expected_hash = item.get("sha256")
        if relative_path in seen:
            raise ContractError(f"duplicate asset lock path: {relative_path}")
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise ContractError(f"invalid sha256 for locked asset: {relative_path}")
        path = _safe_path(root, relative_path)
        if not path.is_file():
            raise ContractError(f"locked asset does not exist: {relative_path}")
        actual_hash = sha256_bytes(path.read_bytes())
        if actual_hash != expected_hash:
            raise ContractError(
                f"query asset hash mismatch for {relative_path}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        seen.add(relative_path)
        verified.append({"path": relative_path, "sha256": actual_hash})

    return {
        "version": 1,
        "verified_count": len(verified),
        "assets": verified,
    }


class RepositoryContracts:
    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root).resolve()
        self.contract_root = self.root / "contracts"
        self.execution_plans_path = self.contract_root / "execution-plans.yaml"
        self.query_registry_path = self.contract_root / "query-registry.yaml"
        self.asset_lock_path = self.contract_root / "query-assets.lock.json"
        self.metric_definitions_path = (
            self.contract_root / "metric-definitions.lock.json"
        )
        self.result_schemas_path = self.contract_root / "result-schemas.yaml"
        self.analysis_profiles_path = self.contract_root / "analysis-profiles.yaml"
        self.post_primary_plan_path = self.contract_root / "post-primary-plan.yaml"
        self.error_code_capabilities_path = (
            self.contract_root / "error-code-capabilities.yaml"
        )
        self.error_code_triggers_path = (
            self.contract_root / "error-code-triggers.yaml"
        )
        self.secondary_relations_path = (
            self.contract_root / "secondary-relations.yaml"
        )

        self._plans_raw = _load_yaml_mapping(self.execution_plans_path)
        self._registry = _load_yaml_mapping(self.query_registry_path)
        self._asset_lock = _load_json_mapping(self.asset_lock_path)
        self._metric_definitions = _load_json_mapping(self.metric_definitions_path)
        self._result_schemas = _load_yaml_mapping(self.result_schemas_path)
        self._analysis_profiles = _load_yaml_mapping(self.analysis_profiles_path)
        self._post_primary_plans = _load_yaml_mapping(self.post_primary_plan_path)
        self._error_code_capabilities = _load_yaml_mapping(
            self.error_code_capabilities_path
        )
        self._error_code_triggers = _load_yaml_mapping(
            self.error_code_triggers_path
        )
        self._secondary_relations = _load_yaml_mapping(
            self.secondary_relations_path
        )
        self._plans = self._validate_plans()
        self._validate_analysis_profiles()
        self._validate_error_code_contracts()
        self._asset_hashes = self._load_asset_hashes()
        self._metric_definition_by_name = self._validate_metric_definitions()
        self._validate_registry()
        self._validate_secondary_relations()
        self._validate_result_schemas()

    @property
    def registry(self) -> dict[str, Any]:
        return self._registry

    @property
    def plans(self) -> dict[str, ExecutionPlan]:
        return dict(self._plans)

    @property
    def asset_hashes(self) -> dict[str, str]:
        return dict(self._asset_hashes)

    @property
    def execution_plan_sha256(self) -> str:
        return sha256_bytes(self.execution_plans_path.read_bytes())

    @property
    def query_registry_sha256(self) -> str:
        return sha256_bytes(self.query_registry_path.read_bytes())

    @property
    def triage_sha256(self) -> str:
        return sha256_bytes(self.triage_path().read_bytes())

    @property
    def result_schemas_sha256(self) -> str:
        return sha256_bytes(self.result_schemas_path.read_bytes())

    @property
    def secondary_relations_sha256(self) -> str:
        return sha256_bytes(self.secondary_relations_path.read_bytes())

    @property
    def error_code_capabilities_sha256(self) -> str:
        return sha256_bytes(self.error_code_capabilities_path.read_bytes())

    @property
    def error_code_triggers_sha256(self) -> str:
        return sha256_bytes(self.error_code_triggers_path.read_bytes())

    @property
    def default_analysis_profile(self) -> str:
        return self._analysis_profiles["default_profile"]

    @property
    def definition_bundle_sha256(self) -> str:
        return self._metric_definitions["bundle_sha256"]

    @property
    def result_defaults(self) -> dict[str, Any]:
        return deepcopy(self._result_schemas["defaults"])

    def metric_result_contract(self, metric: str) -> dict[str, Any]:
        contract = self._result_schemas["metrics"].get(metric)
        if not isinstance(contract, dict):
            raise ContractError(f"result contract is missing metric: {metric}")
        definition = self.metric_definition(metric)
        return {**deepcopy(contract), "direction": definition["direction"]}

    def metric_definition(self, metric: str) -> dict[str, Any]:
        definition = self._metric_definition_by_name.get(metric)
        if not isinstance(definition, dict):
            raise ContractError(f"compiled metric definition is missing: {metric}")
        return deepcopy(definition)

    def analysis_profile(self, profile: str) -> dict[str, Any]:
        value = self._analysis_profiles["profiles"].get(profile)
        if not isinstance(value, dict):
            raise ContractError(f"unknown analysis profile: {profile}")
        return deepcopy(value)

    def analysis_profile_sha256(self, profile: str) -> str:
        return canonical_sha256(
            {"profile": profile, "contract": self.analysis_profile(profile)}
        )

    def post_primary_plan(self, plan_id: str) -> dict[str, Any]:
        value = self._post_primary_plans["plans"].get(plan_id)
        if not isinstance(value, dict):
            raise ContractError(f"unknown post-primary plan: {plan_id}")
        return deepcopy(value)

    def post_primary_plan_contract_sha256(self, plan_id: str) -> str:
        return canonical_sha256(
            {"plan_id": plan_id, "contract": self.post_primary_plan(plan_id)}
        )

    def game_background_policy(self) -> dict[str, Any]:
        plan = self.post_primary_plan("post_primary_v1")
        step = next(
            (
                item
                for item in plan["steps"]
                if item.get("id") == "game_background"
            ),
            None,
        )
        if not isinstance(step, dict) or not isinstance(
            step.get("selection"), dict
        ):
            raise ContractError("game background selection policy is missing")
        return deepcopy(step["selection"])

    def error_code_capability(
        self, chain: str, game_type: str
    ) -> dict[str, Any]:
        capability_id = f"{chain}_{game_type}"
        value = self._error_code_capabilities["capabilities"].get(capability_id)
        if not isinstance(value, dict):
            raise ContractError(
                f"error-code capability is not registered: {chain}/{game_type}"
            )
        return deepcopy(value)

    def error_code_trigger(
        self, chain: str, game_type: str, metric: str
    ) -> dict[str, Any]:
        for route in self._error_code_triggers["routes"]:
            if (
                route["chain"] == chain
                and route["game_type"] == game_type
                and route["metric"] == metric
            ):
                return deepcopy(route)
        raise ContractError(
            f"error-code trigger is not registered: "
            f"{chain}/{game_type}/{metric}"
        )

    def result_schema(self, schema_id: str) -> dict[str, Any]:
        schema = self._result_schemas["schemas"].get(schema_id)
        if not isinstance(schema, dict):
            raise ContractError(f"unknown result schema: {schema_id}")
        return deepcopy(schema)

    def secondary_relation_children(
        self, chain: str, parent_dimension: str = "game_id"
    ) -> tuple[str, ...]:
        chain_relations = self._secondary_relations["relations"].get(chain)
        if not isinstance(chain_relations, dict):
            raise ContractError(f"secondary chain is not registered: {chain}")
        children = chain_relations.get(parent_dimension)
        if not isinstance(children, list) or not children:
            raise ContractError(
                f"secondary parent is not registered: {chain}/{parent_dimension}"
            )
        return tuple(children)

    def secondary_binding(
        self,
        *,
        chain: str,
        metric: str,
        parent_dimension: str,
        parent_value: str,
        child_dimension: str,
    ) -> QueryBinding:
        if child_dimension not in self.secondary_relation_children(
            chain, parent_dimension
        ):
            raise ContractError(
                f"secondary relation is not registered: "
                f"{chain}/{parent_dimension}->{child_dimension}"
            )
        secondary = self._registry["secondary"]
        parent_raw = secondary["parent_dimensions"][chain][parent_dimension]
        parent_config = self._materialize_dimension_config(parent_raw)
        child_config = self._dimension_config(chain, child_dimension)
        if chain == "download":
            metric_config = self._registry["download_metrics"].get(metric)
            if not isinstance(metric_config, dict):
                raise ContractError(f"download metric is not registered: {metric}")
            metric_projection = metric_config["secondary_metric"]
        elif chain == "install":
            metric_config = self._registry["install"]
            if metric != metric_config["metric"]:
                raise ContractError(f"install metric is not registered: {metric}")
            metric_projection = None
        else:
            raise ContractError(f"secondary chain is not registered: {chain}")
        dimension_config = {
            "secondary": True,
            "parent_dimension": parent_dimension,
            "parent_value": parent_value,
            "parent_source_field": parent_config["source_field"],
            "parent_quality_source_expression": parent_config[
                "quality_source_expression"
            ],
            "parent_value_expression": parent_config["value_expression"],
            "child_dimension": child_dimension,
            "child_source_field": child_config["source_field"],
            "child_quality_source_expression": child_config[
                "quality_source_expression"
            ],
            "child_value_expression": child_config["value_expression"],
            "source_field": child_config["source_field"],
            "metric_projection": deepcopy(metric_projection),
        }
        return self._binding(
            secondary["template"]["path"],
            "secondary_markdown_template",
            metric_config,
            result_schema_id="secondary_bucket",
            dimension=child_dimension,
            dimension_config=dimension_config,
        )

    def game_background_binding(self, *, game_id: int) -> QueryBinding:
        if isinstance(game_id, bool) or not isinstance(game_id, int) or game_id <= 0:
            raise ContractError("game background game_id must be a positive integer")
        config = self._registry["game_background"]
        return self._binding(
            config["query"]["path"],
            "query_spec",
            config,
            result_schema_id="game_background_events",
            dimension_config={
                "post_primary": "game_background",
                "game_id": game_id,
            },
        )

    def query_spec_result_contract(
        self, binding: QueryBinding
    ) -> tuple[dict[str, str], dict[str, Any]]:
        if binding.asset_kind != "query_spec":
            raise ContractError("result columns can only be loaded from a QuerySpec")
        raw = _load_yaml_mapping(_safe_path(self.root, binding.asset_path))
        output = raw.get("output")
        quality = raw.get("quality")
        if not isinstance(output, dict) or not isinstance(quality, dict):
            raise ContractError(f"QuerySpec lacks output/quality: {binding.asset_path}")
        columns = output.get("columns")
        if not isinstance(columns, dict) or not columns:
            raise ContractError(f"QuerySpec lacks output columns: {binding.asset_path}")
        return dict(columns), deepcopy(quality)

    def verify_assets(self) -> dict[str, Any]:
        result = verify_asset_lock(self.root, self.asset_lock_path)
        actual_paths = {item["path"] for item in result["assets"]}
        if actual_paths != EXPECTED_LOCKED_ASSETS:
            missing = sorted(EXPECTED_LOCKED_ASSETS - actual_paths)
            extra = sorted(actual_paths - EXPECTED_LOCKED_ASSETS)
            raise ContractError(
                f"query asset lock coverage mismatch; missing={missing}, extra={extra}"
            )
        return result

    def select_plan(self, chain: str, game_type: str, metric: str) -> ExecutionPlan:
        if chain == "download":
            plan_id = "download"
        elif chain == "install" and game_type == "app":
            plan_id = "install_app"
        elif chain == "install" and game_type == "sandbox":
            plan_id = "install_sandbox"
        else:
            raise ContractError(
                f"unsupported chain/game_type combination: {chain}/{game_type}"
            )
        plan = self._plans[plan_id]
        if game_type not in plan.allowed_game_types:
            raise ContractError(f"game_type {game_type} is not allowed by plan {plan_id}")
        if metric not in plan.allowed_metrics:
            raise ContractError(f"metric {metric} is not allowed by plan {plan_id}")
        return plan

    def binding_for(
        self,
        plan: ExecutionPlan,
        step_id: str,
        metric: str,
        game_type: str,
    ) -> QueryBinding | None:
        step_ids = {step.id for step in plan.steps}
        if step_id not in step_ids:
            raise ContractError(f"step {step_id} does not belong to plan {plan.id}")

        if plan.chain == "download":
            metric_config = self._registry["download_metrics"].get(metric)
            if not isinstance(metric_config, dict):
                raise ContractError(f"download metric is not registered: {metric}")
            if step_id == "game_id":
                return self._binding(
                    metric_config["game_query"]["path"],
                    "query_spec",
                    metric_config,
                    result_schema_id="query_spec_bucket",
                )
            dimension_config = self._dimension_config("download", step_id)
            return self._binding(
                metric_config["primary_template"]["path"],
                "markdown_template",
                metric_config,
                result_schema_id="download_primary_bucket",
                dimension=step_id,
                dimension_config=dimension_config,
            )

        install_config = self._registry["install"]
        if metric != install_config["metric"]:
            raise ContractError(f"install metric is not registered: {metric}")
        if step_id == "game_id":
            return self._binding(
                install_config["game_query"]["path"],
                "query_spec",
                install_config,
                result_schema_id="query_spec_bucket",
            )
        if step_id == "install_stage":
            if game_type == "sandbox":
                return None
            allowed = install_config["stage_query"].get("allowed_game_types", [])
            if game_type not in allowed:
                raise ContractError(
                    f"install_stage query does not allow game_type {game_type}"
                )
            return self._binding(
                install_config["stage_query"]["path"],
                "query_spec",
                install_config,
                result_schema_id="install_stage",
            )
        dimension_config = self._dimension_config("install", step_id)
        return self._binding(
            install_config["primary_template"]["path"],
            "markdown_template",
            install_config,
            result_schema_id="install_primary_bucket",
            dimension=step_id,
            dimension_config=dimension_config,
        )

    def all_primary_dimension_fields(self) -> set[str]:
        fields: set[str] = set()
        for chain in ("download", "install"):
            for config in self._registry["dimensions"][chain].values():
                fields.add(config["source_field"])
        return fields

    def triage_text(self) -> str:
        return self.triage_path().read_text(encoding="utf-8")

    def triage_path(self) -> Path:
        return _safe_path(self.root, self._registry["triage_path"])

    def _binding(
        self,
        path: str,
        asset_kind: str,
        config: dict[str, Any],
        result_schema_id: str,
        dimension: str | None = None,
        dimension_config: dict[str, Any] | None = None,
    ) -> QueryBinding:
        if path not in self._asset_hashes:
            raise ContractError(f"query registry path is not locked: {path}")
        return QueryBinding(
            asset_path=path,
            asset_sha256=self._asset_hashes[path],
            asset_kind=asset_kind,
            data_sources=tuple(config.get("data_sources", [])),
            protected_tokens=tuple(config.get("protected_tokens", [])),
            required_predicates=tuple(config.get("required_predicates", [])),
            result_schema_id=result_schema_id,
            dimension=dimension,
            dimension_config=dict(dimension_config) if dimension_config else None,
        )

    def _dimension_config(self, chain: str, step_id: str) -> dict[str, Any]:
        config = self._registry["dimensions"][chain].get(step_id)
        if not isinstance(config, dict):
            raise ContractError(f"dimension is not registered for {chain}: {step_id}")
        return self._materialize_dimension_config(config)

    def _materialize_dimension_config(
        self, config: dict[str, Any]
    ) -> dict[str, Any]:
        normalizer = self._registry["dimension_normalizers"].get(config["normalizer"])
        if not isinstance(normalizer, dict):
            raise ContractError("dimension normalizer is not registered")
        result = dict(config)
        result["value_expression"] = normalizer["value_expression"]
        result["label_expression"] = normalizer["label_expression"]
        return result

    def _validate_plans(self) -> dict[str, ExecutionPlan]:
        if self._plans_raw.get("version") != 1:
            raise ContractError("execution plan version must be 1")
        raw_plans = self._plans_raw.get("plans")
        if not isinstance(raw_plans, dict) or set(raw_plans) != set(
            EXPECTED_PLAN_STEPS
        ):
            raise ContractError("execution plans must define only the three V1 plans")

        plans: dict[str, ExecutionPlan] = {}
        for plan_id, expected_steps in EXPECTED_PLAN_STEPS.items():
            raw = raw_plans[plan_id]
            if not isinstance(raw, dict):
                raise ContractError(f"plan {plan_id} must be a mapping")
            raw_steps = raw.get("steps")
            if not isinstance(raw_steps, list):
                raise ContractError(f"plan {plan_id} steps must be a list")
            actual_ids = tuple(item.get("id") for item in raw_steps)
            if actual_ids != expected_steps:
                raise ContractError(
                    f"plan {plan_id} queue mismatch: expected {expected_steps}, "
                    f"got {actual_ids}"
                )
            steps: list[PlanStep] = []
            for item in raw_steps:
                if item.get("kind") not in {"primary", "diagnostic"}:
                    raise ContractError(f"invalid step kind in {plan_id}: {item}")
                if not isinstance(item.get("produces_candidates"), bool):
                    raise ContractError(
                        f"produces_candidates must be boolean in {plan_id}"
                    )
                if item.get("failure_scope") != "step":
                    raise ContractError(
                        f"all V1 failures must be step scoped in {plan_id}"
                    )
                automatic_status = item.get("automatic_status")
                if automatic_status is not None and not (
                    plan_id == "install_sandbox"
                    and item["id"] == "install_stage"
                    and automatic_status == "skipped_not_applicable"
                    and isinstance(item.get("automatic_reason"), str)
                    and item["automatic_reason"].strip()
                ):
                    raise ContractError(
                        "only install_sandbox.install_stage may be automatic"
                    )
                steps.append(
                    PlanStep(
                        id=item["id"],
                        kind=item["kind"],
                        produces_candidates=item["produces_candidates"],
                        failure_scope=item["failure_scope"],
                        automatic_status=automatic_status,
                        automatic_reason=item.get("automatic_reason"),
                    )
                )
            allowed_game_types = raw.get("allowed_game_types")
            allowed_metrics = raw.get("allowed_metrics")
            if not isinstance(allowed_game_types, list) or not allowed_game_types:
                raise ContractError(f"plan {plan_id} has no allowed_game_types")
            if not isinstance(allowed_metrics, list) or not allowed_metrics:
                raise ContractError(f"plan {plan_id} has no allowed_metrics")
            plans[plan_id] = ExecutionPlan(
                id=plan_id,
                chain=raw.get("chain"),
                allowed_game_types=tuple(allowed_game_types),
                allowed_metrics=tuple(allowed_metrics),
                steps=tuple(steps),
                sha256=canonical_sha256(raw),
            )
        return plans

    def _validate_analysis_profiles(self) -> None:
        profiles = self._analysis_profiles
        if profiles.get("version") != 1:
            raise ContractError("analysis profile version must be 1")
        raw_profiles = profiles.get("profiles")
        if not isinstance(raw_profiles, dict) or set(raw_profiles) != (
            EXPECTED_ANALYSIS_PROFILES
        ):
            raise ContractError("analysis profiles must define primary_v1 and primary_v2")
        if profiles.get("default_profile") != "primary_v1":
            raise ContractError("primary_v1 must remain the default analysis profile")

        post_plans = self._post_primary_plans
        if post_plans.get("version") != 1 or set(post_plans.get("plans", {})) != {
            "post_primary_v1"
        }:
            raise ContractError("post-primary plans must define post_primary_v1")
        plan = post_plans["plans"]["post_primary_v1"]
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not isinstance(steps, list) or tuple(
            item.get("id") for item in steps if isinstance(item, dict)
        ) != EXPECTED_POST_PRIMARY_STEPS:
            raise ContractError("post-primary step order does not match the contract")
        expected_queries = {
            "counterfactual": ("deterministic", 0),
            "secondary": ("query", 1),
            "game_background": ("query", 3),
            "error_code": ("query", 1),
        }
        for step in steps:
            expected = expected_queries.get(step.get("id"))
            if expected is None or (step.get("kind"), step.get("max_queries")) != expected:
                raise ContractError(f"invalid post-primary step contract: {step}")
            if step.get("id") == "game_background" and step.get("selection") != {
                "dominant_counterfactual_max_games": 1,
                "cumulative_root_adverse_ratio": 0.5,
                "max_games": 3,
            }:
                raise ContractError("game background selection policy changed")
        if plan.get("max_additional_queries") != sum(
            item[1] for item in expected_queries.values()
        ):
            raise ContractError("post-primary query budget must total five")

        primary_v1 = raw_profiles["primary_v1"]
        primary_v2 = raw_profiles["primary_v2"]
        if primary_v1 != {
            "primary_plan": "fixed_queue_v1",
            "post_primary_plan": None,
            "enabled_post_primary_steps": [],
        }:
            raise ContractError("primary_v1 profile behavior must remain frozen")
        if (
            primary_v2.get("primary_plan") != "fixed_queue_v1"
            or primary_v2.get("post_primary_plan") != "post_primary_v1"
            or primary_v2.get("enabled_post_primary_steps")
            != ["counterfactual", "secondary", "game_background"]
        ):
            raise ContractError(
                "primary_v2 must enable counterfactual, secondary, and game background"
            )

    def _validate_error_code_contracts(self) -> None:
        capabilities = self._error_code_capabilities
        if set(capabilities) != {
            "version",
            "module",
            "runtime_enabled",
            "capabilities",
        } or capabilities.get("version") != 1:
            raise ContractError("error-code capability contract is invalid")
        if capabilities.get("module") != "error_code":
            raise ContractError("error-code capability module is invalid")
        if capabilities.get("runtime_enabled") is not False:
            raise ContractError("error-code runtime must remain disabled in 1.8-A")

        scopes = capabilities.get("capabilities")
        if not isinstance(scopes, dict):
            raise ContractError("error-code capability scopes are invalid")
        expected_scopes = {
            "download_app": {
                "chain": "download",
                "game_type": "app",
                "source_status": "registered_candidate",
                "error_code_query": "allowed_after_trigger",
                "recovery_query": "disabled_until_semantics_confirmed",
                "query_asset": None,
                "supported_metrics": ["下载失败率"],
                "source": {
                    "table": "tap_dw.dwd_str_game_core_behavior_di",
                    "partition_field": "dt",
                    "freshness": "T+1",
                    "filters": {
                        "behavior_type": "game_download_failed",
                        "action": "appDownloadNewFailed",
                        "platform": "ANDROID",
                        "game_type": "app",
                        "is_risk_device": 0,
                    },
                    "code_expression": "GET_JSON_OBJECT(action_args, '$.code')",
                    "info_expression": "GET_JSON_OBJECT(action_args, '$.info')",
                    "affected_entity_key": ["dt", "device_id", "game_id"],
                    "unmatched_code_bucket": "unmatched_code",
                    "public_info_policy": "redacted_category_only",
                    "dictionary": {
                        "path": (
                            "references/"
                            "download-install-error-code-dictionary.md"
                        ),
                        "load_policy": "after_query_success_and_codes_frozen",
                    },
                },
            },
            "download_sandbox": {
                "chain": "download",
                "game_type": "sandbox",
                "source_status": "unregistered",
                "error_code_query": "disabled",
                "recovery_query": "disabled",
                "query_asset": None,
                "supported_metrics": [],
                "source": None,
                "reason": "download_sandbox_source_unregistered",
            },
            "install_app": {
                "chain": "install",
                "game_type": "app",
                "source_status": "candidate_incomplete",
                "error_code_query": "disabled",
                "recovery_query": "disabled",
                "query_asset": None,
                "supported_metrics": [],
                "source": None,
                "reason": "install_event_semantics_candidate_incomplete",
            },
            "install_sandbox": {
                "chain": "install",
                "game_type": "sandbox",
                "source_status": "unregistered",
                "error_code_query": "disabled",
                "recovery_query": "disabled",
                "query_asset": None,
                "supported_metrics": [],
                "source": None,
                "reason": "install_sandbox_source_unregistered",
            },
        }
        if scopes != expected_scopes:
            raise ContractError("error-code capability scopes changed")
        dictionary_path = scopes["download_app"]["source"]["dictionary"]["path"]
        if not _safe_path(self.root, dictionary_path).is_file():
            raise ContractError("error-code dictionary does not exist")

        triggers = self._error_code_triggers
        if set(triggers) != {"version", "module", "evidence_policy", "routes"}:
            raise ContractError("error-code trigger contract is invalid")
        if triggers.get("version") != 1 or triggers.get("module") != "error_code":
            raise ContractError("error-code trigger contract identity is invalid")
        if triggers.get("evidence_policy") != {
            "allowed_source": "frozen_root_and_attribution_evidence",
            "module_source_scan_before_trigger": False,
            "retry_signal_may_trigger": False,
        }:
            raise ContractError("error-code trigger evidence policy changed")

        routes = triggers.get("routes")
        if not isinstance(routes, list) or not all(
            isinstance(route, dict) for route in routes
        ):
            raise ContractError("error-code trigger routes are invalid")
        route_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        for route in routes:
            key = (route.get("chain"), route.get("game_type"), route.get("metric"))
            if not all(isinstance(value, str) and value for value in key):
                raise ContractError("error-code trigger route identity is invalid")
            if key in route_map:
                raise ContractError(f"duplicate error-code trigger route: {key}")
            route_map[key] = route

        expected_route_keys = {
            (plan.chain, game_type, metric)
            for plan in self._plans.values()
            for game_type in plan.allowed_game_types
            for metric in plan.allowed_metrics
        }
        if set(route_map) != expected_route_keys:
            raise ContractError("error-code trigger routes do not cover all plans")

        registered_key = ("download", "app", "下载失败率")
        if route_map[registered_key] != {
            "chain": "download",
            "game_type": "app",
            "metric": "下载失败率",
            "capability": "download_app",
            "status": "registered_not_enabled",
            "trigger_id": "download_app_failed_entity_rate",
            "requirements": {
                "legal_primary_candidate": True,
                "evidence_source": "frozen_root_metric",
                "root_adverse_delta_bp": {
                    "operator": "at_least",
                    "value": 5,
                },
                "current_affected_entity_count": {
                    "operator": "at_least",
                    "value": 100,
                },
            },
        }:
            raise ContractError("registered error-code trigger changed")

        disabled_reasons = {
            ("download", "app", "下载完成率"): (
                "explicit_failed_signal_not_frozen"
            ),
            ("download", "app", "下载失败次数比率"): (
                "affected_entity_count_not_frozen"
            ),
            ("download", "app", "下载人为停止率"): (
                "explicit_failed_signal_not_frozen"
            ),
            ("download", "sandbox", "下载完成率"): (
                "download_sandbox_source_unregistered"
            ),
            ("download", "sandbox", "下载失败率"): (
                "download_sandbox_source_unregistered"
            ),
            ("download", "sandbox", "下载失败次数比率"): (
                "download_sandbox_source_unregistered"
            ),
            ("download", "sandbox", "下载人为停止率"): (
                "download_sandbox_source_unregistered"
            ),
            ("install", "app", "下载安装完成率"): (
                "install_event_semantics_candidate_incomplete"
            ),
            ("install", "sandbox", "下载安装完成率"): (
                "install_sandbox_source_unregistered"
            ),
        }
        for key, reason in disabled_reasons.items():
            chain, game_type, metric = key
            capability_id = f"{chain}_{game_type}"
            if route_map[key] != {
                "chain": chain,
                "game_type": game_type,
                "metric": metric,
                "capability": capability_id,
                "status": "disabled",
                "reason": reason,
            }:
                raise ContractError(f"disabled error-code trigger changed: {key}")

    def _load_asset_hashes(self) -> dict[str, str]:
        if self._asset_lock.get("version") != 1:
            raise ContractError("query asset lock version must be 1")
        assets = self._asset_lock.get("assets")
        if not isinstance(assets, list):
            raise ContractError("query asset lock assets must be a list")
        result: dict[str, str] = {}
        for item in assets:
            if not isinstance(item, dict):
                raise ContractError("query asset lock entries must be objects")
            path = item.get("path")
            digest = item.get("sha256")
            if path in result:
                raise ContractError(f"duplicate locked asset: {path}")
            if not isinstance(path, str) or not isinstance(digest, str):
                raise ContractError("locked asset path/hash must be strings")
            result[path] = digest
        if set(result) != EXPECTED_LOCKED_ASSETS:
            raise ContractError("query asset lock does not cover the V1 asset set")
        return result

    def _validate_metric_definitions(self) -> dict[str, dict[str, Any]]:
        bundle = self._metric_definitions
        if bundle.get("schema_version") != 1:
            raise ContractError("metric definition lock schema version must be 1")
        expected_hash = bundle.get("bundle_sha256")
        unsigned = dict(bundle)
        unsigned.pop("bundle_sha256", None)
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise ContractError("metric definition bundle hash is invalid")
        if expected_hash != canonical_sha256(unsigned):
            raise ContractError("metric definition bundle integrity check failed")
        if set(bundle) != {"schema_version", "metrics", "bundle_sha256"}:
            raise ContractError("metric definition bundle fields are invalid")

        expected_metrics = {
            metric for plan in self._plans.values() for metric in plan.allowed_metrics
        }
        raw_metrics = bundle.get("metrics")
        if not isinstance(raw_metrics, list) or len(raw_metrics) != len(
            expected_metrics
        ):
            raise ContractError("metric definition bundle coverage is invalid")
        result: dict[str, dict[str, Any]] = {}
        for item in raw_metrics:
            if not isinstance(item, dict) or set(item) != {
                "metric",
                "direction",
                "observation_window",
                "source_definition_path",
                "source_definition_sha256",
            }:
                raise ContractError("compiled metric definition fields are invalid")
            metric = item.get("metric")
            if not isinstance(metric, str) or metric in result:
                raise ContractError("compiled metric definition identity is invalid")
            if item.get("direction") not in {
                "higher_is_better",
                "lower_is_better",
            }:
                raise ContractError(f"compiled metric direction is invalid: {metric}")
            windows = item.get("observation_window")
            if not isinstance(windows, dict) or set(windows) != {"app", "sandbox"}:
                raise ContractError(
                    f"compiled metric observation windows are invalid: {metric}"
                )
            if not all(
                isinstance(value, str) and value.strip() for value in windows.values()
            ):
                raise ContractError(
                    f"compiled metric observation window is empty: {metric}"
                )
            source_path = item.get("source_definition_path")
            source_hash = item.get("source_definition_sha256")
            if (
                not isinstance(source_path, str)
                or not source_path.startswith("knowledge-base/")
                or not isinstance(source_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
            ):
                raise ContractError(
                    f"compiled metric source identity is invalid: {metric}"
                )
            result[metric] = deepcopy(item)
        if set(result) != expected_metrics:
            raise ContractError("metric definition bundle does not cover V1 metrics")
        return result

    def _validate_registry(self) -> None:
        registry = self._registry
        if registry.get("version") != 1:
            raise ContractError("query registry version must be 1")
        if registry.get("execution_mode") != "trusted_host_adapter":
            raise ContractError(
                "V1 query registry must use the trusted Host adapter"
            )
        triage_path = registry.get("triage_path")
        if not _safe_path(self.root, triage_path).is_file():
            raise ContractError("registered SQL triage file does not exist")

        download_metrics = registry.get("download_metrics")
        expected_download_metrics = set(self._plans["download"].allowed_metrics)
        if not isinstance(download_metrics, dict) or set(download_metrics) != (
            expected_download_metrics
        ):
            raise ContractError("download registry must map every allowed metric once")
        for metric, config in download_metrics.items():
            self._validate_query_config(metric, config, ("game_query", "primary_template"))

        install = registry.get("install")
        if not isinstance(install, dict):
            raise ContractError("install query registry must be a mapping")
        if install.get("metric") != "下载安装完成率":
            raise ContractError("install registry metric mismatch")
        self._validate_query_config(
            "install", install, ("game_query", "stage_query", "primary_template")
        )
        if install["stage_query"].get("allowed_game_types") != ["app"]:
            raise ContractError("install stage query must be app-only")

        dimensions = registry.get("dimensions")
        if not isinstance(dimensions, dict):
            raise ContractError("dimension registry must be a mapping")
        expected_dimensions = {
            "download": EXPECTED_PLAN_STEPS["download"][1:],
            "install": tuple(
                step
                for step in EXPECTED_PLAN_STEPS["install_app"]
                if step not in {"game_id", "install_stage"}
            ),
        }
        normalizers = registry.get("dimension_normalizers")
        if not isinstance(normalizers, dict) or set(normalizers) != {
            "standard",
            "reserve_binary",
        }:
            raise ContractError("dimension normalizers are incomplete")
        for chain, expected in expected_dimensions.items():
            chain_dimensions = dimensions.get(chain)
            if not isinstance(chain_dimensions, dict) or tuple(chain_dimensions) != expected:
                raise ContractError(f"{chain} dimension order does not match its plan")
            for dimension, config in chain_dimensions.items():
                if not isinstance(config, dict):
                    raise ContractError(f"invalid dimension config: {dimension}")
                source_field = config.get("source_field")
                quality_expression = config.get("quality_source_expression")
                if not isinstance(source_field, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", source_field
                ):
                    raise ContractError(f"invalid source field for {dimension}")
                if not isinstance(quality_expression, str) or not re.fullmatch(
                    r"1|[A-Za-z_][A-Za-z0-9_]*", quality_expression
                ):
                    raise ContractError(f"invalid quality expression for {dimension}")
                if config.get("normalizer") not in normalizers:
                    raise ContractError(f"unknown normalizer for {dimension}")
                expected_template = (
                    "download_primary" if chain == "download" else "install_primary"
                )
                if config.get("allowed_template") != expected_template:
                    raise ContractError(f"template mismatch for {dimension}")

        secondary = registry.get("secondary")
        if not isinstance(secondary, dict) or set(secondary) != {
            "template",
            "parent_dimensions",
        }:
            raise ContractError("secondary query registry is invalid")
        template = secondary["template"]
        if (
            not isinstance(template, dict)
            or template.get("path")
            != "references/queries/secondary-attribution-template.md"
            or template["path"] not in self._asset_hashes
        ):
            raise ContractError("secondary template is not a locked query asset")
        parent_dimensions = secondary["parent_dimensions"]
        if not isinstance(parent_dimensions, dict) or set(parent_dimensions) != {
            "download",
            "install",
        }:
            raise ContractError("secondary parent dimensions are incomplete")
        for chain, parents in parent_dimensions.items():
            if not isinstance(parents, dict) or set(parents) != {"game_id"}:
                raise ContractError(
                    f"{chain} secondary must register only game_id as a parent"
                )
            parent = parents["game_id"]
            if parent != {
                "source_field": "game_id",
                "quality_source_expression": "1",
                "normalizer": "standard",
            }:
                raise ContractError(f"{chain} game_id parent contract changed")

        game_background = registry.get("game_background")
        self._validate_query_config(
            "game_background", game_background, ("query",)
        )
        if game_background != {
            "query": {
                "path": "references/queries/game-operation-events.yaml"
            },
            "data_sources": [
                "tap_bi.dwd_app_operation_events_df",
                "tap_dw.dwt_game_detail_info_view_df",
            ],
            "protected_tokens": [
                "app_id",
                "game_id",
                "event_date0",
                "transition_evidence",
                "source_snapshot_dt",
            ],
        }:
            raise ContractError("game background query registry changed")

        for metric, config in download_metrics.items():
            projection = config.get("secondary_metric")
            if not isinstance(projection, dict) or set(projection) != {
                "denominator_source_field",
                "numerator_source_field",
                "invalid_metric_predicate",
            }:
                raise ContractError(
                    f"download secondary metric projection is invalid: {metric}"
                )
            if any(
                not isinstance(value, str) or not value.strip()
                for value in projection.values()
            ):
                raise ContractError(
                    f"download secondary metric projection is empty: {metric}"
                )

    def _validate_secondary_relations(self) -> None:
        contract = self._secondary_relations
        if contract.get("version") != 1 or set(contract) != {
            "version",
            "relations",
        }:
            raise ContractError("secondary relations contract version is invalid")
        expected = {
            "download": (
                "device_brand",
                "channel_group",
                "app_major_version",
                "os_major_version",
                "apk_size_tier",
            ),
            "install": (
                "device_brand",
                "storage_headroom_tier",
                "os_major_version",
                "apk_size_tier",
            ),
        }
        relations = contract.get("relations")
        if not isinstance(relations, dict) or set(relations) != set(expected):
            raise ContractError("secondary relation chains are invalid")
        for chain, children in expected.items():
            chain_relations = relations[chain]
            if (
                not isinstance(chain_relations, dict)
                or set(chain_relations) != {"game_id"}
                or tuple(chain_relations["game_id"]) != children
            ):
                raise ContractError(f"{chain} secondary relation order changed")
            if any(
                child not in self._registry["dimensions"][chain]
                for child in children
            ):
                raise ContractError(
                    f"{chain} secondary relation references an unknown child"
                )

    def _validate_query_config(
        self, name: str, config: Any, required_assets: tuple[str, ...]
    ) -> None:
        if not isinstance(config, dict):
            raise ContractError(f"query registry entry must be a mapping: {name}")
        for key in required_assets:
            asset = config.get(key)
            if not isinstance(asset, dict) or not isinstance(asset.get("path"), str):
                raise ContractError(f"missing {key} path for {name}")
            path = asset["path"]
            if path not in self._asset_hashes:
                raise ContractError(f"unlocked query path for {name}: {path}")
            if not _safe_path(self.root, path).is_file():
                raise ContractError(f"registered query path does not exist: {path}")
        for field in ("data_sources", "protected_tokens"):
            values = config.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ContractError(f"{field} must be a non-empty string list for {name}")

    def _validate_result_schemas(self) -> None:
        result = self._result_schemas
        if result.get("version") != 1:
            raise ContractError("result schema version must be 1")
        defaults = result.get("defaults")
        metrics = result.get("metrics")
        schemas = result.get("schemas")
        if not isinstance(defaults, dict):
            raise ContractError("result schema defaults must be a mapping")
        if not isinstance(metrics, dict) or set(metrics) != {
            metric
            for plan in self._plans.values()
            for metric in plan.allowed_metrics
        }:
            raise ContractError("result schemas must cover every registered metric")
        if not isinstance(schemas, dict) or set(schemas) != {
            "query_spec_bucket",
            "download_primary_bucket",
            "install_primary_bucket",
            "install_stage",
            "secondary_bucket",
            "game_background_events",
        }:
            raise ContractError("result schemas do not cover every query binding kind")
        for name, config in metrics.items():
            if set(config) != {"numerator_subset"} or not isinstance(
                config.get("numerator_subset"), bool
            ):
                raise ContractError(f"invalid metric result contract: {name}")
        for schema_id, schema in schemas.items():
            if schema.get("validator") not in {
                "contribution_buckets",
                "install_stage",
                "secondary_contribution_buckets",
                "game_background_events",
            }:
                raise ContractError(f"invalid result validator: {schema_id}")
            columns = schema.get("columns")
            if columns is not None and (
                not isinstance(columns, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in columns.items()
                )
            ):
                raise ContractError(f"invalid result columns: {schema_id}")
            columns_by_chain = schema.get("columns_by_chain")
            if columns_by_chain is not None:
                if (
                    schema_id != "secondary_bucket"
                    or not isinstance(columns_by_chain, dict)
                    or set(columns_by_chain) != {"download", "install"}
                    or any(
                        not isinstance(chain_columns, dict)
                        or not chain_columns
                        or any(
                            not isinstance(key, str) or not isinstance(value, str)
                            for key, value in chain_columns.items()
                        )
                        for chain_columns in columns_by_chain.values()
                    )
                ):
                    raise ContractError("secondary result columns are invalid")
