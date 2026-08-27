from __future__ import annotations

import json
import os
import sys

from host_service.pipeline_handoff import PipelineHandoffSigner


def main() -> int:
    receipt_key_id = os.environ.get("XUANJI_RECEIPT_KEY_ID", "").strip()
    receipt_secret = os.environ.get("XUANJI_RECEIPT_SECRET", "").encode("utf-8")
    signer = PipelineHandoffSigner(
        receipt_key_id=receipt_key_id,
        receipt_secret=receipt_secret,
    )
    print(
        json.dumps(
            {
                "signing_key_id": signer.signing_key_id,
                "public_key_base64url": signer.public_key_base64url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"pipeline public key derivation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
