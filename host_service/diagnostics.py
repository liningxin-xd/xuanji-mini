from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import re
import traceback
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .telemetry import OperationTrace, exception_diagnostic, exception_type_name


_AUTHORIZATION_VALUE = re.compile(
    r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?ix)([\"']?"
    r"[A-Za-z0-9_.-]*(?:token|secret|password|passwd|authorization|"
    r"api[_-]?key|private[_-]?key|signing[_-]?key|encryption[_-]?key|"
    r"access[_-]?key)[A-Za-z0-9_.-]*"
    r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)


class FailureBundleWriter:
    def __init__(
        self,
        *,
        diagnostics_root: Path,
        tasks_root: Path,
        runs_root: Path,
    ):
        self._diagnostics_root = diagnostics_root
        self._tasks_root = tasks_root
        self._runs_root = runs_root

    def write(
        self,
        *,
        trace: OperationTrace,
        exc: BaseException,
        result: Any = None,
    ) -> Path:
        if trace.error_id is None:
            raise ValueError("failure bundle requires an error_id")
        self._diagnostics_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._diagnostics_root, 0o700)
        target = self._diagnostics_root / f"{trace.error_id}.json"
        if target.exists():
            raise FileExistsError("failure bundle already exists")

        task_root = self._tasks_root / trace.task_id
        run_root = (
            self._runs_root / trace.last_query_run_id
            if trace.last_query_run_id is not None
            else None
        )
        payload = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "error_id": trace.error_id,
            "operation_id": trace.operation_id,
            "operation": trace.fields(),
            "tool_input": _json_value(trace.private_tool_input),
            "query": {
                "run_id": trace.last_query_run_id,
                "bucket": trace.last_query_bucket,
                "ordinal": trace.last_query_ordinal,
                "stage": trace.last_query_stage,
                "step": trace.last_query_step,
                "attempt": trace.last_query_attempt,
                "request": _json_value(trace.private_last_query_request),
                "transport_response_received": (
                    trace.private_last_query_transport_response_received
                ),
                "transport_response": _json_value(
                    trace.private_last_query_transport_response
                ),
                "response_received": trace.private_last_query_response_received,
                "raw_response": _json_value(trace.private_last_query_response),
            },
            "exception": {
                "captured_type": trace.exception_type,
                "outer_type": exception_type_name(exc),
                "diagnostic": exception_diagnostic(exc),
                "tree": _exception_tree(exc),
                "traceback": _redact_text(
                    "".join(
                        traceback.TracebackException.from_exception(
                            exc,
                            capture_locals=False,
                        ).format(chain=True)
                    )
                ),
            },
            "result": _json_value(result),
            "task_artifacts": _collect_tree(task_root),
            "run_artifacts": (
                _collect_tree(run_root) if run_root is not None else None
            ),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target


def _collect_tree(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"status": "missing", "files": {}}
    files: dict[str, Any] = {}
    try:
        paths = sorted(root.rglob("*"))
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "traversal_failed",
            "files": files,
            "exception": _exception_tree(exc),
        }
    for path in paths:
        relative = str(path.relative_to(root))
        if path.is_symlink():
            files[relative] = {"kind": "symlink", "content": "[NOT_FOLLOWED]"}
        elif path.is_file():
            try:
                files[relative] = _read_file(path)
            except Exception as exc:  # noqa: BLE001
                files[relative] = {
                    "status": "read_failed",
                    "exception": _exception_tree(exc),
                }
    return {"status": "collected", "files": files}


def _read_file(path: Path) -> dict[str, Any]:
    stat = path.stat()
    raw = path.read_bytes()
    metadata = {
        "mode": oct(stat.st_mode & 0o777),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            **metadata,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
        }
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        content: Any = _redact_text(text)
        encoding = "text"
    else:
        content = _json_value(parsed)
        encoding = "json"
    return {**metadata, "encoding": encoding, "content": content}


def _json_value(value: Any, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        try:
            content = _redact_text(value.decode("utf-8"))
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(value).decode("ascii")
            encoding = "base64"
        return {
            "type": "bytes",
            "size_bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
            "encoding": encoding,
            "content": content,
        }
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)

    tracked = seen if seen is not None else set()
    identity = id(value)
    if identity in tracked:
        return "[CYCLE]"
    tracked.add(identity)
    try:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                name = str(key)
                result[name] = (
                    "[REDACTED]"
                    if _credential_key(name)
                    else _json_value(item, tracked)
                )
            return result
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [_json_value(item, tracked) for item in value]
        if isinstance(value, (set, frozenset)):
            return [_json_value(item, tracked) for item in sorted(value, key=repr)]
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                item.name: (
                    "[REDACTED]"
                    if _credential_key(item.name)
                    else _json_value(getattr(value, item.name), tracked)
                )
                for item in dataclasses.fields(value)
            }
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _json_value(model_dump(mode="python"), tracked)
        return {
            "type": type(value).__name__,
            "repr": _redact_text(repr(value)),
        }
    finally:
        tracked.remove(identity)


def _credential_key(key: str) -> bool:
    with_word_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(
        r"[^a-z0-9]+", "_", with_word_boundaries.lower()
    ).strip("_")
    if normalized == "key_id" or normalized.endswith("receipt_key_id"):
        return False
    parts = set(normalized.split("_"))
    if parts.intersection(
        {
            "token",
            "secret",
            "password",
            "passwd",
            "authorization",
            "credential",
            "credentials",
        }
    ):
        return True
    key_names = {
        "key",
        "api_key",
        "private_key",
        "public_key",
        "signing_key",
        "encryption_key",
        "access_key",
        "secret_key",
        "access_key_id",
    }
    return normalized in key_names or normalized.endswith(
        (
            "_api_key",
            "_private_key",
            "_public_key",
            "_signing_key",
            "_encryption_key",
            "_access_key",
            "_secret_key",
            "_access_key_id",
        )
    )


def _exception_tree(
    exc: BaseException,
    seen: set[int] | None = None,
) -> dict[str, Any]:
    tracked = seen if seen is not None else set()
    identity = id(exc)
    if identity in tracked:
        return {"type": exception_type_name(exc), "cycle": True}
    tracked.add(identity)
    try:
        attributes = {
            name: (
                "[REDACTED]" if _credential_key(name) else _json_value(value)
            )
            for name, value in vars(exc).items()
        }
        children = getattr(exc, "exceptions", None)
        cause = exc.__cause__
        context = None if exc.__suppress_context__ else exc.__context__
        return {
            "type": exception_type_name(exc),
            "message": _redact_text(str(exc)),
            "args": _json_value(exc.args),
            "attributes": attributes,
            "notes": _json_value(getattr(exc, "__notes__", None)),
            "children": (
                [_exception_tree(child, tracked) for child in children]
                if isinstance(children, tuple)
                else []
            ),
            "cause": (
                _exception_tree(cause, tracked)
                if isinstance(cause, BaseException)
                else None
            ),
            "context": (
                _exception_tree(context, tracked)
                if isinstance(context, BaseException) and context is not cause
                else None
            ),
        }
    finally:
        tracked.remove(identity)


def _redact_text(value: str) -> str:
    value = _PRIVATE_KEY_BLOCK.sub("[REDACTED_PRIVATE_KEY]", value)
    value = _AUTHORIZATION_VALUE.sub(
        lambda match: f"{match.group(1)} [REDACTED]",
        value,
    )
    return _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        value,
    )
