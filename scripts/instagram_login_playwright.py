from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from playwright.async_api import async_playwright

from nicheflow_studio.core.instagram_session import (
    DEFAULT_PROFILE_NAME,
    load_playwright_cookies_into_instaloader,
    profile_dir as session_profile_dir,
)
from nicheflow_studio.core.instagram_profile_pool import ProfilePool


STORAGE_STATE_FILE = "storage-state.json"
INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"


async def _has_instagram_session(context) -> bool:  # noqa: ANN001
    cookies = await context.cookies("https://www.instagram.com")
    cookie_names = {cookie.get("name") for cookie in cookies}
    return "sessionid" in cookie_names


def _record_login(profile_name: str | None) -> None:
    if not profile_name:
        print(
            "note: skipping login-date record (script invoked with --profile-dir, "
            "no manifest profile name to attach to)",
            file=sys.stderr,
        )
        return
    try:
        pool = ProfilePool.load()
        updated = pool.mark_login(profile_name)
    except Exception:  # noqa: BLE001
        # Surface the full traceback — silently swallowing this is what
        # caused the "no recorded login date" warning to keep appearing
        # after apparently-successful login runs.
        import traceback
        print(
            f"ERROR: failed to record login for profile '{profile_name}' "
            f"in pool manifest:",
            file=sys.stderr,
        )
        traceback.print_exc()
        return
    print(
        f"recorded login for profile '{profile_name}' at "
        f"{updated.last_login.isoformat() if updated.last_login else '?'} "
        f"(manifest: {pool.manifest_path})",
        file=sys.stderr,
    )
    username = _identify_logged_in_user(profile_name)
    if username:
        print(f"profile '{profile_name}' is logged in as: @{username}", file=sys.stderr)
    else:
        print(
            f"WARNING: could not confirm which account profile '{profile_name}' "
            f"is logged into — the session cookie is present but Instagram did "
            f"not return a username. Try opening instagram.com in the saved "
            f"browser profile to check, or re-run the login script.",
            file=sys.stderr,
        )


def _identify_logged_in_user(profile_name: str) -> str | None:
    """Reach Instagram with the saved cookies and ask 'who am I?'.

    Returns the username string when Instagram recognises the session,
    else ``None``. Uses ``instaloader.test_login()`` so it shares the same
    auth path the scraper uses — if this returns a name, scraping for
    that profile will run as that account.
    """
    try:
        import instaloader
    except Exception:  # noqa: BLE001
        return None
    try:
        loader = instaloader.Instaloader(quiet=True, request_timeout=15.0)
        load_playwright_cookies_into_instaloader(loader, profile_name)
        username = loader.test_login()
        return username or None
    except Exception:  # noqa: BLE001
        return None


async def login_instagram(
    *, profile_dir: Path, wait_seconds: int, check_url: str, profile_name: str | None = None
) -> int:
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_state_path = profile_dir / STORAGE_STATE_FILE
    deadline = time.monotonic() + wait_seconds

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir.resolve()),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            if await _has_instagram_session(context):
                await page.goto(check_url, wait_until="domcontentloaded", timeout=60_000)
                await context.storage_state(path=str(storage_state_path))
                _record_login(profile_name)
                print(f"Already logged in. Profile saved at: {profile_dir.resolve()}")
                print(f"Storage state saved at: {storage_state_path.resolve()}")
                return 0

            await page.goto(INSTAGRAM_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            print("")
            print("Instagram login browser is open.")
            print("1. Log in manually in the browser window.")
            print("2. Complete any 2FA/checkpoint inside that same window.")
            print("3. This script will close automatically after it detects the saved session.")
            print("")
            print(f"Waiting up to {wait_seconds} seconds...")

            while time.monotonic() < deadline:
                if await _has_instagram_session(context):
                    await page.wait_for_timeout(3000)
                    await page.goto(check_url, wait_until="domcontentloaded", timeout=60_000)
                    if not await _has_instagram_session(context):
                        await page.goto(INSTAGRAM_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
                        await page.wait_for_timeout(2000)
                        continue
                    await context.storage_state(path=str(storage_state_path))
                    _record_login(profile_name)
                    print(f"Login detected. Session saved at: {profile_dir.resolve()}")
                    print(f"Storage state saved at: {storage_state_path.resolve()}")
                    return 0
                await page.wait_for_timeout(2000)

            print("Timed out before login was detected.", file=sys.stderr)
            print(f"Profile folder kept at: {profile_dir.resolve()}", file=sys.stderr)
            return 1
        finally:
            await context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Open a visible Playwright browser and save an Instagram login session "
            "into a named profile under data/browser-profiles/instagram/<profile>/."
        )
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_NAME,
        help=(
            f"Profile name (default: {DEFAULT_PROFILE_NAME}). "
            "Use different names like 'alt1', 'alt2' for multiple accounts."
        ),
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="Override profile directory entirely (advanced; ignores --profile).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=600,
        help="How long to wait for manual login before timing out",
    )
    parser.add_argument(
        "--check-url",
        default="https://www.instagram.com/meme.ig/",
        help="URL to open after login is detected",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.wait_seconds < 30:
        parser.error("--wait-seconds must be at least 30.")
    resolved_profile_dir = (
        args.profile_dir if args.profile_dir is not None else session_profile_dir(args.profile)
    )
    print(f"Using profile: {args.profile} ({resolved_profile_dir})", file=sys.stderr)
    # Only record the login under the named-profile manifest entry when the
    # user picked it by name (not when overriding the directory entirely).
    profile_name_for_pool = None if args.profile_dir is not None else args.profile
    return asyncio.run(
        login_instagram(
            profile_dir=resolved_profile_dir,
            wait_seconds=args.wait_seconds,
            check_url=args.check_url,
            profile_name=profile_name_for_pool,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
