from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
STORAGE_STATE_FILE = "storage-state.json"


def normalize_instagram_media_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.netloc.lower()
    if host not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    media_index = next(
        (index for index, part in enumerate(parts) if part in {"p", "reel", "tv"}),
        None,
    )
    if media_index is None or len(parts) <= media_index + 1:
        return None

    kind = parts[media_index]
    shortcode = parts[media_index + 1]
    if not shortcode:
        return None
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def profile_url(username: str) -> str:
    cleaned = username.strip().lstrip("@").strip("/")
    if not cleaned:
        raise ValueError("Instagram username is required.")
    return f"https://www.instagram.com/{cleaned}/"


def read_url_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw_text = path.read_text(encoding="utf-8").strip().strip("\ufeff")
    if not raw_text:
        return []
    if path.suffix.lower() == ".json":
        loaded = json.loads(raw_text)
        if not isinstance(loaded, list):
            raise ValueError(f"{path} must contain a JSON list of Instagram URLs.")
        return [item for item in loaded if isinstance(item, str) and item.strip()]
    return [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def merge_urls(existing_urls: list[str], new_urls: set[str]) -> list[str]:
    merged: dict[str, None] = {}
    for url in existing_urls:
        normalized = normalize_instagram_media_url(url)
        if normalized is not None:
            merged.setdefault(normalized, None)
    for url in new_urls:
        normalized = normalize_instagram_media_url(url)
        if normalized is not None:
            merged.setdefault(normalized, None)
    return list(merged)


def count_new_urls(existing_urls: list[str], merged_urls: list[str]) -> int:
    existing_normalized = {
        normalized
        for url in existing_urls
        if (normalized := normalize_instagram_media_url(url)) is not None
    }
    merged_normalized = {
        normalized
        for url in merged_urls
        if (normalized := normalize_instagram_media_url(url)) is not None
    }
    return len(merged_normalized - existing_normalized)


async def _dismiss_login_wall(page) -> None:  # noqa: ANN001
    for label in ("Not now", "Close"):
        locator = page.get_by_role("button", name=label)
        try:
            if await locator.is_visible(timeout=500):
                await locator.click()
                await page.wait_for_timeout(500)
        except PlaywrightTimeoutError:
            continue


async def _collect_visible_urls(page) -> set[str]:  # noqa: ANN001
    links = await page.eval_on_selector_all(
        "a[href*='/p/'], a[href*='/reel/'], a[href*='/tv/']",
        "els => els.map(e => e.href)",
    )
    discovered: set[str] = set()
    for link in links:
        if not isinstance(link, str):
            continue
        normalized = normalize_instagram_media_url(link)
        if normalized is not None:
            discovered.add(normalized)
    return discovered


async def _has_instagram_session(context) -> bool:  # noqa: ANN001
    cookies = await context.cookies("https://www.instagram.com")
    return any(cookie.get("name") == "sessionid" for cookie in cookies)


async def _page_contains_login_prompt(page) -> bool:  # noqa: ANN001
    text = await page.locator("body").inner_text(timeout=5_000)
    normalized = text.lower()
    return "log in" in normalized and "sign up" in normalized


async def _scroll_metrics(page) -> dict[str, object]:  # noqa: ANN001
    return await page.evaluate(
        """() => {
            const scrollables = [...document.querySelectorAll("body, main, div")]
                .filter((el) => el.scrollHeight > el.clientHeight + 80)
                .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
            const primary = scrollables[0] || document.scrollingElement || document.documentElement;
            return {
                windowY: window.scrollY || document.documentElement.scrollTop || 0,
                documentHeight: document.documentElement.scrollHeight,
                bodyHeight: document.body ? document.body.scrollHeight : 0,
                viewportHeight: window.innerHeight,
                primaryTag: primary.tagName,
                primaryTop: primary.scrollTop || 0,
                primaryHeight: primary.scrollHeight || 0,
                primaryClientHeight: primary.clientHeight || 0,
                scrollableCount: scrollables.length,
            };
        }"""
    )


def _format_scroll_metrics(metrics: dict[str, object]) -> str:
    return (
        f"window={metrics.get('windowY')}/{metrics.get('documentHeight')} "
        f"body={metrics.get('bodyHeight')} viewport={metrics.get('viewportHeight')} "
        f"primary={metrics.get('primaryTag')}:"
        f"{metrics.get('primaryTop')}/{metrics.get('primaryHeight')} "
        f"scrollables={metrics.get('scrollableCount')}"
    )


async def _scroll_for_more(page) -> None:  # noqa: ANN001
    links = page.locator("a[href*='/p/'], a[href*='/reel/'], a[href*='/tv/']")
    link_count = await links.count()
    if link_count:
        try:
            await links.nth(link_count - 1).scroll_into_view_if_needed(timeout=5_000)
        except PlaywrightTimeoutError:
            pass

    await page.keyboard.press("End")
    await page.mouse.wheel(0, 2200)
    await page.evaluate(
        """() => {
            const scrollingElement = document.scrollingElement || document.documentElement;
            scrollingElement.scrollTo(0, scrollingElement.scrollHeight);
            const main = document.querySelector("main") || document.querySelector("[role='main']");
            if (main && main.scrollHeight > main.clientHeight) {
                main.scrollBy(0, 1200);
            }
            const scrollables = [...document.querySelectorAll("body, main, div")]
                .filter((el) => el.scrollHeight > el.clientHeight + 80)
                .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
            for (const el of scrollables.slice(0, 5)) {
                el.scrollBy(0, 1600);
            }
        }"""
    )


async def _wait_after_scroll(page, wait_ms: int) -> None:  # noqa: ANN001
    extra_wait = random.randint(0, 2_000)
    await page.wait_for_timeout(wait_ms + extra_wait)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass


async def discover_profile_urls(
    *,
    username: str,
    limit: int,
    scroll_count: int,
    wait_ms: int,
    headed: bool,
    mobile: bool,
    initial_urls: list[str] | None = None,
    checkpoint_path: Path | None = None,
    stall_limit: int = 3,
    user_data_dir: Path | None = None,
    min_new: int = 0,
) -> list[str]:
    async with async_playwright() as playwright:
        context_options = {
            "user_agent": MOBILE_USER_AGENT if mobile else DESKTOP_USER_AGENT,
            "viewport": {"width": 430, "height": 932} if mobile else {"width": 1280, "height": 900},
            "is_mobile": mobile,
            "has_touch": mobile,
        }
        browser = None
        if user_data_dir is not None:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            storage_state_path = user_data_dir / STORAGE_STATE_FILE
            if storage_state_path.exists():
                browser = await playwright.chromium.launch(headless=not headed)
                context = await browser.new_context(
                    storage_state=str(storage_state_path),
                    **context_options,
                )
            else:
                context = await playwright.chromium.launch_persistent_context(
                    str(user_data_dir),
                    headless=not headed,
                    **context_options,
                )
        else:
            browser = await playwright.chromium.launch(headless=not headed)
            context = await browser.new_context(**context_options)
        page = await context.new_page()
        try:
            await page.goto(profile_url(username), wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(wait_ms)
            await _dismiss_login_wall(page)
            has_session = await _has_instagram_session(context)
            has_login_prompt = await _page_contains_login_prompt(page)
            print(
                "auth: "
                f"sessionid={'yes' if has_session else 'no'}, "
                f"login_prompt={'yes' if has_login_prompt else 'no'}",
                file=sys.stderr,
            )

            baseline_urls = initial_urls or []
            urls = merge_urls(baseline_urls, set())
            previous_count = len(urls)
            stalled_scrolls = 0
            for index in range(scroll_count + 1):
                urls = merge_urls(urls, await _collect_visible_urls(page))
                added = len(urls) - previous_count
                new_this_run = count_new_urls(baseline_urls, urls)
                metrics = await _scroll_metrics(page)
                if checkpoint_path is not None:
                    write_urls(checkpoint_path, urls[:limit])
                print(
                    (
                        f"scroll {index}/{scroll_count}: {len(urls)} urls found, "
                        f"{added} new, {new_this_run} new this run, "
                        f"{_format_scroll_metrics(metrics)}"
                    ),
                    file=sys.stderr,
                )
                if len(urls) >= limit:
                    print(f"stopped: reached limit {limit}", file=sys.stderr)
                    break
                if min_new > 0 and new_this_run >= min_new:
                    print(
                        f"stopped: reached min-new target ({new_this_run}/{min_new})",
                        file=sys.stderr,
                    )
                    break
                if index > 0 and added <= 0:
                    stalled_scrolls += 1
                    if stall_limit >= 0 and stalled_scrolls >= stall_limit:
                        if min_new > 0 and new_this_run < min_new:
                            print(
                                (
                                    "stopped: no new URLs after "
                                    f"{stalled_scrolls} scrolls "
                                    f"({new_this_run}/{min_new} min-new)"
                                ),
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"stopped: no new URLs after {stalled_scrolls} scrolls",
                                file=sys.stderr,
                            )
                        break
                else:
                    stalled_scrolls = 0
                previous_count = len(urls)

                await _scroll_for_more(page)
                await _wait_after_scroll(page, wait_ms)
                await _dismiss_login_wall(page)

            return urls[:limit]
        finally:
            await context.close()
            if browser is not None:
                await browser.close()


def write_urls(path: Path, urls: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(urls, indent=2), encoding="utf-8")
    else:
        path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return path


def save_candidates(*, url_file: Path, limit: int, account_name: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("instagram_scrape_urls.py")),
            "--file",
            str(url_file),
            "--limit",
            str(limit),
            "--save-account",
            account_name,
            "--pretty",
        ],
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover public Instagram post/Reel URLs from a profile with Playwright."
    )
    parser.add_argument("--username", required=True, help="Instagram username, e.g. meme.ig")
    parser.add_argument("--limit", type=int, default=30, help="Max URLs to collect")
    parser.add_argument(
        "--min-new",
        type=int,
        default=0,
        help="Keep scrolling until at least this many URLs not already in --out are found",
    )
    parser.add_argument("--scrolls", type=int, default=10, help="Scroll attempts")
    parser.add_argument("--wait-ms", type=int, default=2000, help="Wait after navigation/scrolls")
    parser.add_argument("--out", type=Path, help="Write URLs to .json or text file")
    parser.add_argument("--resume", action="store_true", help="Load existing URLs from --out first")
    parser.add_argument(
        "--stall-limit",
        type=int,
        default=3,
        help="Stop after this many scrolls with no new URLs; use -1 to disable",
    )
    parser.add_argument("--save-account", help="Save discovered metadata as app candidates")
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        help="Persistent Playwright browser profile directory for logged-in sessions",
    )
    parser.add_argument("--mobile", action="store_true", help="Use mobile browser emulation")
    parser.add_argument("--headed", action="store_true", help="Show browser for debugging")
    return parser


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1.")
    if args.scrolls < 0:
        parser.error("--scrolls must be 0 or greater.")
    if args.min_new < 0:
        parser.error("--min-new must be 0 or greater.")
    if args.resume and args.out is None:
        parser.error("--resume requires --out.")

    output_path = args.out
    if output_path is None and args.save_account:
        safe_username = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_"
            for char in args.username.strip().lstrip("@")
        )
        output_path = Path("data") / "discovered" / f"instagram-{safe_username}-urls.json"

    initial_urls = read_url_file(output_path) if args.resume and output_path is not None else []
    if initial_urls:
        print(f"Loaded {len(initial_urls)} URLs from {output_path}", file=sys.stderr)

    urls = await discover_profile_urls(
        username=args.username,
        limit=args.limit,
        scroll_count=args.scrolls,
        wait_ms=args.wait_ms,
        headed=args.headed,
        mobile=args.mobile,
        initial_urls=initial_urls,
        checkpoint_path=output_path,
        stall_limit=args.stall_limit,
        user_data_dir=args.user_data_dir,
        min_new=args.min_new,
    )
    print(f"Found {len(urls)} URLs")
    for url in urls:
        print(url)

    if output_path is not None:
        written_path = write_urls(output_path, urls)
        print(f"Saved URLs to {written_path}")
        if args.save_account:
            save_candidates(url_file=written_path, limit=args.limit, account_name=args.save_account)

    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
