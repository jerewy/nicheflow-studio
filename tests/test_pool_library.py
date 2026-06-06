from __future__ import annotations

import datetime as dt

from nicheflow_studio.db.media_library import find_or_register_media_asset
from nicheflow_studio.db.models import Account, PoolItem, ScrapeCandidate
from nicheflow_studio.db.pools import (
    accept_into_pool,
    move_pool_item_niche,
    pool_clips_for_source,
    pool_source_summary,
)
from nicheflow_studio.db.session import get_session, init_db


def _utc(y, m, d):
    return dt.datetime(y, m, d, 12, 0, tzinfo=dt.timezone.utc)


def _source_account(session) -> Account:
    """A throwaway account to own the scrape candidates (account_id is NOT NULL)."""
    account = Account(name="src-acct", platform="instagram")
    session.add(account)
    session.flush()
    return account


def _pool_clip(session, niche, shortcode, *, channel, published, likes=None, account=None):
    """Register a downloaded asset, accept it into the niche pool, and attach a
    scrape candidate carrying the source metadata."""
    account = account or _source_account(session)
    asset, _ = find_or_register_media_asset(
        session,
        source_url=f"https://www.instagram.com/reel/{shortcode}/",
        shortcode=shortcode,
        platform="instagram",
    )
    asset.download_status = "downloaded"
    session.add(
        ScrapeCandidate(
            account_id=account.id,
            scrape_source_url=f"https://www.instagram.com/{channel}/",
            source_url=f"https://www.instagram.com/reel/{shortcode}/",
            video_id=shortcode,
            channel_name=channel,
            description=f"caption for {shortcode}",
            like_count=likes,
            published_at=published,
        )
    )
    session.flush()
    return accept_into_pool(session, media_asset=asset, niche=niche)


def test_source_summary_counts_and_newest_date():
    init_db()
    with get_session() as session:
        _pool_clip(session, "history", "A1", channel="crazyfacts", published=_utc(2026, 6, 1))
        _pool_clip(session, "history", "A2", channel="crazyfacts", published=_utc(2026, 6, 3))
        _pool_clip(session, "history", "B1", channel="theanomalists", published=_utc(2026, 5, 9))

        rows = pool_source_summary(session, "history")
        by_source = {r.source_label: r for r in rows}
        assert by_source["crazyfacts"].clip_count == 2
        assert by_source["crazyfacts"].newest_post_at.replace(tzinfo=None) == dt.datetime(2026, 6, 3, 12, 0)
        assert by_source["theanomalists"].clip_count == 1
        # Busiest source first.
        assert rows[0].source_label == "crazyfacts"


def test_clips_for_source_are_scoped_and_newest_first():
    init_db()
    with get_session() as session:
        _pool_clip(session, "history", "A1", channel="crazyfacts", published=_utc(2026, 6, 1), likes=10)
        _pool_clip(session, "history", "A2", channel="crazyfacts", published=_utc(2026, 6, 3), likes=99)
        _pool_clip(session, "history", "B1", channel="theanomalists", published=_utc(2026, 6, 2))

        clips = pool_clips_for_source(session, "history", "crazyfacts")
        assert [c.shortcode for c in clips] == ["A2", "A1"]  # newest post first
        assert clips[0].like_count == 99
        assert "/reel/A2" in clips[0].source_url
        assert clips[0].caption == "caption for A2"
        # Other source's clip is excluded.
        assert all(c.shortcode != "B1" for c in clips)


def test_move_pool_item_niche():
    init_db()
    with get_session() as session:
        item = _pool_clip(session, "history", "A1", channel="crazyfacts", published=_utc(2026, 6, 1))
        item_id = item.id

        assert move_pool_item_niche(session, pool_item_id=item_id, target_niche="movie") is True
        assert session.get(PoolItem, item_id).niche == "movie"
        # It now shows under movie, not history.
        assert pool_source_summary(session, "history") == []
        assert pool_source_summary(session, "movie")[0].source_label == "crazyfacts"


def test_move_pool_item_niche_missing_and_invalid():
    init_db()
    with get_session() as session:
        assert move_pool_item_niche(session, pool_item_id=999999, target_niche="movie") is False
        item = _pool_clip(session, "history", "A1", channel="crazyfacts", published=_utc(2026, 6, 1))
        try:
            move_pool_item_niche(session, pool_item_id=item.id, target_niche="sports")
            raise AssertionError("expected ValueError for invalid niche")
        except ValueError:
            pass
