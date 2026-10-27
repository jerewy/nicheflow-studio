"""Creator baselines: each source creator's normal views, for judging captured clips.

Lookup order for a creator:
1. A saved baseline younger than BASELINE_MAX_AGE.
2. Apify: their latest posts that are old enough to have settled, one run for
   every creator still missing (only when ``fetch_missing``; capture fetches,
   the review screen never does).
3. Our own bulk scrapes of them, when we hold enough rows.

A creator none of these can measure has no baseline, and their clips are judged
"unknown" rather than guessed.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from nicheflow_studio.core.clip_check import STILL_RISING_DAYS, median_views
from nicheflow_studio.db.models import CreatorBaseline, ScrapeCandidate
from nicheflow_studio.scraper.instagram_apify import (
    ApifyConfigError,
    ApifyScrapeError,
    RecentReel,
    fetch_instagram_profile_views_apify,
)

logger = logging.getLogger(__name__)

# Creators' numbers drift; two weeks keeps one Apify row per creator per fortnight.
BASELINE_MAX_AGE = dt.timedelta(days=14)
# Only creators we bulk-scraped qualify. A creator we know only from hand
# captures would be judged against the winners we picked, inflating "normal".
POOL_BASELINE_MIN_ROWS = 30

SOURCE_APIFY = "apify"
SOURCE_POOL = "pool"
# Apify read the profile but found too few settled reels (a creator posting ~10
# a day), so our scrapes stand in. Counts as fresh for capture: asking Apify
# again within the window would buy the same unusable answer.
SOURCE_POOL_CHECKED = "pool_checked"


def normalize_channel(channel: str | None) -> str:
    return (channel or "").strip().lstrip("@").lower()


def resolve_baselines(
    session: Session,
    channels: Iterable[str | None],
    *,
    fetch_missing: bool = True,
    now: dt.datetime | None = None,
) -> dict[str, int]:
    """Normal views per creator (keys are normalized handles). Does not commit.

    With ``fetch_missing`` a saved "pool" baseline does not count as fresh: the
    capture path prefers a creator's current numbers and only falls back to our
    older scrapes when Apify cannot provide them ("pool_checked" records that it
    already tried).
    """
    current = now or dt.datetime.now(dt.timezone.utc)
    wanted = {normalize_channel(channel) for channel in channels} - {""}
    if not wanted:
        return {}

    saved = {
        row.channel_name: row
        for row in session.query(CreatorBaseline).filter(CreatorBaseline.channel_name.in_(wanted))
    }
    baselines: dict[str, int] = {}
    for name, row in saved.items():
        usable = row.source in (SOURCE_APIFY, SOURCE_POOL_CHECKED) or not fetch_missing
        if usable and current - _aware(row.measured_at) <= BASELINE_MAX_AGE:
            baselines[name] = row.median_views

    missing = wanted - baselines.keys()
    checked: set[str] = set()
    if missing and fetch_missing:
        fetched = _fetch_profile_views(sorted(missing))
        checked = set(fetched) & missing
        for name, reels in fetched.items():
            views = _settled_views(reels, current)
            median = median_views(views)
            if name in missing and median is not None:
                _save(session, saved, name, median, len(views), SOURCE_APIFY, current)
                baselines[name] = median

    for name in sorted(wanted - baselines.keys()):
        median, sample_size = _pool_median(session, name)
        if median is not None:
            source = SOURCE_POOL_CHECKED if name in checked else SOURCE_POOL
            _save(session, saved, name, median, sample_size, source, current)
            baselines[name] = median
    return baselines


def _settled_views(reels: list[RecentReel], now: dt.datetime) -> list[int]:
    """Views of reels old enough to have stopped climbing.

    historytrails posts ~6 reels a day, so on 2026-09-28 its latest 12 were all
    under two days old and their median (31.7k) sat at half its settled normal.
    A creator with too few settled reels falls through to our own scrapes.
    """
    cutoff = now - dt.timedelta(days=STILL_RISING_DAYS)
    return [
        reel.views
        for reel in reels
        if reel.published_at is not None and _aware(reel.published_at) <= cutoff
    ]


def _fetch_profile_views(names: list[str]) -> dict[str, list[RecentReel]]:
    # A failed baseline fetch must never fail the capture: the clip is already
    # paid for and pooled, it just gets judged on our own data or "unknown".
    try:
        return fetch_instagram_profile_views_apify(names)
    except (ApifyConfigError, ApifyScrapeError) as exc:
        logger.warning("Creator baseline fetch failed; using stored scrapes: %s", exc)
        return {}


def _pool_median(session: Session, name: str) -> tuple[int | None, int]:
    views = [
        count
        for (count,) in session.query(ScrapeCandidate.view_count).filter(
            func.lower(ScrapeCandidate.channel_name) == name,
            ScrapeCandidate.view_count.isnot(None),
            ScrapeCandidate.view_count > 0,
        )
    ]
    if len(views) < POOL_BASELINE_MIN_ROWS:
        return None, len(views)
    return int(statistics.median(views)), len(views)


def _save(
    session: Session,
    saved: dict[str, CreatorBaseline],
    name: str,
    median: int,
    sample_size: int,
    source: str,
    measured_at: dt.datetime,
) -> None:
    row = saved.get(name)
    if row is None:
        row = CreatorBaseline(channel_name=name)
        session.add(row)
        saved[name] = row
    row.median_views = median
    row.sample_size = sample_size
    row.source = source
    row.measured_at = measured_at
    session.flush()


def _aware(value: dt.datetime) -> dt.datetime:
    # SQLite hands timezone-aware columns back naive.
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
