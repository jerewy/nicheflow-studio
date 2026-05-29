"""Probe the instaloader metadata extraction path.

Standalone diagnostic - does NOT save to DB, does NOT affect the yt-dlp flow.

Usage:
    python scripts/instagram_instaloader_probe.py --url URL [--url URL ...]
    python scripts/instagram_instaloader_probe.py --file data/discovered/accounts/5-memeists-daily/meme.ig-urls.json --limit 5

This tells you:
  - Whether the Playwright session was injected successfully
  - What metadata instaloader returns for each reel
  - Where failures happen (rate limit / private / no video / other)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nicheflow_studio.core.instagram_session import (
    _PLAYWRIGHT_STORAGE_STATE_PATH,
    load_playwright_cookies_into_instaloader,
)
from nicheflow_studio.scraper.instagram import (
    InstagramScrapeStats,
    _make_loader,
    _classify_instaloader_error,
    instagram_shortcode_from_url,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import instaloader as _instaloader_module
except ImportError:
    print("ERROR: instaloader is not installed. Run: pip install instaloader")
    raise SystemExit(1)


def _read_urls(*, urls: list[str], file_path: str | None, limit: int | None) -> list[str]:
    collected = [u.strip() for u in urls if u.strip()]
    if file_path:
        path = Path(file_path)
        raw = path.read_text(encoding="utf-8").strip().strip("\ufeff")
        if path.suffix.lower() == ".json":
            loaded = json.loads(raw or "[]")
            if isinstance(loaded, list):
                for item in loaded:
                    if isinstance(item, str) and item.strip():
                        collected.append(item.strip())
        else:
            for line in raw.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    collected.append(line)
    if limit is not None:
        collected = collected[:limit]
    return collected


def _print_session_status(playwright_ok: bool) -> None:
    print("-" * 60)
    print("Session status")
    print("-" * 60)
    print(
        f"  Playwright storage state : "
        f"{'found' if _PLAYWRIGHT_STORAGE_STATE_PATH.exists() else 'NOT FOUND'}"
    )
    print(f"  Playwright sessionid injected : {'yes' if playwright_ok else 'no'}")
    if not playwright_ok:
        print(
            "  NOTE: Run the app once to create a browser session, "
            "or re-export cookies via instagram_save_cookies.py"
        )
    print()


def _print_post_result(url: str, post: object | None, error: str | None) -> None:
    shortcode = url.rstrip("/").rsplit("/", 1)[-1]
    print(f"  URL      : {url}")
    if error:
        print(f"  status   : FAILED - {error}")
        print()
        return

    assert post is not None
    is_video = getattr(post, "is_video", False)
    print(f"  status   : {'OK (video)' if is_video else 'SKIPPED (not a video)'}")
    print(f"  owner    : @{getattr(post, 'owner_username', '?')}")
    print(f"  likes    : {getattr(post, 'likes', '?')}")

    view_count = getattr(post, "video_view_count", None) or getattr(post, "video_play_count", None)
    print(f"  views    : {view_count if view_count is not None else '(not available)'}")
    print(f"  comments : {getattr(post, 'comments', '?')}")

    duration = getattr(post, "video_duration", None)
    print(f"  duration : {int(duration)}s" if duration else "  duration : (not available)")

    date_utc = getattr(post, "date_utc", None)
    print(f"  posted   : {date_utc.date() if date_utc else '?'}")

    caption = getattr(post, "caption", None) or ""
    if caption:
        # Collapse newlines so the full preview fits on one line
        preview = " ".join(caption.split())
        caption_display = (preview[:200] + "...") if len(preview) > 200 else preview
    else:
        caption_display = "(no caption)"
    print(f"  caption  : {caption_display}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe instaloader metadata for Instagram Reel URLs (no DB writes)."
    )
    parser.add_argument("--url", action="append", default=[], help="Instagram Reel URL")
    parser.add_argument("--file", help="JSON or text file with one URL per line")
    parser.add_argument("--limit", type=int, default=5, help="Max URLs to probe (default: 5)")
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds to wait between requests (default: 2.0)",
    )
    args = parser.parse_args()

    urls = _read_urls(urls=args.url, file_path=args.file, limit=args.limit)
    if not urls:
        parser.error("Provide at least one --url or --file.")

    # Set up loader and inject Playwright session
    loader = _make_loader()
    playwright_ok = load_playwright_cookies_into_instaloader(loader)

    _print_session_status(playwright_ok)

    print("-" * 60)
    print(f"Probing {len(urls)} URL(s) via instaloader")
    print("-" * 60)
    print()

    stats = InstagramScrapeStats(input_urls=len(urls), extraction_limit=len(urls))
    rate_limit_hit = False

    import time, random

    for index, url in enumerate(urls):
        shortcode = instagram_shortcode_from_url(url)
        if not shortcode:
            print(f"  URL      : {url}")
            print("  status   : SKIPPED - not a recognised Instagram reel/post URL")
            print()
            stats.failed_other += 1
            continue

        if index > 0:
            wait = args.delay + random.uniform(0, 1.0)
            print(f"  (waiting {wait:.1f}s...)")
            time.sleep(wait)

        stats.attempted += 1
        try:
            post = _instaloader_module.Post.from_shortcode(loader.context, shortcode)
            _print_post_result(url, post, error=None)

            is_video = getattr(post, "is_video", False)
            if is_video:
                stats.extracted += 1
            else:
                stats.failed_no_video += 1

        except Exception as exc:  # noqa: BLE001
            kind = _classify_instaloader_error(exc)
            error_label = {
                "rate_limit": f"rate limit / login required ({exc})",
                "unavailable": f"unavailable or private ({exc})",
                "no_video": f"no video in post ({exc})",
                "other": f"unexpected error ({exc})",
            }[kind]
            _print_post_result(url, None, error=error_label)

            if kind == "rate_limit":
                stats.failed_rate_limited += 1
                rate_limit_hit = True
                print("  WARNING: Rate limit hit - stopping probe.")
                break
            elif kind == "unavailable":
                stats.failed_unavailable += 1
            elif kind == "no_video":
                stats.failed_no_video += 1
            else:
                stats.failed_other += 1

    print("-" * 60)
    print("Probe summary")
    print("-" * 60)
    print(f"  input URLs            : {stats.input_urls}")
    print(f"  attempted             : {stats.attempted}")
    print(f"  extracted (video OK)  : {stats.extracted}")
    print(f"  failed no video       : {stats.failed_no_video}")
    print(f"  failed unavailable    : {stats.failed_unavailable}")
    print(f"  failed rate limited   : {stats.failed_rate_limited}")
    print(f"  failed other          : {stats.failed_other}")
    print(f"  rate limit stopped    : {'yes' if rate_limit_hit else 'no'}")
    print()

    if rate_limit_hit:
        print("Instagram rate limited the session. Wait 15-30 min before running again.")
        return 1
    if stats.extracted == 0 and stats.attempted > 0:
        print("No videos extracted. Check session status above.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
