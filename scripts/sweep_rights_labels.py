"""Heuristic rights-label sweep for accepted-but-unlabeled pool items.

Purpose (docs/SOURCING_POOLING_PLAN.md §2.2): a large backlog of accepted pool
items has ``rights_confidence`` NULL, and some are already assigned and queued to
publish unchecked. Manually classifying every one is slow. This script scans the
source title/description/channel for keyword signals and SUGGESTS a rights label,
narrowing the human's manual pass to the risky subset — it does not replace it.

    # Dry run (default): print suggestions, write nothing. Assigned items only.
    .venv/Scripts/python.exe scripts/sweep_rights_labels.py

    # Widen to every accepted+unlabeled item, not just assigned ones.
    .venv/Scripts/python.exe scripts/sweep_rights_labels.py --no-assigned-only

    # Actually write the suggested labels (one transaction). Review the dry run first.
    .venv/Scripts/python.exe scripts/sweep_rights_labels.py --apply

Heuristics are ordered by RIGHTS RISK. A clip that matches a risky category
(news_broadcast > tv_moment > broadcast_sport) is labeled that even if it also
matches archival/meme keywords — the risky match always wins, so --apply never
downgrades a broadcast clip to a "safe" label.

Data join: pool_items -> media_assets (media_asset_id) -> scrape_candidates ON
scrape_candidates.video_id == media_assets.source_shortcode (verified to match;
canonical_source_url does NOT). Items with no matching candidate text can't be
classified and are reported as "needs manual review".

Keyword matching is case-insensitive and whole-word (so "goal" does not fire on
"goalkeeper"). Edit the module-level keyword tuples freely — tuning them is the
whole point of the script.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from collections import Counter

# UTF-8 console so this runs on a stock Windows (cp1252) terminal — same fix as
# scripts/pool_admin.py.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

from nicheflow_studio.db import pools  # noqa: E402
from nicheflow_studio.db.models import (  # noqa: E402
    Assignment,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
)
from nicheflow_studio.db.session import get_session, init_db  # noqa: E402

# SQLite caps a statement at ~999 bound variables; chunk IN() lists below that.
_IN_CHUNK = 500

# --- Keyword heuristics (edit these freely) ------------------------------- #
NEWS_BROADCAST_KEYWORDS = (
    "president", "minister", "funeral", "ceremony", "speech", "press",
    "interview", "zelensky", "trump", "obama", "biden", "royal",
    "parliament", "D-Day", "anniversary of", "summit",
)
TV_MOMENT_KEYWORDS = (
    "the voice", "got talent", "american idol", "x factor", "jimmy",
    "fallon", "kimmel", "snl", "oscars", "grammys", "talk show",
)
BROADCAST_SPORT_KEYWORDS = (
    "nba", "nfl", "ufc", "olympics", "world cup", "premier league",
    "goal", "knockout",
)
ARCHIVAL_KEYWORDS = (
    "colorized", "footage from", "rare footage", "100 years ago",
)
# Fallback ONLY — a clip is labeled meme only when it looks explicitly meme-ish
# and nothing riskier or more specific matched.
MEME_KEYWORDS = ("meme", "pov", "relatable")

# Archival also fires on an explicit 18xx/19xx year appearing in the text.
_ARCHIVAL_YEAR_RE = re.compile(r"\b(1[89]\d{2})\b")

# Categories in RISK PRIORITY order — the first one with a hit wins.
_CATEGORIES = (
    ("news_broadcast", NEWS_BROADCAST_KEYWORDS),
    ("tv_moment", TV_MOMENT_KEYWORDS),
    ("broadcast_sport", BROADCAST_SPORT_KEYWORDS),
    ("archival", ARCHIVAL_KEYWORDS),
    ("meme", MEME_KEYWORDS),
)

_NO_HIT = "needs manual review"

# Display / summary ordering: risky labels first, manual-review last.
_DISPLAY_ORDER = (
    "news_broadcast", "tv_moment", "broadcast_sport", "archival", "meme", _NO_HIT,
)


def _compile(keywords: tuple[str, ...]) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
        for kw in keywords
    ]


_COMPILED_CATEGORIES = [(label, _compile(kws)) for label, kws in _CATEGORIES]


def classify(text: str) -> tuple[str, str] | None:
    """Return ``(label, matched_keyword)`` for the highest-priority keyword hit,
    or ``None`` when nothing matched. Risky categories are checked first, so a
    risky match always wins over archival/meme."""
    if not text:
        return None
    for label, compiled in _COMPILED_CATEGORIES:
        for keyword, pattern in compiled:
            if pattern.search(text):
                return label, keyword
        if label == "archival":
            year = _ARCHIVAL_YEAR_RE.search(text)
            if year is not None:
                return "archival", year.group(1)
    return None


def _sweep_rows(session, assigned_only: bool) -> list[tuple[PoolItem, str | None]]:
    """Accepted pool items with a NULL rights label, joined to their asset's
    shortcode. Optionally limited to items that already have an assignment."""
    rows = (
        session.query(PoolItem, MediaAsset.source_shortcode)
        .join(MediaAsset, MediaAsset.id == PoolItem.media_asset_id)
        .filter(PoolItem.acceptance_status == "accepted")
        .filter(PoolItem.rights_confidence.is_(None))
        .order_by(PoolItem.id.asc())
        .all()
    )
    if assigned_only:
        assigned = {pid for (pid,) in session.query(Assignment.pool_item_id).distinct().all()}
        rows = [(item, shortcode) for (item, shortcode) in rows if item.id in assigned]
    return rows


def _meta_by_shortcode(session, shortcodes: set[str]) -> dict[str, tuple[str, str]]:
    """Map each shortcode to ``(search_text, title)`` from its candidate (first
    candidate wins). ``search_text`` joins title/description/channel_name for the
    keyword scan; ``title`` alone drives the display column. Chunked to stay under
    SQLite's bind-variable cap."""
    meta: dict[str, tuple[str, str]] = {}
    ordered = list(shortcodes)
    for start in range(0, len(ordered), _IN_CHUNK):
        chunk = ordered[start : start + _IN_CHUNK]
        rows = (
            session.query(
                ScrapeCandidate.video_id,
                ScrapeCandidate.title,
                ScrapeCandidate.description,
                ScrapeCandidate.channel_name,
            )
            .filter(ScrapeCandidate.video_id.in_(chunk))
            .order_by(ScrapeCandidate.id.asc())
            .all()
        )
        for video_id, title, description, channel_name in rows:
            if video_id in meta:
                continue
            search_text = " ".join(
                part for part in (title, description, channel_name) if part
            )
            meta[video_id] = (search_text, (title or "").strip())
    return meta


