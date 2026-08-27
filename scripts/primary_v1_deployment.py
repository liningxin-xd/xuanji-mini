from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "deploy" / "primary-v1" / "manifests.yaml"
TEMPLATE_IMAGE = (
    "xuanji-primary-v1.invalid/repository@sha256:"
    + "0" * 64
)
_DIGEST_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_CONFIG_NAME = "xuanji-primary-v1-config"
_WORKLOAD_NAME = "xuanji-primary-v1"
_SECRET_REFS = {
    "XUANJI_HOST_BEARER_TOKEN": ("xuanji-primary-v1-host-auth", "bearer-token"),
    "XUANJI_DVIEW_BEARER_TOKEN": (
        "xuanji-primary-v1-dview-readonly",
        "bearer-token",
    ),
    "XUANJI_RECEIPT_SECRET": ("xuanji-primary-v1-receipt-auth", "secret"),
}
_ROOTS = {
    "XUANJI_RUNS_ROOT": "/var/lib/xuanji/runs",
    "XUANJI_TASKS_ROOT": "/var/lib/xuanji/tasks",
    "XUANJI_RESULTS_ROOT": "/var/lib/xuanji/results",
}


class DeploymentContractError(ValueError):
    pass


def load_documents(path: Path | str) -> list[dict[str, Any]]:
    try:
        documents = list(yaml.safe_load_all(Path(path).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise DeploymentContractError("deployment manifest cannot be loaded") from exc
    if not documents or any(not isinstance(item, dict) for item in documents):
        raise DeploymentContractError("deployment manifest contains an invalid document")
    return documents


def render_documents(
    documents: list[dict[str, Any]],
    *,
    image: str,
    host_public_url: str,
    dview_mcp_url: str,
) -> list[dict[str, Any]]:
    validate_documents(documents, allow_template_image=True)
    _validate_image(image, allow_template=False)
    rendered = deepcopy(documents)
    deployment = _one(rendered, "Deployment", _WORKLOAD_NAME)
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = image
    config = _one(rendered, "ConfigMap", _CONFIG_NAME)["data"]
    config["XUANJI_HOST_PUBLIC_URL"] = _validate_url(
        host_public_url, "host public URL"
    )
    config["XUANJI_DVIEW_MCP_URL"] = _validate_url(dview_mcp_url, "DView MCP URL")
    validate_documents(rendered, allow_template_image=False)
    return rendered


def validate_documents(
    documents: list[dict[str, Any]],
    *,
    allow_template_image: bool,
) -> None:
    if any(item.get("kind") == "Secret" for item in documents):
        raise DeploymentContractError("deployment source must not contain Secret values")

    service_account = _one(documents, "ServiceAccount", _WORKLOAD_NAME)
    if service_account.get("automountServiceAccountToken") is not False:
        raise DeploymentContractError("service account token automount must be disabled")

    config = _one(documents, "ConfigMap", _CONFIG_NAME).get("data")
    if not isinstance(config, dict):
        raise DeploymentContractError("deployment ConfigMap data is invalid")
    for name, value in _ROOTS.items():
        if config.get(name) != value:
            raise DeploymentContractError(f"{name} must remain {value}")
    _validate_url(config.get("XUANJI_HOST_PUBLIC_URL"), "host public URL")
    _validate_url(config.get("XUANJI_DVIEW_MCP_URL"), "DView MCP URL")

    pvc = _one(documents, "PersistentVolumeClaim", "xuanji-primary-v1-data")
    if (
        pvc.get("spec", {}).get("accessModes") != ["ReadWriteOnce"]
        or pvc.get("spec", {}).get("resources", {}).get("requests", {}).get(
            "storage"
        )
        != "10Gi"
    ):
        raise DeploymentContractError("primary_v1 PVC must be ReadWriteOnce")

    service = _one(documents, "Service", _WORKLOAD_NAME)
    if service.get("spec") != {
        "type": "ClusterIP",
        "selector": {"app.kubernetes.io/name": _WORKLOAD_NAME},
        "ports": [
            {"name": "mcp", "port": 8091, "targetPort": "mcp", "protocol": "TCP"}
        ],
    }:
        raise DeploymentContractError("primary_v1 Service must be cluster-internal")

    network_policy = _one(
        documents, "NetworkPolicy", "xuanji-primary-v1-ingress"
    )
    if network_policy.get("spec") != {
        "podSelector": {
            "matchLabels": {"app.kubernetes.io/name": _WORKLOAD_NAME}
        },
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [
                    {
                        "podSelector": {
                            "matchLabels": {"xuanji.taptap/client": "true"}
                        }
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 8091}],
            }
        ],
    }:
        raise DeploymentContractError("primary_v1 ingress must be explicitly restricted")

    deployment = _one(documents, "Deployment", _WORKLOAD_NAME)
    spec = deployment.get("spec", {})
    if spec.get("replicas") != 1 or spec.get("strategy") != {"type": "Recreate"}:
        raise DeploymentContractError("primary_v1 must run one Recreate replica")
    pod = spec.get("template", {}).get("spec", {})
    if any(pod.get(field) for field in ("hostNetwork", "hostPID", "hostIPC")):
        raise DeploymentContractError("primary_v1 cannot share host namespaces")
    if (
        pod.get("serviceAccountName") != _WORKLOAD_NAME
        or pod.get("automountServiceAccountToken") is not False
        or pod.get("terminationGracePeriodSeconds") != 720
    ):
        raise DeploymentContractError("primary_v1 pod service account is invalid")
    pod_security = pod.get("securityContext", {})
    expected_security = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    if pod_security != expected_security:
        raise DeploymentContractError("primary_v1 pod security context changed")

    containers = pod.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise DeploymentContractError("primary_v1 must contain exactly one container")
    container = containers[0]
    _validate_image(container.get("image"), allow_template=allow_template_image)
    if container.get("imagePullPolicy") != "IfNotPresent":
        raise DeploymentContractError("digest images must use IfNotPresent")
    container_security = container.get("securityContext", {})
    if container_security != {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }:
        raise DeploymentContractError("primary_v1 container security context changed")
    if not all(
        isinstance(container.get(name), dict)
        and isinstance(container[name].get("exec"), dict)
        for name in ("startupProbe", "readinessProbe", "livenessProbe")
    ):
        raise DeploymentContractError("primary_v1 probes must run inside the pod")

    env_from = container.get("envFrom")
    if env_from != [{"configMapRef": {"name": _CONFIG_NAME}}]:
        raise DeploymentContractError("primary_v1 ConfigMap reference changed")
    env = container.get("env")
    if not isinstance(env, list):
        raise DeploymentContractError("primary_v1 secret environment is invalid")
    actual_refs: dict[str, tuple[str, str]] = {}
    for item in env:
        ref = item.get("valueFrom", {}).get("secretKeyRef", {})
        actual_refs[item.get("name")] = (ref.get("name"), ref.get("key"))
    if actual_refs != _SECRET_REFS:
        raise DeploymentContractError("primary_v1 must reference three independent secrets")
    if len({name for name, _ in actual_refs.values()}) != 3:
        raise DeploymentContractError("primary_v1 secret resources must be distinct")

    mounts = {
        item.get("name"): item.get("mountPath")
        for item in container.get("volumeMounts", [])
    }
    if mounts != {"data": "/var/lib/xuanji", "tmp": "/tmp"}:
        raise DeploymentContractError("primary_v1 writable mounts changed")
    volumes = {item.get("name"): item for item in pod.get("volumes", [])}
    if volumes.get("data", {}).get("persistentVolumeClaim", {}).get(
        "claimName"
    ) != "xuanji-primary-v1-data" or "emptyDir" not in volumes.get("tmp", {}):
        raise DeploymentContractError("primary_v1 volume contract changed")


