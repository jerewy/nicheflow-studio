"""WI-4 scraping inputs: history source preset + ScrapeCandidate engagement_rate."""

from __future__ import annotations

import pytest

from nicheflow_studio.core.history_sources import (
    DEFAULT_HISTORY_SOURCE_PRESETS,
    DEFAULT_HISTORY_SOURCE_URLS,
)
from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.session import get_session, init_db


def _make_account(session) -> int:
    account = Account(name="history-src")
    session.add(account)
    session.flush()
    return account.id


def _add_candidate(session, account_id: int, **counts) -> None:
    url = "https://www.instagram.com/reel/abc123/"
    session.add(
        ScrapeCandidate(
            scrape_source_url=url,
            source_url=url,
            video_id="abc123",
            account_id=account_id,
            **counts,
        )
    )


def test_history_preset_includes_historytrails() -> None:
    handles = {preset.handle for preset in DEFAULT_HISTORY_SOURCE_PRESETS}
    assert "historytrails" in handles
    assert "https://www.instagram.com/historytrails/" in DEFAULT_HISTORY_SOURCE_URLS


def test_engagement_rate_computed_on_insert(tmp_path) -> None:
    init_db()
    with get_session() as session:
        account_id = _make_account(session)
        _add_candidate(session, account_id, view_count=1000, like_count=80, comment_count=20)
        session.commit()

        candidate = session.query(ScrapeCandidate).one()
        # (likes + comments) / views = (80 + 20) / 1000
        assert candidate.engagement_rate == pytest.approx(0.1)


def test_engagement_rate_recomputed_on_update(tmp_path) -> None:
    init_db()
    with get_session() as session:
        account_id = _make_account(session)
        _add_candidate(session, account_id, view_count=1000, like_count=80, comment_count=20)
        session.commit()

        candidate = session.query(ScrapeCandidate).one()
        candidate.like_count = 280  # (280 + 20) / 1000 = 0.3
        session.commit()
        session.refresh(candidate)
        assert candidate.engagement_rate == pytest.approx(0.3)


def test_engagement_rate_handles_missing_counts(tmp_path) -> None:
    """Missing views must not raise; ER is zero rather than NULL on a new scrape."""
    init_db()
    with get_session() as session:
        account_id = _make_account(session)
        _add_candidate(session, account_id, view_count=None, like_count=None, comment_count=None)
        session.commit()

        candidate = session.query(ScrapeCandidate).one()
        assert candidate.engagement_rate == 0.0
