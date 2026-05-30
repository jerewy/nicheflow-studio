from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from nicheflow_studio.core.instagram_session import (
    DEFAULT_PROFILE_NAME,
    load_playwright_cookies_from_storage_state,
    profile_storage_state_path,
)
from nicheflow_studio.core.instagram_profile_pool import (
    AUTO_PROFILE,
    NoAvailableProfilesError,
    ProfilePool,
)


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


def normalize_instagram_reel_url(value: str) -> str | None:
    normalized = normalize_instagram_media_url(value)
    if normalized is None:
        return None
    return normalized if "/reel/" in normalized else None


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
        return [
            normalized
            for item in loaded
            if isinstance(item, str) and item.strip()
            if (normalized := normalize_instagram_reel_url(item)) is not None
        ]
    return [
        normalized
        for line in raw_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
        if (normalized := normalize_instagram_reel_url(line)) is not None
    ]


def merge_urls(existing_urls: list[str], new_urls: set[str]) -> list[str]:
    merged: dict[str, None] = {}
    for url in existing_urls:
        normalized = normalize_instagram_reel_url(url)
        if normalized is not None:
            merged.setdefault(normalized, None)
    for url in new_urls:
        normalized = normalize_instagram_reel_url(url)
        if normalized is not None:
            merged.setdefault(normalized, None)
    return list(merged)


def count_new_urls(existing_urls: list[str], merged_urls: list[str]) -> int:
    existing_normalized = {
        normalized
        for url in existing_urls
        if (normalized := normalize_instagram_reel_url(url)) is not None
    }
    merged_normalized = {
        normalized
        for url in merged_urls
        if (normalized := normalize_instagram_reel_url(url)) is not None
    }
    return len(merged_normalized - existing_normalized)


def should_fast_forward_through_cache(
    *,
    baseline_count: int,
    new_this_run: int,
    previous_new_this_run: int,
) -> bool:
    return baseline_count > 0 and new_this_run == previous_new_this_run


def effective_resume_limit(*, requested_limit: int, initial_count: int) -> int:
    return requested_limit + initial_count if initial_count > 0 else requested_limit


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
        "a[href*='/reel/']",
        "els => els.map(e => e.href)",
    )
    discovered: set[str] = set()
    for link in links:
        if not isinstance(link, str):
            continue
        normalized = normalize_instagram_reel_url(link)
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


