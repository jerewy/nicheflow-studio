from __future__ import annotations

import datetime as dt

from nicheflow_studio.db.models import Account, ScrapeCandidate, Source
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.db.sources import advance_source_newest_date

PROFILE = "https://www.instagram.com/insidehistory/"


def _add_candidate(session, account_id: int, shortcode: str, published_at: dt.datetime | None):
    candidate = ScrapeCandidate(
        account_id=account_id,
        scrape_source_url=PROFILE,
        source_url=f"https://www.instagram.com/reel/{shortcode}/",
        video_id=shortcode,
        published_at=published_at,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _utc(y, m, d):
    return dt.datetime(y, m, d, 12, 0, tzinfo=dt.timezone.utc)


def _naive(value: dt.datetime | None) -> dt.datetime | None:
    # SQLite stores DateTime tz-naive; normalize both sides before comparing so
    # the assertion checks the wall-clock value, not the tzinfo representation.
    return value.replace(tzinfo=None) if value is not None else None


def test_cursor_set_to_newest_published_at():
    init_db()
    with get_session() as session:
        account = Account(name="insidehistory", platform="instagram")
        session.add(account)
        session.flush()
        _add_candidate(session, account.id, "OLD", _utc(2026, 6, 1))
        _add_candidate(session, account.id, "NEWEST", _utc(2026, 6, 3))
        _add_candidate(session, account.id, "MIDDLE", _utc(2026, 6, 2))

        source = advance_source_newest_date(
            session, account=account, source_url=PROFILE, label="insidehistory"
        )
        # Cursor is the newest VIDEO date (June 3), not "now".
        assert _naive(source.last_scraped_at) == _naive(_utc(2026, 6, 3))
        assert source.last_seen_external_id == "NEWEST"


def test_cursor_only_moves_forward():
    init_db()
    with get_session() as session:
        account = Account(name="insidehistory", platform="instagram")
        session.add(account)
        session.flush()
        _add_candidate(session, account.id, "NEW", _utc(2026, 6, 3))
        advance_source_newest_date(session, account=account, source_url=PROFILE)

        # A later MANUAL import of an OLDER clip must not rewind the cursor.
        _add_candidate(session, account.id, "ANCIENT", _utc(2025, 1, 1))
        source = advance_source_newest_date(session, account=account, source_url=PROFILE)
        assert _naive(source.last_scraped_at) == _naive(_utc(2026, 6, 3))
        assert source.last_seen_external_id == "NEW"


def test_source_is_reused_not_duplicated():
    init_db()
    with get_session() as session:
        account = Account(name="insidehistory", platform="instagram")
        session.add(account)
        session.flush()
        _add_candidate(session, account.id, "A", _utc(2026, 6, 3))
        advance_source_newest_date(session, account=account, source_url=PROFILE)
        advance_source_newest_date(session, account=account, source_url=PROFILE)
        sources = session.query(Source).filter(Source.account_id == account.id).all()
        assert len(sources) == 1


def test_no_candidates_with_dates_leaves_cursor_none():
    init_db()
    with get_session() as session:
        account = Account(name="insidehistory", platform="instagram")
        session.add(account)
        session.flush()
        _add_candidate(session, account.id, "NODATE", None)
        source = advance_source_newest_date(session, account=account, source_url=PROFILE)
        assert source.last_scraped_at is None
        assert source.last_seen_external_id is None
