"""Scrape ~10 posts from cinema.defined to extract their title/caption patterns."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nicheflow_studio.scraper.instagram import scrape_instagram_source

TARGET_PROFILE = "https://www.instagram.com/cinema.defined/"
MAX_ITEMS = 10


def main() -> None:
    print(f"Scraping {MAX_ITEMS} posts from {TARGET_PROFILE}...\n")
    try:
        candidates = scrape_instagram_source(
            source_url=TARGET_PROFILE,
            max_items=MAX_ITEMS,
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"Got {len(candidates)} posts.\n")
    print("=" * 70)
    for i, c in enumerate(candidates, 1):
        print(f"\n--- POST {i} ---")
        print(f"URL:   {c.source_url}")
        print(f"Likes: {c.like_count}  Views: {c.view_count}  Comments: {c.comment_count}")
        print(f"TITLE: {c.title!r}")
        print(f"CAPTION:\n{c.description or '(no caption)'}")
        print("-" * 70)


if __name__ == "__main__":
    main()
