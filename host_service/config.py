from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class HostConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class HostServiceSettings:
    public_url: str
    bind_host: str
    port: int
    host_bearer_token: str = field(repr=False)
    dview_mcp_url: str
    dview_bearer_token: str = field(repr=False)
    dview_read_timeout_seconds: int
    receipt_key_id: str
    receipt_secret: bytes = field(repr=False)
    runs_root: Path
    results_root: Path

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> HostServiceSettings:
        values = os.environ if env is None else env
        public_url = _required_url(values, "XUANJI_HOST_PUBLIC_URL")
        dview_mcp_url = _required_url(values, "XUANJI_DVIEW_MCP_URL")
        host_token = _required_secret(values, "XUANJI_HOST_BEARER_TOKEN", 32)
        dview_token = _required_secret(values, "XUANJI_DVIEW_BEARER_TOKEN", 16)
        receipt_secret = _required_secret(
            values,
            "XUANJI_RECEIPT_SECRET",
            32,
        ).encode("utf-8")
        receipt_key_id = _required_text(values, "XUANJI_RECEIPT_KEY_ID")
        bind_host = values.get("XUANJI_HOST", "127.0.0.1").strip()
        if not bind_host:
            raise HostConfigurationError("XUANJI_HOST must not be empty")
        port = _bounded_int(values, "XUANJI_PORT", 8091, 1024, 65535)
        read_timeout = _bounded_int(
            values,
            "XUANJI_DVIEW_READ_TIMEOUT_SECONDS",
            660,
            1,
            3600,
        )
        runs_root = _absolute_path(
            values.get("XUANJI_RUNS_ROOT", "/var/lib/xuanji/runs"),
            "XUANJI_RUNS_ROOT",
        )
        results_root = _absolute_path(
            values.get("XUANJI_RESULTS_ROOT", "/var/lib/xuanji/results"),
            "XUANJI_RESULTS_ROOT",
        )
        return cls(
            public_url=public_url,
            bind_host=bind_host,
            port=port,
            host_bearer_token=host_token,
            dview_mcp_url=dview_mcp_url,
            dview_bearer_token=dview_token,
            dview_read_timeout_seconds=read_timeout,
            receipt_key_id=receipt_key_id,
            receipt_secret=receipt_secret,
            runs_root=runs_root,
            results_root=results_root,
        )


def _required_text(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise HostConfigurationError(f"{name} is required")
    return value


def _required_secret(
    values: Mapping[str, str],
    name: str,
    minimum_bytes: int,
) -> str:
    value = _required_text(values, name)
    if len(value.encode("utf-8")) < minimum_bytes:
        raise HostConfigurationError(
            f"{name} must contain at least {minimum_bytes} bytes"
        )
    return value


def _required_url(values: Mapping[str, str], name: str) -> str:
    value = _required_text(values, name).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HostConfigurationError(f"{name} must be an absolute HTTP(S) URL")
    return value


def _bounded_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise HostConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise HostConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _absolute_path(value: str, name: str) -> Path:
    path = Path(value.strip())
    if not path.is_absolute():
        raise HostConfigurationError(f"{name} must be an absolute path")
    return path
