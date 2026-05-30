"""Smoke test for the Apify Instagram source.

Run this with one or more Instagram post/reel URLs to confirm the Apify
integration works end-to-end before wiring it into the main discover flow.

Usage:
    .venv\\Scripts\\python scripts\\instagram_apify_smoke.py https://www.instagram.com/reel/<shortcode>/

Requires APIFY_TOKEN in .env. Free Apify plan covers ~1,800 results/month.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make src/ importable when run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from nicheflow_studio.core.env import load_dotenv  # noqa: E402
from nicheflow_studio.scraper.instagram_apify import (  # noqa: E402
    ApifyConfigError,
    ApifyScrapeError,
    scrape_instagram_urls_apify,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apify Instagram smoke test")
    parser.add_argument(
        "urls",
        nargs="+",
        help="One or more Instagram post/reel URLs",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full candidate JSON instead of a short summary",
    )
    args = parser.parse_args()

    load_dotenv()

    try:
        candidates = scrape_instagram_urls_apify(args.urls)
    except ApifyConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2
    except ApifyScrapeError as exc:
        print(f"[scrape] {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "source_url": c.source_url,
                        "video_id": c.video_id,
                        "channel_name": c.channel_name,
                        "title": c.title,
                        "published_at": c.published_at.isoformat() if c.published_at else None,
                        "view_count": c.view_count,
                        "like_count": c.like_count,
                        "comment_count": c.comment_count,
                        "duration_seconds": c.duration_seconds,
                        "thumbnail_url": c.thumbnail_url,
                        "description": c.description,
                    }
                    for c in candidates
                ],
                indent=2,
            )
        )
        return 0

    print(f"OK  Apify returned {len(candidates)} candidate(s):")
    for c in candidates:
        print(
            f"  - {c.video_id}  @{c.channel_name}  "
            f"views={c.view_count}  likes={c.like_count}  "
            f"comments={c.comment_count}  dur={c.duration_seconds}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
