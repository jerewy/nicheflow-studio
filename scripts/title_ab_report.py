"""Compare Short-title accounts against Auto-title accounts, before vs after a start date.

Read-only against the local database. Run ``scripts/ig_insights.py <account>`` for
every account first so the metrics are current.

The comparison is a difference-in-differences: each group is measured before and
after the switch, and the question is whether the Short group improved MORE than
the control group did. That cancels out network-wide swings (a good or bad week
for every account) that a plain before/after would mistake for the title effect.

Posts younger than ``--mature-days`` at pull time are skipped, because a reel
keeps collecting views for a few days and a fresh one would look like a flop.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sqlite3
import statistics
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nicheflow_studio.core.paths import data_dir  # noqa: E402
from nicheflow_studio.processing import draft_guard  # noqa: E402

# vaultedhistory joined the Short arm on 2026-09-12: its own history could not
# settle a band (only 18 short posts, and its best measured band was over 130
# characters, which the render ceiling no longer allows), so it collects a clean
# short sample instead of sitting in a control it cannot inform.
DEFAULT_SHORT = ("beneathhistory", "theoddhistoryclub", "vaultedhistory")
DEFAULT_CONTROL = ("pastmomentsdaily", "resurfacedhistory")
HIT_VIEWS = 100_000
# Matching key length: long enough to be unique per post, short enough to survive
# Instagram trimming or re-wrapping the end of a caption.
CAPTION_KEY_CHARS = 50


def _norm(text: str | None) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def _load_posts(conn: sqlite3.Connection, mature_days: int) -> list[dict]:
    """Posted reels joined to their metrics by account + caption text.

    upload_jobs.posted_media_id is empty for most posts, so the join matches the
    Instagram caption against the job's description (or title) instead. The
    maturity cutoff is per account: accounts are pulled one at a time, and a
    single network-wide cutoff would count posts an account's older pull never
    saw as missing.
    """
    mature_before = {
        account: dt.datetime.fromisoformat(pulled[:19]) - dt.timedelta(days=mature_days)
        for account, pulled in conn.execute(
            "select account_key, max(pulled_at) from account_post_metrics group by account_key"
        )
    }
    metrics: dict[str, list[tuple[str, dict]]] = {}
    for account, caption, views, shares, saved, timestamp in conn.execute(
        "select account_key, caption, views, shares, saved, timestamp from account_post_metrics"
    ):
        metrics.setdefault(account, []).append(
            (_norm(caption), {"views": views or 0, "shares": shares or 0,
                              "saved": saved or 0, "timestamp": timestamp})
        )
    posts = []
    for handle, posted_at, title, description in conn.execute(
        """select a.instagram_handle, u.posted_at, u.title, u.description
           from upload_jobs u join accounts a on a.id = u.account_id
           where u.posted_at is not null"""
    ):
        cutoff = mature_before.get(handle)
        if cutoff is None or dt.datetime.fromisoformat(posted_at[:19]) > cutoff:
            continue
        keys = [k[:CAPTION_KEY_CHARS] for k in (_norm(description), _norm(title)) if len(k) >= 20]
        match = next(
            (m for cap, m in metrics.get(handle, []) if any(k in cap for k in keys)), None
        )
        if match is None:
            continue
        posts.append({"account": handle, "posted_at": posted_at[:10],
                      "title_len": len(title or ""), **match})
    return posts


def _summary(posts: list[dict]) -> dict | None:
    if not posts:
        return None
    views = [p["views"] for p in posts]
    total = sum(views) or 1
    return {
        "n": len(posts),
        "title_len": int(statistics.median(p["title_len"] for p in posts)),
        "median_views": int(statistics.median(views)),
        "shares_per_1k": 1000 * sum(p["shares"] for p in posts) / total,
        "saves_per_1k": 1000 * sum(p["saved"] for p in posts) / total,
        "hit_rate": 100 * sum(v >= HIT_VIEWS for v in views) / len(views),
    }


def _row(label: str, s: dict | None) -> str:
    if s is None:
        return f"  {label:8s} no matched posts"
    return (
        f"  {label:8s} n={s['n']:4d}  title_len={s['title_len']:4d}  "
        f"median_views={s['median_views']:7d}  shares/1k={s['shares_per_1k']:5.1f}  "
        f"saves/1k={s['saves_per_1k']:5.1f}  100k+={s['hit_rate']:5.1f}%"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2026-09-11", help="switch date, YYYY-MM-DD")
    parser.add_argument("--baseline-days", type=int, default=28,
                        help="days before --start used as the 'before' window")
    parser.add_argument("--mature-days", type=int, default=3,
                        help="skip posts younger than this at pull time")
    parser.add_argument("--short", nargs="+", default=list(DEFAULT_SHORT))
    parser.add_argument("--control", nargs="+", default=list(DEFAULT_CONTROL))
    args = parser.parse_args(argv)

    start = dt.date.fromisoformat(args.start)
    baseline_from = (start - dt.timedelta(days=args.baseline_days)).isoformat()
    conn = sqlite3.connect(data_dir() / "nicheflow.db")
    pulls = dict(conn.execute(
        "select account_key, max(pulled_at) from account_post_metrics group by account_key"
    ))
    if not pulls:
        print("No insights in the database. Run scripts/ig_insights.py first.", file=sys.stderr)
        return 2
    posts = _load_posts(conn, args.mature_days)

    print("Last insights pull per account (UTC):")
    for account in (*args.short, *args.control):
        print(f"  {account:18s} {(pulls.get(account) or 'never')[:16]}")
    print(f"Posts younger than {args.mature_days} days at their account's pull are skipped.")
    print(f"Before: {baseline_from} to {start - dt.timedelta(days=1)}   After: {start} onward\n")
    results = {}
    for group, accounts in (("SHORT", args.short), ("CONTROL", args.control)):
        print(f"{group} ({', '.join(accounts)})")
        mine = [p for p in posts if p["account"] in accounts]
        before = _summary([p for p in mine if baseline_from <= p["posted_at"] < args.start])
        after = _summary([p for p in mine if p["posted_at"] >= args.start])
        print(_row("before", before))
        print(_row("after", after))
        results[group] = (before, after)
        print()

    (sb, sa), (cb, ca) = results["SHORT"], results["CONTROL"]
    if not all((sb, sa, cb, ca)):
        print("Not enough matured posts in every window yet; re-run after more days.")
        return 0
    print("Change after minus before (SHORT change vs CONTROL change):")
    for key, label in (("shares_per_1k", "shares/1k"), ("hit_rate", "100k+ hit rate"),
                       ("median_views", "median views")):
        short_change = sa[key] - sb[key]
        control_change = ca[key] - cb[key]
        print(f"  {label:15s} short {short_change:+9.1f}   control {control_change:+9.1f}   "
              f"difference {short_change - control_change:+9.1f}")
    if sa["title_len"] > draft_guard.SHORT_TITLE_MAX_CHARS:
        print(
            f"\nWarning: the SHORT group median title is still over "
            f"{draft_guard.SHORT_TITLE_MAX_CHARS} characters, so the short setting "
            "may not be reaching the posts yet."
        )
    if min(sa["n"], ca["n"]) < 40:
        print("\nWarning: fewer than 40 matured posts in an 'after' window; "
              "treat the difference as a hint, not a result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
