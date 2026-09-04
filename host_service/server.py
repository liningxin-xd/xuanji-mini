from __future__ import annotations

import logging
import os

from .config import HostServiceSettings
from .telemetry import emit_service_event
from .tools import create_mcp


_LOGGER = logging.getLogger(__name__)


def main() -> None:
    level_name = os.environ.get("XUANJI_LOG_LEVEL", "INFO").upper()
    if level_name not in {"INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("XUANJI_LOG_LEVEL must be INFO, WARNING, ERROR, or CRITICAL")
    logging.basicConfig(level=getattr(logging, level_name), format="%(message)s")
    analysis_profile = None
    try:
        settings = HostServiceSettings.from_env()
        analysis_profile = settings.analysis_profile
        emit_service_event(
            _LOGGER,
            logging.INFO,
            "service_started",
            analysis_profile=analysis_profile,
        )
        create_mcp(settings).run(transport="streamable-http")
    except KeyboardInterrupt:
        emit_service_event(
            _LOGGER,
            logging.INFO,
            "service_stopped",
            analysis_profile=analysis_profile,
        )
    except Exception as exc:
        emit_service_event(
            _LOGGER,
            logging.CRITICAL,
            "service_failed",
            analysis_profile=analysis_profile,
            exc=exc,
        )
        raise
    else:
        emit_service_event(
            _LOGGER,
            logging.INFO,
            "service_stopped",
            analysis_profile=analysis_profile,
        )


if __name__ == "__main__":
    main()
