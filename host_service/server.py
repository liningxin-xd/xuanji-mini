from __future__ import annotations

import logging
import os

from .config import HostServiceSettings
from .tools import create_mcp


def main() -> None:
    level_name = os.environ.get("XUANJI_LOG_LEVEL", "INFO").upper()
    if level_name not in {"INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("XUANJI_LOG_LEVEL must be INFO, WARNING, ERROR, or CRITICAL")
    logging.basicConfig(level=getattr(logging, level_name), format="%(message)s")
    settings = HostServiceSettings.from_env()
    try:
        create_mcp(settings).run(transport="streamable-http")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
