from __future__ import annotations

from .config import HostServiceSettings
from .tools import create_mcp


def main() -> None:
    settings = HostServiceSettings.from_env()
    try:
        create_mcp(settings).run(transport="streamable-http")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
