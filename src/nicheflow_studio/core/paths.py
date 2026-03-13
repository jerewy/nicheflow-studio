from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """
    MVP default is a repo-local `./data` folder.

    In production packaging, we should move this to a user directory (AppData),
    but keeping it repo-local makes iteration simple and transparent.
    """
    override = os.environ.get("NICHEFLOW_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd() / "data"


def downloads_dir() -> Path:
    return data_dir() / "downloads"


def logs_dir() -> Path:
    return data_dir() / "logs"


def ensure_data_dirs() -> None:
    downloads_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)

