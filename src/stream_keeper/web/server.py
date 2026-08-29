from __future__ import annotations

import logging

import uvicorn

from ..settings import Settings, load_environment_file
from .app import create_app


def main() -> int:
    load_environment_file()
    settings = Settings.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    uvicorn.run(
        create_app(settings),
        host=settings.web_host,
        port=settings.web_port,
        workers=1,
        proxy_headers=True,
        access_log=False,
    )
    return 0
