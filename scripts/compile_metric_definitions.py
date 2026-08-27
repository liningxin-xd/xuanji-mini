from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "contracts" / "metric-definitions.lock.json"
DOMAIN = "商店（移动端）"
METRIC_DIRECTIONS = {
    "下载完成率": "higher_is_better",
    "下载失败率": "lower_is_better",
    "下载失败次数比率": "lower_is_better",
    "下载人为停止率": "lower_is_better",
    "下载安装完成率": "higher_is_better",
}
_SCOPE_LABELS = {"app": "APK", "sandbox": "沙盒"}


class DefinitionCompileError(ValueError):
    pass


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DefinitionCompileError(f"cannot load knowledge-base YAML: {path}") from exc


def _safe_path(root: Path, relative_path: Any) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise DefinitionCompileError("knowledge-base path must be non-empty")
    resolved_root = root.resolve()
    path = (resolved_root / relative_path).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise DefinitionCompileError("knowledge-base path escapes its skill root") from exc
    return path


def _observation_windows(metric: str, definition: dict[str, Any]) -> dict[str, str]:
    names = definition.get("standard_name")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not all(
        isinstance(item, str) and item.strip() for item in names
    ):
        raise DefinitionCompileError(f"metric lacks standard_name windows: {metric}")

    result = {}
    for scope, label in _SCOPE_LABELS.items():
        matches = [item for item in names if label in item]
        if len(matches) != 1 or "_" not in matches[0]:
            raise DefinitionCompileError(
                f"metric must define exactly one {scope} observation window: {metric}"
            )
        window = matches[0].split("_", 1)[0].strip()
        if not window:
            raise DefinitionCompileError(f"metric observation window is empty: {metric}")
        result[scope] = window
    return result


def compile_bundle(skill_root: Path | str) -> dict[str, Any]:
    root = Path(skill_root).absolute()
    knowledge_base_root = (root / "knowledge-base").resolve()
    manifest_path = knowledge_base_root / "manifest.yaml"
    manifest = _load_yaml(manifest_path)
    if not isinstance(manifest, dict):
        raise DefinitionCompileError("knowledge-base manifest must be an object")
    domains = manifest.get("domains")
    if not isinstance(domains, list):
        raise DefinitionCompileError("knowledge-base manifest lacks domains")
    matches = [item for item in domains if isinstance(item, dict) and item.get("name") == DOMAIN]
    if len(matches) != 1:
        raise DefinitionCompileError(f"knowledge-base must contain one domain: {DOMAIN}")

    index_path = _safe_path(knowledge_base_root, matches[0].get("metric_index"))
    metric_index = _load_yaml(index_path)
    if not isinstance(metric_index, list):
        raise DefinitionCompileError("metric index must be a list")

    compiled_metrics = []
    for metric, direction in METRIC_DIRECTIONS.items():
        entries = [
            item
            for item in metric_index
            if isinstance(item, dict)
            and isinstance(item.get("aliases"), list)
            and metric in item["aliases"]
        ]
        if len(entries) != 1:
            raise DefinitionCompileError(
                f"metric index must resolve exactly one definition: {metric}"
            )
        definition_path = _safe_path(index_path.parent, entries[0].get("file"))
        definition = _load_yaml(definition_path)
        if not isinstance(definition, dict) or definition.get("metric") != metric:
            raise DefinitionCompileError(f"metric definition identity mismatch: {metric}")
        for required in ("业务口径", "技术口径", "sql"):
            if not definition.get(required):
                raise DefinitionCompileError(
                    f"metric definition lacks required field {required}: {metric}"
                )
        compiled_metrics.append(
            {
                "metric": metric,
                "direction": direction,
                "observation_window": _observation_windows(metric, definition),
                "source_definition_path": (
                    Path("knowledge-base")
                    / definition_path.relative_to(knowledge_base_root)
                ).as_posix(),
                "source_definition_sha256": sha256_bytes(definition_path.read_bytes()),
            }
        )

    bundle = {
        "schema_version": 1,
        "metrics": compiled_metrics,
    }
    return {**bundle, "bundle_sha256": canonical_sha256(bundle)}


def render_bundle(bundle: dict[str, Any]) -> bytes:
    return (
        json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def write_bundle(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile the registered metric-definition bundle."
    )
    parser.add_argument(
        "--skill-root",
        default=os.environ.get("TAPTAP_DATA_ANALYSIS_SKILL_ROOT"),
        help="Path to the taptap-data-analysis skill root.",
    )
    parser.add_argument("--output", type=Path, default=LOCK_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.skill_root:
        parser.error(
            "--skill-root or TAPTAP_DATA_ANALYSIS_SKILL_ROOT is required"
        )

    try:
        content = render_bundle(compile_bundle(args.skill_root))
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != content:
                raise DefinitionCompileError(
                    "compiled metric definitions differ from the committed lock"
                )
        else:
            write_bundle(args.output, content)
    except DefinitionCompileError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"metric definition lock verified: {args.output}" if args.check else f"metric definition lock written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
