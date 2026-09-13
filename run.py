"""Start the chat server using deployment settings from config.json."""
from pathlib import Path

import uvicorn

from app import AppConfig


def main() -> None:
    config = AppConfig.load(Path("config.json"))
    uvicorn.run("app:app", host=config.host, port=config.port)


if __name__ == "__main__":
    main()
