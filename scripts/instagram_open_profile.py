from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from nicheflow_studio.core.instagram_session import (
    DEFAULT_PROFILE_NAME,
    profile_dir as session_profile_dir,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Open a visible browser on a saved Instagram profile and keep it "
            "open for manual use (e.g. posting a reel by hand). Close the "
            "browser window to exit."
        )
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE_NAME,
        help=f"Profile name under data/browser-profiles/instagram/ (default: {DEFAULT_PROFILE_NAME})",
    )
    parser.add_argument(
        "--url",
        default="https://www.instagram.com/",
        help="URL to open first",
    )
    args = parser.parse_args()

    profile_dir = session_profile_dir(args.profile)
    if not profile_dir.exists():
        print(f"ERROR: profile folder not found: {profile_dir}", file=sys.stderr)
        return 1

    print(f"Opening profile '{args.profile}' ({profile_dir})", file=sys.stderr)
    with sync_playwright() as playwright:
        # Same launch shape as instagram_login_playwright.py so the session
        # keeps the fingerprint Instagram already knows for this profile.
        launch_kwargs: dict = {
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
            "channel": "chrome",
        }
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir.resolve()), **launch_kwargs
            )
        except PlaywrightError:
            launch_kwargs.pop("channel")
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir.resolve()), **launch_kwargs
            )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        print("Browser is open. Work manually; close the browser window when done.")
        try:
            context.wait_for_event("close", timeout=0)
        except PlaywrightError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
