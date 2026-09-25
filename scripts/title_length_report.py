"""Report title-length bands: what the accounts posted, and what it earned.

Two halves, because "are we writing the right lengths" and "which length wins"
are different questions:

    history   per account, every posted reel grouped into length bands with its
              measured views, shares and 100k+ hit rate. This is the evidence
              behind an account's Title Length setting.
    batch     per account, the length bands of the options in the newest draft
              revisions and of the option each one recommends. This is how you
              catch a batch that quietly came back all-long.

Read-only. Run scripts/ig_insights.py first so the measured half is current.

Hit rate, not median views, is the number to read: a length that produces the
occasional 100k reel matters more than one that reliably produces 4k.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import statistics
import sys
from collections import defaultdict

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nicheflow_studio.core.paths import data_dir  # noqa: E402

HIT_VIEWS = 100_000
# Same bands the Title Length dropdown implies, plus the over-130 bucket that
# only exists because older prompts had no ceiling.
BANDS = (("short <=60", 0, 60), ("medium 61-100", 61, 100), ("long 101-130", 101, 130),
         ("over 130", 131, 10_000))
# Below this a band's hit rate is noise, so the recommendation says so instead
# of naming a winner.
MIN_BAND_SAMPLE = 25
CAPTION_KEY_CHARS = 50


def _norm(text: str | None) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def _band(length: int) -> str:
    return next(name for name, lo, hi in BANDS if lo <= length <= hi)


def _posted_with_metrics(conn: sqlite3.Connection, mature_days: int, since: str) -> list[dict]:
    """Posted reels joined to their insights by account + caption text.

    posted_media_id is empty on most rows, so the join matches the Instagram
    caption against the job's description. The maturity cutoff is per account
    because insights are pulled one account at a time.
    """
    metrics: dict[str, list[tuple[str, tuple[int, int, int]]]] = defaultdict(list)
    for account, caption, views, shares, saved in conn.execute(
        "select account_key, caption, views, shares, saved from account_post_metrics"
    ):
        metrics[account].append((_norm(caption), (views or 0, shares or 0, saved or 0)))
    pulls = dict(conn.execute(
        "select account_key, max(pulled_at) from account_post_metrics group by account_key"
    ))
    posts: list[dict] = []
    for handle, posted_at, title, description in conn.execute(
        """select a.instagram_handle, u.posted_at, u.title, u.description
           from upload_jobs u join accounts a on a.id = u.account_id
           where u.posted_at is not null and a.niche is not null"""
    ):
        pulled = pulls.get(handle)
        if not pulled or posted_at[:10] < since:
            continue
        cutoff = dt.datetime.fromisoformat(pulled[:19]) - dt.timedelta(days=mature_days)
        if dt.datetime.fromisoformat(posted_at[:19]) > cutoff:
            continue
        keys = [k[:CAPTION_KEY_CHARS] for k in (_norm(description), _norm(title)) if len(k) >= 20]
        match = next((m for cap, m in metrics.get(handle, []) if any(k in cap for k in keys)), None)
        if match is None:
            continue
        views, shares, saved = match
        posts.append({"account": handle, "length": len(title or ""), "views": views,
                      "shares": shares, "saved": saved})
    return posts


def _row(label: str, posts: list[dict]) -> tuple[str, float | None]:
    views = sorted(p["views"] for p in posts)
    total = sum(views) or 1
    hit = 100 * sum(v >= HIT_VIEWS for v in views) / len(views)
    thin = "" if len(posts) >= MIN_BAND_SAMPLE else "  (thin sample)"
    line = (f"  {label:15s} n={len(posts):4d}  median_views={int(statistics.median(views)):6d}  "
            f"shares/1k={1000 * sum(p['shares'] for p in posts) / total:5.1f}  "
            f"100k+={hit:5.1f}%{thin}")
    return line, (hit if len(posts) >= MIN_BAND_SAMPLE else None)


def cmd_history(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    since = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    posts = _posted_with_metrics(conn, args.mature_days, since)
    if not posts:
        print("No matured posts with insights. Run scripts/ig_insights.py first.")
        return
    print(f"Posted reels since {since}, by title length band\n")
    for account in sorted({p["account"] for p in posts}):
        mine = [p for p in posts if p["account"] == account]
        print(f"{account} (n={len(mine)})")
        scored: dict[str, float] = {}
        for name, lo, hi in BANDS:
            band_posts = [p for p in mine if lo <= p["length"] <= hi]
            if not band_posts:
                continue
            line, hit = _row(name, band_posts)
            print(line)
            if hit is not None:
                scored[name] = hit
        if scored:
            best = max(scored, key=scored.get)
            runner = sorted(scored.values(), reverse=True)
            margin = runner[0] - (runner[1] if len(runner) > 1 else 0)
            verdict = "clear" if margin >= 3 else "close, keep the current setting"
            print(f"  -> best measured band: {best} ({scored[best]:.1f}%), {verdict}")
        else:
            print("  -> every band is under the minimum sample; no recommendation")
        print()


def cmd_batch(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    # An account whose Title Length is capped is SUPPOSED to land in one band,
    # so the spread warning below must not fire for it.
    capped = {
        handle for handle, prefs in conn.execute(
            "select instagram_handle, processing_preferences from accounts")
        if prefs and '"title_length": "short"' in prefs
    }
    rows = conn.execute("""
        select a.instagram_handle, r.title_options, r.recommended_title_index, r.created_at
        from draft_revisions r
        join download_items d on d.id = r.download_item_id
        join accounts a on a.id = d.account_id
        where r.id in (select max(id) from draft_revisions group by download_item_id)
          and r.created_at >= ?
        order by a.instagram_handle""", (args.since,))
    per: dict[str, dict] = defaultdict(lambda: {"options": defaultdict(int), "picked": defaultdict(int),
                                                "lengths": []})
    for handle, titles_json, recommended, _created in rows:
        titles = json.loads(titles_json or "[]")
        if not titles:
            continue
        for title in titles:
            per[handle]["options"][_band(len(title))] += 1
        picked = titles[min(max((recommended or 1) - 1, 0), len(titles) - 1)]
        per[handle]["picked"][_band(len(picked))] += 1
        per[handle]["lengths"].append(len(picked))
    if not per:
        print(f"No draft revisions created since {args.since}.")
        return
    print(f"Draft revisions created since {args.since}\n")
    for handle, data in sorted(per.items()):
        print(f"{handle} ({len(data['lengths'])} reels)")
        print(f"  options offered: {dict(data['options'])}")
        print(f"  options picked:  {dict(data['picked'])}  "
              f"median picked length {int(statistics.median(data['lengths']))}")
        if len(data["picked"]) == 1 and handle not in capped:
            only = next(iter(data["picked"]))
            print(f"  -> every pick landed in one band ({only}); the batch offered no real choice")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_history = sub.add_parser("history", help="Measured performance per length band, per account.")
    p_history.add_argument("--days", type=int, default=120, help="how far back to look")
    p_history.add_argument("--mature-days", type=int, default=3,
                           help="skip posts younger than this at their account's insights pull")
    p_batch = sub.add_parser("batch", help="Length bands in recent draft revisions.")
    p_batch.add_argument("--since", default=dt.date.today().isoformat(),
                         help="only revisions created on or after this date")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(data_dir() / "nicheflow.db")
    {"history": cmd_history, "batch": cmd_batch}[args.command](conn, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
