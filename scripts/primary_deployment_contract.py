from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
_DIGEST_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_ROOTS = {
    "XUANJI_RUNS_ROOT": "/var/lib/xuanji/runs",
    "XUANJI_TASKS_ROOT": "/var/lib/xuanji/tasks",
    "XUANJI_RESULTS_ROOT": "/var/lib/xuanji/results",
}


@dataclass(frozen=True)
class DeploymentProfile:
    analysis_profile: str
    resource_prefix: str
    default_manifest: Path
    template_image: str
    ingress_client_selector: tuple[tuple[str, str], ...]
    receipt_key_id: str = "xuanji-primary-host-v1"
    service_port: int = 8091

    @property
    def config_name(self) -> str:
        return f"{self.resource_prefix}-config"

    @property
    def pvc_name(self) -> str:
        return f"{self.resource_prefix}-data"

    @property
    def network_policy_name(self) -> str:
        return f"{self.resource_prefix}-ingress"

    @property
    def secret_refs(self) -> dict[str, tuple[str, str]]:
        return {
            "XUANJI_HOST_BEARER_TOKEN": (
                f"{self.resource_prefix}-host-auth",
                "bearer-token",
            ),
            "XUANJI_DVIEW_BEARER_TOKEN": (
                f"{self.resource_prefix}-dview-readonly",
                "bearer-token",
            ),
            "XUANJI_RECEIPT_SECRET": (
                f"{self.resource_prefix}-receipt-auth",
                "secret",
            ),
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
    profile: DeploymentProfile,
    image: str,
    host_public_url: str,
    dview_mcp_url: str,
) -> list[dict[str, Any]]:
    validate_documents(documents, profile=profile, allow_template_image=True)
    _validate_image(image, profile=profile, allow_template=False)
    rendered = deepcopy(documents)
    deployment = _one(rendered, "Deployment", profile.resource_prefix)
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = image
    config = _one(rendered, "ConfigMap", profile.config_name)["data"]
    config["XUANJI_HOST_PUBLIC_URL"] = _validate_url(
        host_public_url, "host public URL"
    )
    config["XUANJI_DVIEW_MCP_URL"] = _validate_url(
        dview_mcp_url, "DView MCP URL"
    )
    validate_documents(rendered, profile=profile, allow_template_image=False)
    return rendered


