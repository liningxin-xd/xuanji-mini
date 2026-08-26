from __future__ import annotations

import hashlib
import json
import re
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

        self._plans_raw = _load_yaml_mapping(self.execution_plans_path)
        self._registry = _load_yaml_mapping(self.query_registry_path)
        self._asset_lock = _load_json_mapping(self.asset_lock_path)
        self._plans = self._validate_plans()
        self._asset_hashes = self._load_asset_hashes()
        self._validate_registry()

    @property
    def registry(self) -> dict[str, Any]:
        return self._registry

    @property
    def plans(self) -> dict[str, ExecutionPlan]:
        return dict(self._plans)

    @property
    def asset_hashes(self) -> dict[str, str]:
        return dict(self._asset_hashes)

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
                )
            dimension_config = self._dimension_config("download", step_id)
            return self._binding(
                metric_config["primary_template"]["path"],
                "markdown_template",
                metric_config,
                step_id,
                dimension_config,
            )

        install_config = self._registry["install"]
        if metric != install_config["metric"]:
            raise ContractError(f"install metric is not registered: {metric}")
        if step_id == "game_id":
            return self._binding(
                install_config["game_query"]["path"],
                "query_spec",
                install_config,
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
            )
        dimension_config = self._dimension_config("install", step_id)
        return self._binding(
            install_config["primary_template"]["path"],
            "markdown_template",
            install_config,
            step_id,
            dimension_config,
        )

    def all_primary_dimension_fields(self) -> set[str]:
        fields: set[str] = set()
        for chain in ("download", "install"):
            for config in self._registry["dimensions"][chain].values():
                fields.add(config["source_field"])
        return fields

    def triage_text(self) -> str:
        path = _safe_path(self.root, self._registry["triage_path"])
        return path.read_text(encoding="utf-8")

    def _binding(
        self,
        path: str,
        asset_kind: str,
        config: dict[str, Any],
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
            dimension=dimension,
            dimension_config=dict(dimension_config) if dimension_config else None,
        )

    def _dimension_config(self, chain: str, step_id: str) -> dict[str, Any]:
        config = self._registry["dimensions"][chain].get(step_id)
        if not isinstance(config, dict):
            raise ContractError(f"dimension is not registered for {chain}: {step_id}")
        normalizer = self._registry["dimension_normalizers"].get(
            config["normalizer"]
        )
        if not isinstance(normalizer, dict):
            raise ContractError(f"normalizer is not registered for {step_id}")
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

    def _validate_registry(self) -> None:
        registry = self._registry
        if registry.get("version") != 1:
            raise ContractError("query registry version must be 1")
        if registry.get("execution_mode") != "task_ticket":
            raise ContractError("V1 query registry must use task_ticket mode")
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
