from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright


DEFAULT_COOKIES_PATH = Path("data") / "browser-profiles" / "instagram-cookies.json"
DEFAULT_STORAGE_STATE_PATH = (
    Path("data") / "browser-profiles" / "instagram" / "storage-state.json"
)
INSTAGRAM_HOME = "https://www.instagram.com/"


def _same_site(value: object) -> str:
    if not isinstance(value, str):
        return "Lax"
    normalized = value.strip().lower()
    if normalized in {"strict", "lax", "none"}:
        return normalized.capitalize()
    if normalized in {"no_restriction", "unspecified"}:
        return "None" if normalized == "no_restriction" else "Lax"
    return "Lax"


def _cookie_domain(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ".instagram.com"
    domain = value.strip()
    if domain in {"instagram.com", "www.instagram.com"}:
        return ".instagram.com"
    return domain


def _cookie_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        return "/"
    return value


def _expires(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def normalize_cookie_editor_export(raw_cookies: object) -> list[dict[str, Any]]:
    if not isinstance(raw_cookies, list):
        raise ValueError("Cookie export must be a JSON list.")

    cookies: list[dict[str, Any]] = []
    for raw_cookie in raw_cookies:
        if not isinstance(raw_cookie, dict):
            continue
        name = raw_cookie.get("name")
        value = raw_cookie.get("value")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(value, str):
            continue

        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": _cookie_domain(raw_cookie.get("domain")),
            "path": _cookie_path(raw_cookie.get("path")),
            "httpOnly": bool(raw_cookie.get("httpOnly", False)),
            "secure": bool(raw_cookie.get("secure", True)),
            "sameSite": _same_site(raw_cookie.get("sameSite")),
        }
        expires = _expires(raw_cookie.get("expirationDate", raw_cookie.get("expires")))
        if expires is not None:
            cookie["expires"] = expires
        cookies.append(cookie)

    return cookies


async def inject_cookies(*, cookies_path: Path, storage_state_path: Path) -> int:
    if not cookies_path.exists():
        print(f"Cookie file not found: {cookies_path}", file=sys.stderr)
        return 1

    raw_cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
    cookies = normalize_cookie_editor_export(raw_cookies)
    cookie_names = {cookie["name"] for cookie in cookies}
    if "sessionid" not in cookie_names:
        print(
            "Cookie export does not include Instagram's sessionid cookie. "
            "Export cookies after logging into instagram.com in your real browser.",
            file=sys.stderr,
        )
        return 1

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto(INSTAGRAM_HOME, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3_000)
        stored_cookies = await context.cookies(INSTAGRAM_HOME)
        stored_names = {cookie.get("name") for cookie in stored_cookies}
        body_text = await page.locator("body").inner_text(timeout=5_000)
        login_prompt_visible = "Log into Instagram" in body_text or (
            "Log in" in body_text and "Sign up" in body_text
        )
        if "sessionid" not in stored_names or login_prompt_visible:
            print(
                "Injected cookies, but Instagram still shows a logged-out session. "
                "Re-export fresh cookies from Chrome/Edge and try again.",
                file=sys.stderr,
            )
            await browser.close()
            return 1

        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(storage_state_path))
        await browser.close()

    print(f"Loaded {len(cookies)} cookies from {cookies_path}")
    print(f"Session valid. Storage state saved at: {storage_state_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert exported Instagram browser cookies into Playwright storage state."
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=DEFAULT_COOKIES_PATH,
        help=f"Cookie-Editor JSON export path (default: {DEFAULT_COOKIES_PATH})",
    )
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=DEFAULT_STORAGE_STATE_PATH,
        help=f"Output Playwright storage state path (default: {DEFAULT_STORAGE_STATE_PATH})",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(
        inject_cookies(
            cookies_path=args.cookies,
            storage_state_path=args.storage_state,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
