from __future__ import annotations

from pathlib import Path


SESSION_FILE_DIR = Path.home() / ".instagram_scraper"


def load_latest_instagram_session(loader: object) -> str | None:
    if not SESSION_FILE_DIR.exists():
        return None

    sessions = [path for path in SESSION_FILE_DIR.glob("*.session") if path.is_file()]
    if not sessions:
        return None

    session_path = max(sessions, key=lambda path: path.stat().st_mtime)
    username = session_path.stem
    try:
        loader.load_session_from_file(username, str(session_path))
    except Exception:  # noqa: BLE001
        return None
    return username
