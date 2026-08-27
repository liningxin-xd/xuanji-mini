from __future__ import annotations

import base64
import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from host_service.pipeline_handoff import (
    PipelineHandoffError,
    PipelineHandoffSigner,
)
from runtime.contracts import canonical_sha256
from scripts.derive_pipeline_public_key import main as derive_public_key


def _artifact() -> dict:
    analysis = {
        "source": "dataworks_dqc",
        "project": "tap_dw",
        "table": "tap_dw.monitor",
        "partition": "dt=2026-08-26",
        "overall_status": "completed",
        "investigations": [
            {
                "status": "completed",
                "rule_indexes": [0],
                "summary": "validated",
                "queries": [{"query_id": "private-query"}],
                "attribution_execution": {
                    "steps": [
                        {
                            "step": "game_id",
                            "query_id": "private-step-query",
                            "candidate_count": 1,
                        }
                    ]
                },
            }
        ],
    }
    receipt = {
        "status": "valid",
        "task_id": "task-1",
        "payload_sha256": "a" * 64,
        "definition_bundle_sha256": "b" * 64,
        "overall_status": "completed",
        "investigation_count": 1,
        "successful_investigation_count": 1,
        "rule_indexes_sha256": "c" * 64,
        "analysis_sha256": canonical_sha256(analysis),
        "root_snapshot_sha256s": ["d" * 64],
        "investigation_receipts": [],
    }
    receipt["validation_receipt_sha256"] = canonical_sha256(receipt)
    return {
        "task_id": "task-1",
        "analysis": analysis,
        "validation_receipt": receipt,
    }


class PipelineHandoffSignerTest(unittest.TestCase):
    def setUp(self):
        self.signer = PipelineHandoffSigner(
            receipt_key_id="receipt-v1",
            receipt_secret=b"r" * 32,
        )

    def test_builds_verifiable_public_projection(self):
        preview, handoff = self.signer.build(
            task_id="task-1",
            artifact=_artifact(),
        )
        encoded = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn("query_id", encoded)
        self.assertNotIn("private-query", encoded)
        self.assertEqual(canonical_sha256(preview), handoff["analysis_preview_sha256"])
        self.assertEqual("a" * 64, handoff["payload_sha256"])

        signature = _decode_base64url(handoff["signature"])
        unsigned = dict(handoff)
        unsigned.pop("signature")
        public_key = Ed25519PublicKey.from_public_bytes(
            _decode_base64url(self.signer.public_key_base64url)
        )
        public_key.verify(signature, _canonical_json(unsigned))

        repeated_preview, repeated_handoff = self.signer.build(
            task_id="task-1",
            artifact=_artifact(),
        )
        self.assertEqual(preview, repeated_preview)
        self.assertEqual(handoff, repeated_handoff)

    def test_rejects_analysis_or_receipt_tampering(self):
        changed_analysis = _artifact()
        changed_analysis["analysis"]["overall_status"] = "failed"
        with self.assertRaisesRegex(PipelineHandoffError, "analysis"):
            self.signer.build(task_id="task-1", artifact=changed_analysis)

        changed_receipt = _artifact()
        changed_receipt["validation_receipt"]["payload_sha256"] = "e" * 64
        with self.assertRaisesRegex(PipelineHandoffError, "receipt hash"):
            self.signer.build(task_id="task-1", artifact=changed_receipt)

    def test_operator_command_outputs_only_the_public_trust_anchor(self):
        stdout = StringIO()
        with patch.dict(
            os.environ,
            {
                "XUANJI_RECEIPT_KEY_ID": "receipt-v1",
                "XUANJI_RECEIPT_SECRET": "r" * 32,
            },
            clear=True,
        ), redirect_stdout(stdout):
            self.assertEqual(0, derive_public_key())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(self.signer.signing_key_id, payload["signing_key_id"])
        self.assertEqual(
            self.signer.public_key_base64url,
            payload["public_key_base64url"],
        )
        self.assertNotIn("r" * 32, stdout.getvalue())


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
