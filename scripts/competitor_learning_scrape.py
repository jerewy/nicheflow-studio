"""Competitor learning batch.

Scrape the N most-recent posts from each reference Instagram account via Apify
and write a structured dataset for pattern analysis (Step 1 of the sourcing
plan). Reuses ``scrape_instagram_source_apify`` — no new scraping code.

Usage:
    # default: the 10 reference accounts, 30 posts each
    .venv\\Scripts\\python scripts\\competitor_learning_scrape.py

    # custom
    .venv\\Scripts\\python scripts\\competitor_learning_scrape.py --limit 30 handle1 handle2

Requires APIFY_TOKEN in .env. Apify charges per returned result. Output goes to
data/_competitor_learning/<run-timestamp>/ : one JSON per account (resumable),
plus combined all_posts.json and all_posts.csv with computed metrics.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from nicheflow_studio.core.env import load_dotenv  # noqa: E402
from nicheflow_studio.scraper.instagram_apify import (  # noqa: E402
    ApifyConfigError,
    ApifyScrapeError,
    scrape_instagram_source_apify,
)

DEFAULT_HANDLES = [
    "theanomalists",
    "thehistologian",
    "crazyfactscorner",
    "houseofhistorian",
    "factsontheway",
    "thelegendartist",
    "themysterist",
    "thecinemast",
    "entertainist",
    "thelegendast",
]

# Naive topic guess from caption/title keywords. A rough signal for Step 2; the
# real classification (archive vs interview, layout) needs the clip itself.
TOPIC_KEYWORDS = {
    "movie_tv": ["movie", "film", "scene", "series", "show", "episode", "cinema", "actor", "director"],
    "music": ["song", "music", "album", "singer", "band", "lyric", "concert"],
    "celebrity": ["celebrity", "star", "famous", "interview", "red carpet", "fame"],
    "sports": ["game", "match", "player", "team", "championship", "tennis", "nba", "goal"],
    "crime": ["murder", "killer", "crime", "detective", "case", "victim", "police"],
    "mystery": ["mystery", "unsolved", "strange", "disappear", "conspiracy", "secret"],
    "history": ["history", "ancient", "war", "century", "historical", "old", "1900", "king", "queen"],
    "internet": ["viral", "internet", "meme", "online", "youtuber", "streamer"],
}


def _word_count(text: str | None) -> int:
    return len(text.split()) if text else 0


def _guess_topic(text: str | None) -> str:
    if not text:
        return "unknown"
    lowered = text.lower()
    best, best_hits = "unknown", 0
    for topic, words in TOPIC_KEYWORDS.items():
        hits = sum(1 for w in words if w in lowered)
        if hits > best_hits:
            best, best_hits = topic, hits
    return best


def _row_from_candidate(handle: str, c) -> dict:
    title = c.title or ""
    caption = c.description or ""
    views = c.view_count or 0
    likes = c.like_count or 0
    comments = c.comment_count or 0
    engagement_rate = round((likes + comments) / views, 4) if views else None
    return {
        "handle": handle,
        "url": c.source_url,
        "video_id": c.video_id,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "title_hook": title,
        "title_word_count": _word_count(title),
        "caption": caption,
        "caption_word_count": _word_count(caption),
        "has_caption": bool(caption.strip()),
        "view_count": c.view_count,
        "like_count": c.like_count,
        "comment_count": c.comment_count,
        "duration_seconds": c.duration_seconds,
        "engagement_rate": engagement_rate,
        "topic_guess": _guess_topic(f"{title}\n{caption}"),
        "thumbnail_url": c.thumbnail_url,
    }


def _mark_high_performers(rows: list[dict]) -> None:
    """Flag posts whose views are >= 1.5x the account's median (per account)."""
    by_handle: dict[str, list[dict]] = {}
    for row in rows:
        by_handle.setdefault(row["handle"], []).append(row)
    for handle, account_rows in by_handle.items():
        views = [r["view_count"] for r in account_rows if r["view_count"]]
        median = statistics.median(views) if views else 0
        for row in account_rows:
            v = row["view_count"] or 0
            row["account_median_views"] = median
            row["high_performer"] = bool(median and v >= 1.5 * median)


def main() -> int:
    parser = argparse.ArgumentParser(description="Competitor learning batch scrape")
    parser.add_argument("handles", nargs="*", default=[], help="IG handles (default: 10 refs)")
    parser.add_argument("--limit", type=int, default=30, help="Posts per account (default 30)")
    args = parser.parse_args()
    handles = args.handles or DEFAULT_HANDLES

    load_dotenv()

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "data" / "_competitor_learning" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {out_dir}")

    all_rows: list[dict] = []
    for handle in handles:
        per_account_file = out_dir / f"{handle}.json"
        url = f"https://www.instagram.com/{handle}/"
        print(f"\n[{handle}] scraping {args.limit} recent posts ...")
        try:
            candidates = scrape_instagram_source_apify(source_url=url, max_items=args.limit)
        except (ApifyConfigError, ApifyScrapeError) as exc:
            print(f"  [error] {exc}")
            per_account_file.write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
            continue
        rows = [_row_from_candidate(handle, c) for c in candidates]
        per_account_file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"  got {len(rows)} posts -> {per_account_file.name}")
        all_rows.extend(rows)

    if not all_rows:
        print("\nNo rows collected.")
        return 1

    _mark_high_performers(all_rows)
    (out_dir / "all_posts.json").write_text(json.dumps(all_rows, indent=2), encoding="utf-8")
    fields = list(all_rows[0].keys())
    with (out_dir / "all_posts.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nDone. {len(all_rows)} posts across {len(handles)} handles.")
    print(f"Combined: {out_dir / 'all_posts.json'} and all_posts.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