def _classify_rows(rows, meta_by_shortcode) -> list[dict]:
    results: list[dict] = []
    for item, shortcode in rows:
        search_text, title = meta_by_shortcode.get(shortcode, ("", "")) if shortcode else ("", "")
        hit = classify(search_text)
        results.append(
            {
                "pool_item_id": item.id,
                "niche": item.niche,
                "label": hit[0] if hit else _NO_HIT,
                "keyword": hit[1] if hit else "-",
                "title": title[:60],
            }
        )
    return results


def _print_table(results: list[dict]) -> None:
    rank = {label: i for i, label in enumerate(_DISPLAY_ORDER)}
    ordered = sorted(results, key=lambda r: (rank.get(r["label"], 99), r["pool_item_id"]))
    print(
        f"  {'id':>6}  {'niche':<8} {'suggested_label':<19} "
        f"{'matched_keyword':<16} title"
    )
    print(f"  {'-' * 6}  {'-' * 8} {'-' * 19} {'-' * 16} {'-' * 40}")
    for r in ordered:
        title = (r["title"] or "").replace("\n", " ")[:48]
        print(
            f"  {r['pool_item_id']:>6}  {r['niche']:<8} {r['label']:<19} "
            f"{r['keyword']:<16} {title}"
        )


def _print_summary(results: list[dict]) -> None:
    counts = Counter(r["label"] for r in results)
    print("\n=== Summary (suggested labels) ===")
    for label in _DISPLAY_ORDER:
        if counts.get(label):
            print(f"  {label:<19} {counts[label]}")
    print(f"  {'TOTAL':<19} {len(results)}")


def _apply(results: list[dict]) -> int:
    """Write every non-manual suggestion in ONE transaction. Returns the count."""
    applied = 0
    with get_session() as session:
        for r in results:
            if r["label"] == _NO_HIT:
                continue
            pools.set_pool_item_rights_confidence(
                session, pool_item_id=r["pool_item_id"], rights_confidence=r["label"]
            )
            applied += 1
        session.commit()
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--assigned-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only sweep accepted+unlabeled items that already have an assignment "
        "row (default: on). Use --no-assigned-only to sweep the whole backlog.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the suggested labels (one transaction). Default is a dry run.",
    )
    args = parser.parse_args()

    init_db()
    with get_session() as session:
        rows = _sweep_rows(session, assigned_only=args.assigned_only)
        shortcodes = {shortcode for (_item, shortcode) in rows if shortcode}
        meta_by_shortcode = _meta_by_shortcode(session, shortcodes)
        results = _classify_rows(rows, meta_by_shortcode)

    scope = "assigned-only" if args.assigned_only else "all accepted+unlabeled"
    print(f"=== Rights-label sweep — {len(results)} pool item(s) [{scope}] ===")
    if not results:
        print("Nothing to sweep: no accepted pool items with a NULL rights label.")
        return

    _print_table(results)
    _print_summary(results)

    if args.apply:
        applied = _apply(results)
        print(f"\nApplied {applied} suggested label(s). Now review the risky subset "
              f"(news_broadcast / tv_moment / broadcast_sport) in the Pool Review tab.")
    else:
        suggestable = sum(1 for r in results if r["label"] != _NO_HIT)
        print(f"\nDry run — nothing written. {suggestable} item(s) would be labeled; "
              f"re-run with --apply to write them.")


if __name__ == "__main__":
    main()
