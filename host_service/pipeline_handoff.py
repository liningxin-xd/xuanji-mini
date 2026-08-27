from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from runtime.contracts import canonical_sha256
from runtime.public_projection import public_analysis_projection


PIPELINE_HANDOFF_SCHEMA_VERSION = 1
PIPELINE_HANDOFF_PROVIDER = "xuanji-mini"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HKDF_SALT = b"xuanji-pipeline-handoff-v1"
_HKDF_INFO = b"ed25519-signing-key"


class PipelineHandoffError(ValueError):
    pass


class PipelineHandoffSigner:
    """Build a signed public projection from one verified private task artifact."""

    def __init__(self, *, receipt_key_id: str, receipt_secret: bytes):
        if not isinstance(receipt_key_id, str) or not receipt_key_id.strip():
            raise PipelineHandoffError("receipt key ID is required")
        if not isinstance(receipt_secret, bytes) or len(receipt_secret) < 32:
            raise PipelineHandoffError(
                "receipt secret must contain at least 32 bytes"
            )
        seed = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=_HKDF_INFO,
        ).derive(receipt_secret)
        self._private_key = Ed25519PrivateKey.from_private_bytes(seed)
        self.signing_key_id = (
            f"{receipt_key_id}.pipeline-handoff-ed25519-v1"
        )

    @property
    def public_key_base64url(self) -> str:
        encoded = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _base64url(encoded)

    def build(
        self,
        *,
        task_id: str,
        artifact: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        analysis, receipt = _verified_task_artifact(task_id, artifact)
        preview = public_analysis_projection(analysis)
        unsigned = {
            "schema_version": PIPELINE_HANDOFF_SCHEMA_VERSION,
            "provider": PIPELINE_HANDOFF_PROVIDER,
            "task_id": task_id,
            "payload_sha256": receipt["payload_sha256"],
            "analysis_preview_sha256": canonical_sha256(preview),
            "validation_receipt_sha256": receipt[
                "validation_receipt_sha256"
            ],
            "signing_key_id": self.signing_key_id,
        }
        signature = self._private_key.sign(_canonical_json(unsigned))
        return preview, {**unsigned, "signature": _base64url(signature)}


def _verified_task_artifact(
    task_id: str,
    artifact: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(artifact, dict) or set(artifact) != {
        "task_id",
        "analysis",
        "validation_receipt",
    }:
        raise PipelineHandoffError("task sink artifact has invalid fields")
    if artifact.get("task_id") != task_id:
        raise PipelineHandoffError("task sink artifact changed task identity")
    analysis = artifact.get("analysis")
    receipt = artifact.get("validation_receipt")
    if not isinstance(analysis, dict) or not isinstance(receipt, dict):
        raise PipelineHandoffError("task sink artifact is incomplete")
    if receipt.get("status") != "valid" or receipt.get("task_id") != task_id:
        raise PipelineHandoffError("task validation receipt is not valid")
    payload_sha256 = receipt.get("payload_sha256")
    analysis_sha256 = receipt.get("analysis_sha256")
    receipt_sha256 = receipt.get("validation_receipt_sha256")
    if any(
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
        for value in (payload_sha256, analysis_sha256, receipt_sha256)
    ):
        raise PipelineHandoffError("task validation receipt hashes are invalid")
    if analysis_sha256 != canonical_sha256(analysis):
        raise PipelineHandoffError("task analysis no longer matches its receipt")
    unsigned_receipt = deepcopy(receipt)
    unsigned_receipt.pop("validation_receipt_sha256", None)
    if receipt_sha256 != canonical_sha256(unsigned_receipt):
        raise PipelineHandoffError("task validation receipt hash is invalid")
    investigations = analysis.get("investigations")
    if (
        receipt.get("overall_status") != analysis.get("overall_status")
        or not isinstance(investigations, list)
        or receipt.get("investigation_count") != len(investigations)
    ):
        raise PipelineHandoffError("task receipt does not match the analysis summary")
    return deepcopy(analysis), deepcopy(receipt)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
