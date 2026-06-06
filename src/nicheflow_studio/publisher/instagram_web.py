from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from nicheflow_studio.core.instagram_session import DEFAULT_PROFILE_NAME, profile_dir


log = logging.getLogger(__name__)

INSTAGRAM_UPLOAD_URL = "https://www.instagram.com/"
INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"


def launch_instagram_login(
    profile_name: str | None = None,
    *,
    wait_seconds: int = 120,
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
    minimized: bool = True,
) -> subprocess.Popen[bytes]:
    """Open Instagram using the saved Playwright profile."""
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
    if minimized:
        command.append("--minimized")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(  # noqa: S603 - command is fixed; profile/url are arguments.
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _minimize_browser_window(context, page) -> None:  # noqa: ANN001
    """Best-effort minimize for persistent Chrome windows."""
    try:
        cdp_session = context.new_cdp_session(page)
        window_info = cdp_session.send("Browser.getWindowForTarget")
        cdp_session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_info["windowId"],
                "bounds": {"windowState": "minimized"},
            },
        )
    except Exception:  # noqa: BLE001 - minimizing is cosmetic
        log.debug("could not minimize Instagram upload assist window", exc_info=True)


def open_visible_instagram_profile(
    profile_name: str,
    *,
    url: str = INSTAGRAM_UPLOAD_URL,
    minimized: bool = True,
) -> None:
    """Open a persistent Chromium profile and keep it alive until closed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    user_data_dir = profile_dir(profile_name)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        launch_kwargs: dict = {"headless": False, "viewport": None, "channel": "chrome"}
        if minimized:
            launch_kwargs["args"] = ["--start-minimized"]
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir), **launch_kwargs
            )
        except PlaywrightError:
            launch_kwargs.pop("channel")
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir), **launch_kwargs
            )
        page = context.pages[0] if context.pages else context.new_page()
        if minimized:
            _minimize_browser_window(context, page)
        page.goto(url, wait_until="domcontentloaded")
        if minimized:
            _minimize_browser_window(context, page)
        else:
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
    parser.add_argument(
        "--minimized",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep the browser window minimized instead of bringing it to front.",
    )
    args = parser.parse_args()
    open_visible_instagram_profile(
        str(args.profile),
        url=str(args.url),
        minimized=bool(args.minimized),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
