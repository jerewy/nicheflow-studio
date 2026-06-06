"""Source cursor maintenance — the per-source "newest content date".

A :class:`Source`'s incremental-scrape cursor (``last_scraped_at`` /
``last_seen_external_id``) is what feeds Apify's ``onlyPostsNewerThan`` so a later
run only pulls genuinely new posts (docs/SOURCING_POOLING_PLAN.md §3). This module
advances that cursor to the *video's own publish date* — ``max(published_at)``
across the account's candidates — rather than wall-clock run time, which is the
date the user actually cares about ("the newest post we have is June 3"). It only
ever moves the cursor *forward*, so a manual single-URL import of an older clip
can never rewind it and cause the next scrape to re-pull history.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from nicheflow_studio.db.models import Account, ScrapeCandidate, Source


def find_or_create_source(
    session: Session,
    *,
    account: Account,
    source_url: str,
    label: str | None = None,
    platform: str = "instagram",
    source_type: str = "profile",
) -> Source:
    """Return the :class:`Source` for ``(account, source_url)``, creating it if
    absent. Does not commit; the caller owns the transaction."""
    source = (
        session.query(Source)
        .filter(Source.account_id == account.id, Source.source_url == source_url)
        .first()
    )
    if source is None:
        source = Source(
            account_id=account.id,
            platform=platform,
            source_type=source_type,
            label=label or (account.name or source_url),
            source_url=source_url,
        )
        session.add(source)
        session.flush()
    return source


def advance_source_newest_date(
    session: Session,
    *,
    account: Account,
    source_url: str,
    label: str | None = None,
    platform: str = "instagram",
) -> Source:
    """Advance the source's newest-content cursor to the newest ``published_at``
    among ``account``'s candidates, returning the :class:`Source`.

    Sets ``last_scraped_at`` to that newest video's *publish timestamp* (its real
    post date, e.g. 2026-06-03) and ``last_seen_external_id`` to its shortcode, so
    a later incremental scrape resumes from real content rather than the time the
    scrape happened to run. **Only moves forward** — if every candidate is older
    than the existing cursor, the cursor is left untouched. Does not commit.
    """
    source = find_or_create_source(
        session,
        account=account,
        source_url=source_url,
        label=label,
        platform=platform,
    )
    newest = (
        session.query(ScrapeCandidate)
        .filter(
            ScrapeCandidate.account_id == account.id,
            ScrapeCandidate.published_at.is_not(None),
        )
        .order_by(ScrapeCandidate.published_at.desc())
        .first()
    )
    if newest is not None and newest.published_at is not None:
        current = source.last_scraped_at
        if current is None or newest.published_at > current:
            source.last_scraped_at = newest.published_at
            source.last_seen_external_id = newest.video_id
    session.flush()
    return source
