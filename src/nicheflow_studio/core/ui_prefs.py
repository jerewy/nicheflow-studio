"""Tiny JSON-backed store for desktop UI preferences.

This holds small, non-critical interface toggles that should survive an app
restart (e.g. whether "Auto-publish due reels" is on). It is deliberately
separate from the SQLite DB: these are per-machine UI choices, not domain data,
and a corrupt/missing file should never block startup — reads fall back to
defaults silently.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from nicheflow_studio.core.paths import data_dir


def _prefs_path() -> Path:
    return data_dir() / "ui_prefs.json"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_ui_prefs() -> dict[str, Any]:
    """Return all stored preferences, or an empty dict if none/unreadable."""
    path = _prefs_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def get_ui_pref(key: str, default: Any = None) -> Any:
    """Return a single stored preference, falling back to ``default``."""
    return load_ui_prefs().get(key, default)


def set_ui_pref(key: str, value: Any) -> None:
    """Persist a single preference, leaving other keys untouched."""
    prefs = load_ui_prefs()
    prefs[key] = value
    _atomic_write_text(_prefs_path(), json.dumps(prefs, indent=2))
