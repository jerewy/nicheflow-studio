from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.paths import data_dir


SESSION_FILE_DIR = Path.home() / ".instagram_scraper"
_COOKIES_JSON_PATH = data_dir() / "browser-profiles" / "instagram-cookies.json"
_COOKIES_TXT_PATH = data_dir() / "browser-profiles" / "instagram-cookies.txt"

DEFAULT_PROFILE_NAME = "main"
_INSTAGRAM_PROFILES_ROOT = data_dir() / "browser-profiles" / "instagram"
_LEGACY_STORAGE_STATE_PATH = _INSTAGRAM_PROFILES_ROOT / "storage-state.json"


def profile_root() -> Path:
    return _INSTAGRAM_PROFILES_ROOT


def profile_dir(profile_name: str = DEFAULT_PROFILE_NAME) -> Path:
    return _INSTAGRAM_PROFILES_ROOT / profile_name


def profile_storage_state_path(profile_name: str = DEFAULT_PROFILE_NAME) -> Path:
    """Resolve the per-profile storage-state.json, migrating the legacy root path on first call."""
    new_path = profile_dir(profile_name) / "storage-state.json"
    if (
        profile_name == DEFAULT_PROFILE_NAME
        and not new_path.exists()
        and _LEGACY_STORAGE_STATE_PATH.exists()
    ):
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_STORAGE_STATE_PATH, new_path)
    return new_path


def list_profiles() -> list[str]:
    if not _INSTAGRAM_PROFILES_ROOT.exists():
        return []
    profiles = [
        child.name
        for child in _INSTAGRAM_PROFILES_ROOT.iterdir()
        if child.is_dir() and (child / "storage-state.json").exists()
    ]
    return sorted(profiles)


def load_playwright_cookies_from_storage_state(profile_name: str = DEFAULT_PROFILE_NAME) -> list[dict]:
    """Read cookies from a profile's storage-state.json in Playwright add_cookies() shape.

    Returns [] if the file is missing or unparseable, so callers can fail fast on the auth check.
    """
    path = profile_storage_state_path(profile_name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return []
    return [cookie for cookie in cookies if isinstance(cookie, dict) and cookie.get("name")]


@dataclass(frozen=True)
class InstagramCookieStatus:
    cookiefile: str | None
    has_sessionid: bool


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


def _json_cookies_to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        name = cookie.get("name", "")
        value = cookie.get("value", "")
        if not name:
            continue
        domain = cookie.get("domain") or ".instagram.com"
        if not domain.startswith("."):
            domain = f".{domain}"
        include_subdomains = "TRUE"
        path = cookie.get("path") or "/"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = int(cookie.get("expirationDate") or cookie.get("expires") or 0)
        lines.append(f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def get_instagram_yt_dlp_cookiefile() -> str | None:
    """Convert the saved Cookie-Editor JSON export to Netscape cookies.txt for yt-dlp.

    Returns the path to the cookies.txt file, or None if no cookie export exists.
    """
    source = _COOKIES_JSON_PATH
    if not source.exists():
        return None

    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return None
        netscape = _json_cookies_to_netscape(raw)
    except Exception:  # noqa: BLE001
        return None

    dest = _COOKIES_TXT_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(netscape, encoding="utf-8")
    return str(dest)


def _cookies_txt_has_sessionid(path: Path) -> bool:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] == "sessionid" and bool(parts[6].strip()):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def instagram_yt_dlp_cookie_status() -> InstagramCookieStatus:
    cookiefile = get_instagram_yt_dlp_cookiefile()
    if cookiefile is None:
        return InstagramCookieStatus(cookiefile=None, has_sessionid=False)
    path = Path(cookiefile)
    return InstagramCookieStatus(
        cookiefile=cookiefile,
        has_sessionid=_cookies_txt_has_sessionid(path),
    )


# ---------------------------------------------------------------------------
# Instaloader - Playwright session injection
# ---------------------------------------------------------------------------

_PLAYWRIGHT_STORAGE_STATE_PATH = (
    data_dir() / "browser-profiles" / "instagram" / "storage-state.json"
)


# Instagram web-app GraphQL identifier. Without this header the
# /graphql/query endpoint returns 403 Forbidden even with a valid
# sessionid cookie — the iPhone user-agent that ships with instaloader
# (4.15) is now blocked for graphql calls.
_INSTAGRAM_WEB_APP_ID = "936619743392459"
# A recent desktop Chrome UA. Aligns instaloader's HTTPS requests with
# what the browser session was issued for; mismatched UAs are a common
# graphql 403 trigger.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def load_playwright_cookies_into_instaloader(
    loader: object, profile_name: str = DEFAULT_PROFILE_NAME
) -> bool:
    """Inject Instagram cookies from the Playwright storage state into an instaloader session.

    This shares the same authenticated session that the browser scroll phase uses,
    which gives instaloader a higher rate limit than its own login path.

    Also overrides the requests.Session headers with browser-like headers so
    Instagram's /graphql/query endpoint accepts the request — see comments on
    ``_INSTAGRAM_WEB_APP_ID`` above for why.

    Returns True if a sessionid was successfully injected, False otherwise.
    """
    path = profile_storage_state_path(profile_name)
    if not path.exists():
        return False

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        if not isinstance(cookies, list):
            return False

        # loader.context._session is a requests.Session
        session = loader.context._session  # type: ignore[attr-defined]
        has_sessionid = False
        csrftoken_value: str | None = None

        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain") or ".instagram.com"
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(value, str):
                continue
            # requests cookiejar needs a plain domain string
            session.cookies.set(name, value, domain=domain)
            if name == "sessionid" and value:
                has_sessionid = True
            if name == "csrftoken" and value:
                csrftoken_value = value

        # Browser-like headers. instaloader's defaults look like an iPhone
        # app; the /graphql/query endpoint now 403s those even with a valid
        # sessionid. Setting the web App ID + Chrome UA + a csrftoken header
        # matches what the browser sends.
        session.headers.update(
            {
                "User-Agent": _BROWSER_USER_AGENT,
                "X-IG-App-ID": _INSTAGRAM_WEB_APP_ID,
                "X-ASBD-ID": "129477",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.instagram.com/",
                "Origin": "https://www.instagram.com",
            }
        )
        if csrftoken_value:
            session.headers["X-CSRFToken"] = csrftoken_value
        # instaloader also stores user_agent on the context; sync it so any
        # internal code path that re-builds headers picks up the browser UA.
        try:
            loader.context.user_agent = _BROWSER_USER_AGENT  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        return has_sessionid
    except Exception:  # noqa: BLE001
        return False
