from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from nicheflow_studio.core.instagram_session import DEFAULT_PROFILE_NAME, profile_dir


INSTAGRAM_UPLOAD_URL = "https://www.instagram.com/"
INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"


def launch_instagram_login(
    profile_name: str | None = None,
    *,
    wait_seconds: int = 600,
) -> subprocess.Popen[bytes]:
    """Open the manual Instagram login flow for a saved profile (re-login helper).

    Reuses scripts/instagram_login_playwright.py so the saved session
    (storage-state.json) and the login-date manifest are updated the same way
    a normal login does. Raises FileNotFoundError if the script is missing
    (e.g. a packaged build without scripts/).
    """
    selected_profile = (profile_name or DEFAULT_PROFILE_NAME).strip() or DEFAULT_PROFILE_NAME
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "instagram_login_playwright.py"
    if not script.exists():
        raise FileNotFoundError(f"login script not found at {script}")
    command = [
        sys.executable,
        str(script),
        "--profile",
        selected_profile,
        "--wait-seconds",
        str(wait_seconds),
    ]
    return subprocess.Popen(command)  # noqa: S603 - fixed command; profile is an argument.


def launch_instagram_upload_assist(
    profile_name: str | None = None,
    *,
    url: str = INSTAGRAM_UPLOAD_URL,
) -> subprocess.Popen[bytes]:
    """Open Instagram in a visible browser using the saved Playwright profile."""
    selected_profile = (profile_name or DEFAULT_PROFILE_NAME).strip() or DEFAULT_PROFILE_NAME
    command = [
        sys.executable,
        "-m",
        "nicheflow_studio.publisher.instagram_web",
        "--profile",
        selected_profile,
        "--url",
        url,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(  # noqa: S603 - command is fixed; profile/url are arguments.
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def open_visible_instagram_profile(profile_name: str, *, url: str = INSTAGRAM_UPLOAD_URL) -> None:
    """Open a persistent visible Chromium profile and keep it alive until closed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    user_data_dir = profile_dir(profile_name)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            viewport=None,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.bring_to_front()
        try:
            while context.pages:
                page.wait_for_timeout(1000)
        except PlaywrightError:
            return
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Open Instagram for assisted manual upload.")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    parser.add_argument("--url", default=INSTAGRAM_UPLOAD_URL)
    args = parser.parse_args()
    open_visible_instagram_profile(str(args.profile), url=str(args.url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
