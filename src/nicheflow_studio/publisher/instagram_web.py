from __future__ import annotations

import argparse
import os
import subprocess
import sys

from nicheflow_studio.core.instagram_session import DEFAULT_PROFILE_NAME, profile_dir


INSTAGRAM_UPLOAD_URL = "https://www.instagram.com/"


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
