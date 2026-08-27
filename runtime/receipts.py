from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


class ReceiptVerificationError(ValueError):
    pass


class TrustedReceiptVerifier:
    """Verifies receipts signed by a Host-owned adapter secret.

    The secret must stay in the Host integration process. The task-ticket CLI does
    not load it from environment variables or repository files.
    """

    def __init__(self, *, key_id: str, secret: bytes):
        if not isinstance(key_id, str) or not key_id.strip():
            raise ReceiptVerificationError("trusted receipt key_id is required")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ReceiptVerificationError(
                "trusted receipt secret must contain at least 32 bytes"
            )
        self.key_id = key_id
        self._secret = secret

    def sign(self, run_id: str, event: dict[str, Any]) -> str:
        return hmac.new(
            self._secret,
            self._payload(run_id, event),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, run_id: str, event: dict[str, Any]) -> None:
        if event.get("receipt_key_id") != self.key_id:
            raise ReceiptVerificationError(
                "trusted receipt key_id does not match the run authority"
            )
        signature = event.get("receipt_signature")
        if not isinstance(signature, str) or len(signature) != 64:
            raise ReceiptVerificationError(
                "trusted receipt signature must be a sha256 HMAC"
            )
        if not hmac.compare_digest(signature, self.sign(run_id, event)):
            raise ReceiptVerificationError("trusted receipt signature is invalid")

    def _payload(self, run_id: str, event: dict[str, Any]) -> bytes:
        signed = {
            "run_id": run_id,
            "event": event.get("event"),
            "step_id": event.get("step_id"),
            "attempt_no": event.get("attempt_no"),
            "receipt_type": event.get("receipt_type"),
            "receipt_key_id": event.get("receipt_key_id"),
            "receipt_id": event.get("receipt_id"),
            "submitted_sql_sha256": event.get("submitted_sql_sha256"),
            "query_id": event.get("query_id"),
            "raw_result_sha256": event.get("raw_result_sha256"),
            "error_class": event.get("error_class"),
            "error_code": event.get("error_code"),
            "error_message": event.get("error_message"),
        }
        return json.dumps(
            signed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
