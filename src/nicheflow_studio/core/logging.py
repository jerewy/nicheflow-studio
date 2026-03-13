from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from nicheflow_studio.core.paths import logs_dir


def configure_logging() -> None:
    log_path = logs_dir() / "app.log"

    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    root.addHandler(handler)