async def _scroll_for_more(page, *, fast: bool = False) -> None:  # noqa: ANN001
    links = page.locator("a[href*='/reel/']")
    link_count = await links.count()
    if link_count and not fast:
        try:
            await links.nth(link_count - 1).scroll_into_view_if_needed(timeout=5_000)
        except PlaywrightTimeoutError:
            pass

    await page.keyboard.press("End")
    await page.mouse.wheel(0, 7000 if fast else 2200)
    await page.evaluate(
        """(fast) => {
            const scrollingElement = document.scrollingElement || document.documentElement;
            scrollingElement.scrollTo(0, scrollingElement.scrollHeight);
            const main = document.querySelector("main") || document.querySelector("[role='main']");
            if (main && main.scrollHeight > main.clientHeight) {
                main.scrollBy(0, fast ? 5000 : 1200);
            }
            const scrollables = [...document.querySelectorAll("body, main, div")]
                .filter((el) => el.scrollHeight > el.clientHeight + 80)
                .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
            for (const el of scrollables.slice(0, 5)) {
                el.scrollBy(0, fast ? 7000 : 1600);
            }
        }""",
        fast,
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
    cookie_profile: str = DEFAULT_PROFILE_NAME,
    require_session: bool = True,
    min_new: int = 0,
) -> list[str]:
    async with async_playwright() as playwright:
        context_options = {
            "user_agent": MOBILE_USER_AGENT if mobile else DESKTOP_USER_AGENT,
            "viewport": {"width": 430, "height": 932} if mobile else {"width": 1280, "height": 900},
            "is_mobile": mobile,
            "has_touch": mobile,
        }
        # Resolve where the session lives. --cookie-profile is the new path;
        # --user-data-dir is kept so the existing launcher keeps working.
        if user_data_dir is not None:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            storage_state_path = user_data_dir / STORAGE_STATE_FILE
        else:
            storage_state_path = profile_storage_state_path(cookie_profile)
            storage_state_path.parent.mkdir(parents=True, exist_ok=True)

        # Always a fresh ephemeral context; we inject cookies ourselves so we can
        # see exactly which ones IG accepts. Playwright's storage_state= path was
        # silently dropping cookies (sameSite/secure sanitization), which is what
        # caused the "instaloader injected: yes" but "discover sessionid: no" split.
        browser = await playwright.chromium.launch(headless=not headed)
        context = await browser.new_context(**context_options)
        cookies = load_playwright_cookies_from_storage_state(cookie_profile)
        if user_data_dir is not None and not cookies and storage_state_path.exists():
            # Legacy fallback when explicitly pointed at a directory.
            try:
                raw = json.loads(storage_state_path.read_text(encoding="utf-8"))
                cookies = [c for c in raw.get("cookies", []) if isinstance(c, dict)]
            except Exception:  # noqa: BLE001
                cookies = []
        if cookies:
            await context.add_cookies(cookies)
        print(
            f"cookie profile: {cookie_profile} "
            f"({len(cookies)} cookie(s) loaded from {storage_state_path})",
            file=sys.stderr,
        )

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
            if require_session and not has_session:
                raise RuntimeError(
                    f"Instagram session missing for profile '{cookie_profile}'. "
                    f"Run: python scripts/instagram_login_playwright.py --profile {cookie_profile}"
                )

            baseline_urls = initial_urls or []
            urls = merge_urls(baseline_urls, set())
            previous_count = len(urls)
            previous_new_this_run = 0
            stalled_scrolls = 0
            for index in range(scroll_count + 1):
                urls = merge_urls(urls, await _collect_visible_urls(page))
                added = len(urls) - previous_count
                new_this_run = count_new_urls(baseline_urls, urls)
                fast_forward = should_fast_forward_through_cache(
                    baseline_count=len(baseline_urls),
                    new_this_run=new_this_run,
                    previous_new_this_run=previous_new_this_run,
                )
                metrics = await _scroll_metrics(page)
                if checkpoint_path is not None:
                    write_urls(checkpoint_path, urls[:limit])
                print(
                    (
                        f"scroll {index}/{scroll_count}: {len(urls)} urls found, "
                        f"{added} new, {new_this_run} new this run, "
                        f"mode={'fast-known' if fast_forward else 'normal'}, "
                        f"{_format_scroll_metrics(metrics)}"
                    ),
                    file=sys.stderr,
                )
                if fast_forward and index > 0 and index % 10 == 0:
                    print(
                        "fast-forwarding through URLs already cached for this account",
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
                if index > 0 and added <= 0 and not fast_forward:
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
                previous_new_this_run = new_this_run

                await _scroll_for_more(page, fast=fast_forward)
                if fast_forward:
                    await page.wait_for_timeout(350)
                else:
                    await _wait_after_scroll(page, wait_ms)
                await _dismiss_login_wall(page)

            return urls[:limit]
        finally:
            try:
                await context.storage_state(path=str(storage_state_path))
            except Exception:  # noqa: BLE001
                pass
            await context.close()
            await browser.close()


def write_urls(path: Path, urls: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(urls, indent=2), encoding="utf-8")
    else:
        path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return path


def save_candidates(
    *,
    url_file: Path,
    limit: int,
    account_name: str,
    metadata_extractor: str = "apify",
) -> None:
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
            "--metadata-extractor",
            metadata_extractor,
            "--pretty",
        ],
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover public Instagram Reel URLs from a profile with Playwright."
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
        "--metadata-extractor",
        choices=("instaloader", "apify", "yt-dlp"),
        default="apify",
        help=(
            "Metadata backend used when --save-account is set. "
            "Default uses Apify to avoid local Instagram account automation during metadata extraction."
        ),
    )
    parser.add_argument(
        "--cookie-profile",
        default=DEFAULT_PROFILE_NAME,
        help=(
            f"Named login profile (default: {DEFAULT_PROFILE_NAME}). "
            f"Use '{AUTO_PROFILE}' to rotate across all logged-in profiles. "
            "Create with: python scripts/instagram_login_playwright.py --profile <name>"
        ),
    )
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="Do not require a logged-in session (will likely 0-new on most accounts).",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        help="(Legacy) Explicit profile directory; --cookie-profile is preferred.",
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
    effective_limit = effective_resume_limit(
        requested_limit=args.limit,
        initial_count=len(initial_urls),
    )
    if effective_limit != args.limit:
        print(
            f"Resume mode: collecting up to {args.limit} additional URL(s) "
            f"beyond {len(initial_urls)} cached.",
            file=sys.stderr,
        )

    pool: ProfilePool | None = None
    resolved_profile = args.cookie_profile
    if args.cookie_profile == AUTO_PROFILE:
        pool = ProfilePool.load()
        try:
            picked = pool.pick()
        except NoAvailableProfilesError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        resolved_profile = picked.name
        cooling = [
            f"{p.name}(until {p.cooldown_until.isoformat()})"
            for p in pool.profiles.values()
            if p.cooldown_until is not None and not p.is_cooled(datetime.now(timezone.utc))
        ]
        print(
            f"pool: picked '{resolved_profile}' "
            f"(available={len(pool.available())}, cooling={cooling or 'none'})",
            file=sys.stderr,
        )
    else:
        pool = ProfilePool.load()

    if pool is not None and resolved_profile in pool.profiles:
        if pool.session_is_stale(resolved_profile):
            warning = pool.session_warning(resolved_profile)
            print(f"ERROR: {warning}", file=sys.stderr)
            return 2
        warning = pool.session_warning(resolved_profile)
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)

    try:
        urls = await discover_profile_urls(
            username=args.username,
            limit=effective_limit,
            scroll_count=args.scrolls,
            wait_ms=args.wait_ms,
            headed=args.headed,
            mobile=args.mobile,
            initial_urls=initial_urls,
            checkpoint_path=output_path,
            stall_limit=args.stall_limit,
            user_data_dir=args.user_data_dir,
            cookie_profile=resolved_profile,
            require_session=not args.allow_anonymous,
            min_new=args.min_new,
        )
    except RuntimeError as exc:
        # Session missing or hard auth failure for the chosen profile.
        if pool is not None:
            pool.mark_dead(resolved_profile)
            print(
                f"pool: marked '{resolved_profile}' as dead for 24h ({exc})",
                file=sys.stderr,
            )
        raise

    if pool is not None:
        new_count = count_new_urls(initial_urls, urls)
        pool.mark_used(resolved_profile, count=new_count)
        print(
            f"pool: '{resolved_profile}' marked used "
            f"(+{new_count} new URLs this run)",
            file=sys.stderr,
        )
    print(f"Found {len(urls)} URLs")
    for url in urls:
        print(url)

    if output_path is not None:
        written_path = write_urls(output_path, urls)
        print(f"Saved URLs to {written_path}")
        if args.save_account:
            save_candidates(
                url_file=written_path,
                limit=args.limit,
                account_name=args.save_account,
                metadata_extractor=args.metadata_extractor,
            )

    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
