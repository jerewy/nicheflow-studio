"""Show which Instagram account each saved profile is logged in as.

Usage:
    python scripts/instagram_whoami.py              # all profiles
    python scripts/instagram_whoami.py --profile main

Confirms that profiles like 'main', 'alt1', 'alt2' are actually mapped to
DIFFERENT accounts and that none of them silently fell back to a stale or
shared session.

Exit codes:
    0 - every profile checked returned a username
    1 - at least one profile failed to identify (cookies missing/dead)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nicheflow_studio.core.instagram_session import (
    list_profiles,
    load_playwright_cookies_into_instaloader,
    profile_storage_state_path,
)


def _identify(profile_name: str) -> tuple[str | None, str | None]:
    """Return (username, error). Username is None if not identifiable."""
    storage = profile_storage_state_path(profile_name)
    if not storage.exists():
        return None, f"no storage-state.json at {storage}"
    try:
        import instaloader
        from instaloader.exceptions import ConnectionException
    except ImportError:
        return None, "instaloader not installed (pip install -r requirements.txt)"
    try:
        loader = instaloader.Instaloader(
            quiet=True,
            request_timeout=15.0,
            max_connection_attempts=1,
        )
        cookies_ok = load_playwright_cookies_into_instaloader(loader, profile_name)
        if not cookies_ok:
            return None, "no sessionid cookie in storage-state.json"
        username = loader.test_login()
        if not username:
            return None, "Instagram did not return a username (session likely dead)"
        return username, None
    except ConnectionException as exc:
        message = str(exc)
        lowered = message.lower()
        if (
            "401 unauthorized" in lowered
            or "please wait a few minutes" in lowered
            or "graphql/query" in lowered
        ):
            return (
                None,
                "Instagram throttled this session. Stop Instagram automation and wait before retrying.",
            )
        return None, f"{type(exc).__name__}: {message}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show which Instagram account each saved profile is logged in as."
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Check only this profile name (default: all profiles found on disk).",
    )
    args = parser.parse_args()

    if args.profile is not None:
        names = [args.profile]
    else:
        names = list_profiles()
        if not names:
            print("No profiles found on disk.", file=sys.stderr)
            print(
                "Run: python scripts/instagram_login_playwright.py --profile <name>",
                file=sys.stderr,
            )
            return 1

    name_width = max(len(n) for n in names)
    any_failed = False
    identified: list[tuple[str, str | None]] = []
    for name in names:
        username, error = _identify(name)
        identified.append((name, username))
        if username:
            print(f"{name:<{name_width}}  @{username}")
        else:
            any_failed = True
            print(f"{name:<{name_width}}  FAILED  ({error})")

    # Warn if two profiles map to the same account — usually means cookies
    # got copied or the wrong account was used during login.
    accounts = [u for _, u in identified if u]
    duplicates = {u for u in accounts if accounts.count(u) > 1}
    if duplicates:
        print(file=sys.stderr)
        print(
            f"WARNING: multiple profiles share the same account: {sorted(duplicates)}",
            file=sys.stderr,
        )
        print(
            "Re-run instagram_login_playwright.py for the affected profile(s) "
            "and log into the intended distinct account.",
            file=sys.stderr,
        )

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
