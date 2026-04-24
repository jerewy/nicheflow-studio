from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "NicheFlow Studio"


def _is_packaged_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def _packaged_windows_data_dir() -> Path:
    base_dir = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base_dir:
        return Path(base_dir).expanduser().resolve() / APP_DIR_NAME / "data"
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME / "data"


def data_dir() -> Path:
    """
    Source/dev runs use a repo-local `./data` folder for transparency.
    Packaged Windows builds default to a per-user AppData location.
    """
    override = os.environ.get("NICHEFLOW_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and _is_packaged_runtime():
        return _packaged_windows_data_dir()
    return Path.cwd() / "data"


def downloads_dir() -> Path:
    return data_dir() / "downloads"


def processed_dir() -> Path:
    return data_dir() / "processed"


def backups_dir() -> Path:
    return data_dir() / "backups"


def logs_dir() -> Path:
    return data_dir() / "logs"


def ensure_data_dirs() -> None:
    downloads_dir().mkdir(parents=True, exist_ok=True)
    processed_dir().mkdir(parents=True, exist_ok=True)
    backups_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
