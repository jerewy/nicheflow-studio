from __future__ import annotations

import os
from pathlib import Path

from nicheflow_studio.core.paths import settings_env_path

# Variables that were in the process environment before any file was loaded.
# A real environment variable outranks both files, so the Settings screen needs
# to know which keys it cannot override.
_PREEXISTING_KEYS: frozenset[str] = frozenset()


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=value file into a dict. A missing file reads as empty."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    # utf-8-sig, not utf-8: Notepad and PowerShell's Out-File write a UTF-8 BOM,
    # and plain "utf-8" keeps it as a character on the first line — so the FIRST
    # key in the file silently becomes "﻿GROQ_API_KEY" and every lookup of
    # "GROQ_API_KEY" misses. Decoding as utf-8-sig drops the BOM if present and
    # is a no-op otherwise.
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            continue

        normalized_value = value.strip()
        is_quoted = (
            len(normalized_value) >= 2
            and normalized_value[0] == normalized_value[-1]
            and normalized_value[0] in {"'", '"'}
        )
        if is_quoted:
            normalized_value = normalized_value[1:-1]
        values[normalized_key] = normalized_value
    return values


def load_dotenv(dotenv_path: Path | None = None) -> None:
    target_path = dotenv_path or Path.cwd() / ".env"
    for key, value in read_env_file(target_path).items():
        if key not in os.environ:
            os.environ[key] = value


def load_app_env() -> None:
    """Load integration keys for the app, highest precedence first.

    1. Real environment variables (never overwritten).
    2. The per-user settings file the Settings screen writes. A packaged build
       has no repo .env beside it, so this is where its keys live.
    3. The repo ``.env`` in the working directory (source checkouts).
    """
    global _PREEXISTING_KEYS
    _PREEXISTING_KEYS = frozenset(os.environ)

    repo_env = Path.cwd() / ".env"
    # NICHEFLOW_DATA_DIR decides where the settings file lives, so it has to be
    # known before that file is read. Let the repo .env supply it.
    if "NICHEFLOW_DATA_DIR" not in os.environ:
        override = read_env_file(repo_env).get("NICHEFLOW_DATA_DIR")
        if override:
            os.environ["NICHEFLOW_DATA_DIR"] = override

    load_dotenv(settings_env_path())
    load_dotenv(repo_env)


def preexisting_env_keys() -> frozenset[str]:
    """Keys set in the real environment when :func:`load_app_env` ran."""
    return _PREEXISTING_KEYS
