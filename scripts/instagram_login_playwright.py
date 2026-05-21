from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright


DEFAULT_PROFILE_DIR = Path("data") / "browser-profiles" / "instagram"
STORAGE_STATE_FILE = "storage-state.json"
INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"


async def _has_instagram_session(context) -> bool:  # noqa: ANN001
    cookies = await context.cookies("https://www.instagram.com")
    cookie_names = {cookie.get("name") for cookie in cookies}
    return "sessionid" in cookie_names


async def login_instagram(*, profile_dir: Path, wait_seconds: int, check_url: str) -> int:
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
        description="Open a visible Playwright browser and save an Instagram login session."
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=f"Persistent browser profile directory (default: {DEFAULT_PROFILE_DIR})",
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
    return asyncio.run(
        login_instagram(
            profile_dir=args.profile_dir,
            wait_seconds=args.wait_seconds,
            check_url=args.check_url,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