def validate_documents(
    documents: list[dict[str, Any]],
    *,
    profile: DeploymentProfile,
    allow_template_image: bool,
) -> None:
    label = profile.analysis_profile
    if any(item.get("kind") == "Secret" for item in documents):
        raise DeploymentContractError("deployment source must not contain Secret values")
    expected_inventory = {
        ("ServiceAccount", profile.resource_prefix),
        ("ConfigMap", profile.config_name),
        ("PersistentVolumeClaim", profile.pvc_name),
        ("Service", profile.resource_prefix),
        ("NetworkPolicy", profile.network_policy_name),
        ("Deployment", profile.resource_prefix),
    }
    actual_inventory = {
        (item.get("kind"), item.get("metadata", {}).get("name"))
        for item in documents
    }
    if len(documents) != len(expected_inventory) or actual_inventory != (
        expected_inventory
    ):
        raise DeploymentContractError(
            f"{label} deployment resource inventory changed"
        )

    service_account = _one(documents, "ServiceAccount", profile.resource_prefix)
    if service_account.get("automountServiceAccountToken") is not False:
        raise DeploymentContractError("service account token automount must be disabled")

    config = _one(documents, "ConfigMap", profile.config_name).get("data")
    expected_static_config = {
        "XUANJI_HOST": "0.0.0.0",
        "XUANJI_PORT": str(profile.service_port),
        "XUANJI_LOG_LEVEL": "INFO",
        "XUANJI_ANALYSIS_PROFILE": label,
        "XUANJI_DVIEW_READ_TIMEOUT_SECONDS": "660",
        "XUANJI_RECEIPT_KEY_ID": profile.receipt_key_id,
        **_ROOTS,
    }
    if not isinstance(config, dict):
        raise DeploymentContractError("deployment ConfigMap data is invalid")
    if set(config) != {
        "XUANJI_HOST_PUBLIC_URL",
        "XUANJI_DVIEW_MCP_URL",
        *expected_static_config,
    }:
        raise DeploymentContractError(f"{label} ConfigMap inventory changed")
    for name, value in expected_static_config.items():
        if config.get(name) != value:
            raise DeploymentContractError(f"{name} must remain {value}")
    _validate_url(config.get("XUANJI_HOST_PUBLIC_URL"), "host public URL")
    _validate_url(config.get("XUANJI_DVIEW_MCP_URL"), "DView MCP URL")

    pvc = _one(documents, "PersistentVolumeClaim", profile.pvc_name)
    if (
        pvc.get("spec", {}).get("accessModes") != ["ReadWriteOnce"]
        or pvc.get("spec", {}).get("resources", {}).get("requests", {}).get(
            "storage"
        )
        != "10Gi"
    ):
        raise DeploymentContractError(f"{label} PVC must be ReadWriteOnce")

    selector = {"app.kubernetes.io/name": profile.resource_prefix}
    service = _one(documents, "Service", profile.resource_prefix)
    if service.get("spec") != {
        "type": "ClusterIP",
        "selector": selector,
        "ports": [
            {
                "name": "mcp",
                "port": profile.service_port,
                "targetPort": "mcp",
                "protocol": "TCP",
            }
        ],
    }:
        raise DeploymentContractError(f"{label} Service must be cluster-internal")

    network_policy = _one(
        documents, "NetworkPolicy", profile.network_policy_name
    )
    if network_policy.get("spec") != {
        "podSelector": {"matchLabels": selector},
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [
                    {
                        "podSelector": {
                            "matchLabels": dict(profile.ingress_client_selector)
                        }
                    }
                ],
                "ports": [{"protocol": "TCP", "port": profile.service_port}],
            }
        ],
    }:
        raise DeploymentContractError(f"{label} ingress must be explicitly restricted")

    deployment = _one(documents, "Deployment", profile.resource_prefix)
    expected_labels = {
        "app.kubernetes.io/name": profile.resource_prefix,
        "app.kubernetes.io/component": "attribution-host",
    }
    if deployment.get("metadata", {}).get("labels") != expected_labels:
        raise DeploymentContractError(f"{label} deployment labels changed")
    spec = deployment.get("spec", {})
    if (
        spec.get("replicas") != 1
        or spec.get("strategy") != {"type": "Recreate"}
        or spec.get("selector") != {"matchLabels": selector}
    ):
        raise DeploymentContractError(f"{label} must run one Recreate replica")
    template = spec.get("template", {})
    if template.get("metadata", {}).get("labels") != expected_labels:
        raise DeploymentContractError(f"{label} pod labels changed")
    pod = template.get("spec", {})
    if any(pod.get(field) for field in ("hostNetwork", "hostPID", "hostIPC")):
        raise DeploymentContractError(f"{label} cannot share host namespaces")
    if (
        pod.get("serviceAccountName") != profile.resource_prefix
        or pod.get("automountServiceAccountToken") is not False
        or pod.get("terminationGracePeriodSeconds") != 720
    ):
        raise DeploymentContractError(f"{label} pod service account is invalid")
    expected_security = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    if pod.get("securityContext", {}) != expected_security:
        raise DeploymentContractError(f"{label} pod security context changed")

    containers = pod.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise DeploymentContractError(f"{label} must contain exactly one container")
    container = containers[0]
    _validate_image(
        container.get("image"),
        profile=profile,
        allow_template=allow_template_image,
    )
    if (
        container.get("name") != "host"
        or container.get("imagePullPolicy") != "IfNotPresent"
        or container.get("ports")
        != [
            {
                "name": "mcp",
                "containerPort": profile.service_port,
                "protocol": "TCP",
            }
        ]
        or container.get("resources")
        != {
            "requests": {"cpu": "250m", "memory": "512Mi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        }
    ):
        raise DeploymentContractError(f"{label} container contract changed")
    if container.get("securityContext", {}) != {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }:
        raise DeploymentContractError(f"{label} container security context changed")
    expected_probe = {
        "exec": {
            "command": [
                "python",
                "-c",
                "import socket; "
                f"socket.create_connection(('127.0.0.1', {profile.service_port}), "
                "2).close()",
            ]
        }
    }
    for name in ("startupProbe", "readinessProbe", "livenessProbe"):
        probe = container.get(name)
        if not isinstance(probe, dict) or probe.get("exec") != expected_probe["exec"]:
            raise DeploymentContractError(f"{label} probes must run inside the pod")

    if container.get("envFrom") != [
        {"configMapRef": {"name": profile.config_name}}
    ]:
        raise DeploymentContractError(f"{label} ConfigMap reference changed")
    env = container.get("env")
    if not isinstance(env, list):
        raise DeploymentContractError(f"{label} secret environment is invalid")
    actual_refs: dict[str, tuple[str, str]] = {}
    for item in env:
        ref = item.get("valueFrom", {}).get("secretKeyRef", {})
        actual_refs[item.get("name")] = (ref.get("name"), ref.get("key"))
    if actual_refs != profile.secret_refs:
        raise DeploymentContractError(
            f"{label} must reference three profile-owned secrets"
        )
    if len({name for name, _ in actual_refs.values()}) != 3:
        raise DeploymentContractError(f"{label} secret resources must be distinct")

    mounts = {
        item.get("name"): item.get("mountPath")
        for item in container.get("volumeMounts", [])
    }
    if mounts != {"data": "/var/lib/xuanji", "tmp": "/tmp"}:
        raise DeploymentContractError(f"{label} writable mounts changed")
    volumes = {item.get("name"): item for item in pod.get("volumes", [])}
    if volumes.get("data", {}).get("persistentVolumeClaim", {}).get(
        "claimName"
    ) != profile.pvc_name or volumes.get("tmp") != {
        "name": "tmp",
        "emptyDir": {"sizeLimit": "64Mi"},
    }:
        raise DeploymentContractError(f"{label} volume contract changed")


def dump_documents(documents: list[dict[str, Any]]) -> str:
    return yaml.safe_dump_all(
        documents,
        allow_unicode=False,
        explicit_start=True,
        sort_keys=False,
    )


def deployment_main(
    profile: DeploymentProfile, argv: list[str] | None = None
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"Verify or render the {profile.analysis_profile} production deployment."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, default=profile.default_manifest)
    verify.add_argument("--allow-template-image", action="store_true")
    render = subparsers.add_parser("render")
    render.add_argument("--manifest", type=Path, default=profile.default_manifest)
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
                profile=profile,
                allow_template_image=args.allow_template_image,
            )
            print(
                f"{profile.analysis_profile} deployment verified: {args.manifest}"
            )
            return 0
        rendered = render_documents(
            documents,
            profile=profile,
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
            print(f"{profile.analysis_profile} deployment rendered: {output}")
        return 0
    except DeploymentContractError as exc:
        print(str(exc), file=sys.stderr)
        return 1


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


def _validate_image(
    value: Any, *, profile: DeploymentProfile, allow_template: bool
) -> str:
    if not isinstance(value, str) or _DIGEST_IMAGE.fullmatch(value) is None:
        raise DeploymentContractError("production image must use repository@sha256:digest")
    if value == profile.template_image:
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