def dump_documents(documents: list[dict[str, Any]]) -> str:
    return yaml.safe_dump_all(
        documents,
        allow_unicode=False,
        explicit_start=True,
        sort_keys=False,
    )


def _one(
    documents: list[dict[str, Any]], kind: str, name: str
) -> dict[str, Any]:
    matches = [
        item
        for item in documents
        if item.get("kind") == kind
        and item.get("metadata", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise DeploymentContractError(f"deployment requires one {kind}/{name}")
    return matches[0]


def _validate_image(value: Any, *, allow_template: bool) -> str:
    if not isinstance(value, str) or _DIGEST_IMAGE.fullmatch(value) is None:
        raise DeploymentContractError("production image must use repository@sha256:digest")
    if value == TEMPLATE_IMAGE:
        if allow_template:
            return value
        raise DeploymentContractError("template image digest must be replaced")
    if value.endswith("@sha256:" + "0" * 64):
        raise DeploymentContractError("production image digest cannot be all zeroes")
    return value


def _validate_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise DeploymentContractError(f"{label} must be an absolute HTTP(S) URL")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DeploymentContractError(f"{label} must be an absolute HTTP(S) URL")
    return value.rstrip("/")


def _write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify or render the primary_v1 production deployment."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    verify.add_argument("--allow-template-image", action="store_true")
    render = subparsers.add_parser("render")
    render.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    render.add_argument("--image", required=True)
    render.add_argument("--host-public-url", required=True)
    render.add_argument("--dview-mcp-url", required=True)
    render.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    try:
        documents = load_documents(args.manifest)
        if args.command == "verify":
            validate_documents(
                documents,
                allow_template_image=args.allow_template_image,
            )
            print(f"primary_v1 deployment verified: {args.manifest}")
            return 0
        rendered = render_documents(
            documents,
            image=args.image,
            host_public_url=args.host_public_url,
            dview_mcp_url=args.dview_mcp_url,
        )
        content = dump_documents(rendered)
        if args.output == "-":
            sys.stdout.write(content)
        else:
            output = Path(args.output)
            _write_output(output, content)
            print(f"primary_v1 deployment rendered: {output}")
        return 0
    except DeploymentContractError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
