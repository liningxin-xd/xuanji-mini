from __future__ import annotations

from pathlib import Path
from typing import Any

if __package__:
    from .primary_deployment_contract import (
        DeploymentContractError,
        DeploymentProfile,
        deployment_main,
        dump_documents,
        load_documents,
        render_documents as _render_documents,
        validate_documents as _validate_documents,
    )
else:
    from primary_deployment_contract import (
        DeploymentContractError,
        DeploymentProfile,
        deployment_main,
        dump_documents,
        load_documents,
        render_documents as _render_documents,
        validate_documents as _validate_documents,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "deploy" / "primary-v2" / "manifests.yaml"
TEMPLATE_IMAGE = "xuanji-primary-v2.invalid/repository@sha256:" + "0" * 64
_PROFILE = DeploymentProfile(
    analysis_profile="primary_v2",
    resource_prefix="xuanji-primary-v2",
    default_manifest=DEFAULT_MANIFEST,
    template_image=TEMPLATE_IMAGE,
    ingress_client_selector=(("xuanji.taptap/client-profile", "primary-v2"),),
)


def render_documents(
    documents: list[dict[str, Any]],
    *,
    image: str,
    host_public_url: str,
    dview_mcp_url: str,
) -> list[dict[str, Any]]:
    return _render_documents(
        documents,
        profile=_PROFILE,
        image=image,
        host_public_url=host_public_url,
        dview_mcp_url=dview_mcp_url,
    )


def validate_documents(
    documents: list[dict[str, Any]], *, allow_template_image: bool
) -> None:
    _validate_documents(
        documents,
        profile=_PROFILE,
        allow_template_image=allow_template_image,
    )


def main(argv: list[str] | None = None) -> int:
    return deployment_main(_PROFILE, argv)


if __name__ == "__main__":
    raise SystemExit(main())
