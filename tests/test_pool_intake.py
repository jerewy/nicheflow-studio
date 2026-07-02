from __future__ import annotations

import datetime as dt

from nicheflow_studio.db.models import Account, PoolItem, ScrapeCandidate
from nicheflow_studio.db.pool_intake import ReelMetadata, add_reel_to_pool
from nicheflow_studio.db.pools import POOL_STATUS_PENDING_REVIEW, pool_size
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
        # Candidate-first accepts land in pending_review until the pool approval
        # gate promotes them (docs/SOURCING_POOLING_PLAN.md §1, §13).
        assert pool_size(session, "history", status=POOL_STATUS_PENDING_REVIEW) == 1
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
        # not double-pooled (still pending_review, same as the first accept)
        assert pool_size(session, "history", status=POOL_STATUS_PENDING_REVIEW) == 1


def test_duplicate_capture_keeps_first_pin():
    init_db()
    with get_session() as session:
        first = _history_account(session)
        second = Account(name="Another History", platform="instagram", niche="history")
        session.add(second)
        session.flush()

        add_reel_to_pool(
            session,
            niche="history",
            metadata=_meta("PINNED01"),
            pinned_account_id=first.id,
        )
        session.commit()
        result = add_reel_to_pool(
            session,
            niche="history",
            metadata=_meta("PINNED01"),
            pinned_account_id=second.id,
        )
        session.commit()

        assert result.status == "duplicate"
        assert session.query(PoolItem).one().pinned_account_id == first.id


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
