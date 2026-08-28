from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class ErrorCodeDictionaryError(ValueError):
    pass


class ErrorCodeDictionary:
    def __init__(self, root: Path):
        self.path = root / "references/download-install-error-code-dictionary.md"

    def annotate_frozen_codes(
        self, codes: tuple[str, ...]
    ) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
        if not codes:
            return {}, ()
        if any(re.fullmatch(r"[0-9]{4}", code) is None for code in codes):
            raise ErrorCodeDictionaryError("frozen download error code is invalid")
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ErrorCodeDictionaryError(
                "download error-code dictionary is unavailable"
            ) from exc
        required_markers = (
            "下载命名空间：TapFileDownload `TapDownException`",
            "客户端版本生效区间：未登记",
            "事件版本无法与该快照建立适用关系",
        )
        if any(marker not in text for marker in required_markers):
            raise ErrorCodeDictionaryError(
                "download error-code dictionary applicability changed"
            )
        return (
            {
                code: {
                    "code": code,
                    "meaning_status": "unconfirmed_version",
                }
                for code in codes
            },
            ("dictionary_version_unconfirmed",),
        )
