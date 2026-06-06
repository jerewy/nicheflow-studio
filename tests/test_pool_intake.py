from __future__ import annotations

import datetime as dt

from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.pool_intake import ReelMetadata, add_reel_to_pool
from nicheflow_studio.db.pools import pool_size
from nicheflow_studio.db.session import get_session, init_db


def _history_account(session) -> Account:
    account = Account(name="Past Moments Daily", platform="instagram", niche="history")
    session.add(account)
    session.flush()
    return account


def _meta(shortcode: str) -> ReelMetadata:
    return ReelMetadata(
        source_url=f"https://www.instagram.com/reel/{shortcode}/",
        shortcode=shortcode,
        channel_name="insidehistory",
        description="a caption",
        published_at=dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc),
        like_count=31177,
    )


def test_add_reel_creates_pool_item_and_saves_metadata():
    init_db()
    with get_session() as session:
        _history_account(session)
        result = add_reel_to_pool(session, niche="history", metadata=_meta("DZIAtSVCqCS"))
        session.commit()
        assert result.status == "added"
        assert pool_size(session, "history") == 1
        candidate = (
            session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.video_id == "DZIAtSVCqCS")
            .one()
        )
        assert candidate.channel_name == "insidehistory"
        assert candidate.like_count == 31177


def test_adding_same_reel_twice_is_deduped():
    init_db()
    with get_session() as session:
        _history_account(session)
        add_reel_to_pool(session, niche="history", metadata=_meta("DUPE01"))
        session.commit()
        result = add_reel_to_pool(session, niche="history", metadata=_meta("DUPE01"))
        session.commit()
        assert result.status == "duplicate"
        assert "skipped" in result.message.lower()
        assert pool_size(session, "history") == 1  # not double-pooled


def test_reel_already_in_other_niche_is_reported_duplicate():
    init_db()
    with get_session() as session:
        session.add(Account(name="hist", platform="instagram", niche="history"))
        session.add(Account(name="mov", platform="instagram", niche="movie"))
        session.flush()
        add_reel_to_pool(session, niche="history", metadata=_meta("CROSS1"))
        session.commit()
        result = add_reel_to_pool(session, niche="movie", metadata=_meta("CROSS1"))
        session.commit()
        assert result.status == "duplicate"
        assert result.niche == "history"  # reports where it already lives
        assert pool_size(session, "movie") == 0


def test_no_account_for_niche_is_reported():
    init_db()
    with get_session() as session:
        # No history account exists.
        result = add_reel_to_pool(session, niche="history", metadata=_meta("NOACCT"))
        assert result.status == "no_account"
        assert pool_size(session, "history") == 0
