from __future__ import annotations

from copy import deepcopy
from typing import Any


_PRIVATE_FIELDS = {
    "query_id",
    "receipt_id",
    "raw_result",
    "raw_result_sha256",
    "receipt_signature",
    "rendered_sql",
    "rendered_sql_sha256",
    "submitted_sql_sha256",
    "private_queries",
    "root_snapshot_sha256",
    "root_snapshot_sha256s",
    "root_snapshot_reused",
    "root_query_count",
}


def public_analysis_projection(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: public_analysis_projection(child)
            for key, child in value.items()
            if key not in _PRIVATE_FIELDS and not key.endswith("_sha256")
        }
    if isinstance(value, list):
        return [public_analysis_projection(child) for child in value]
    return deepcopy(value)
