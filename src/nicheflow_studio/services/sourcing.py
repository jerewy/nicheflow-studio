"""Per-account scraping / source management (UI-independent).

Backs the migrated Scraping tab, which is scoped to the *active account*: manage
that account's source profiles and review its scrape candidates. Sources, scrape
runs, and candidates are per-account in the schema (``Source.account_id`` etc.),
so this stays naturally isolated to one account.

Scope: manage + review only. Running the actual Apify scrape, queuing a candidate
for download, and accepting a candidate into the shared niche pool remain in the
desktop app (heavy/external or cross-account pool mutations). The review actions
here are limited to the safe, reversible ``ignored`` <-> ``candidate`` toggle.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from nicheflow_studio.db.models import Account, ScrapeCandidate, ScrapeRun, Source
from nicheflow_studio.db.session import get_session
from nicheflow_studio.db.sources import find_or_create_source
from nicheflow_studio.services.errors import ServiceError

_CANDIDATE_LIMIT = 200
# Reversible review states the migrated screen may set. Accept-into-pool and
# queue-for-download intentionally stay in the desktop app.
_REVIEW_STATES = {"candidate", "ignored"}


class SourcingError(ServiceError):
    """Raised for invalid source/candidate operations."""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _normalize_url(raw: str) -> str:
    return (raw or "").strip().rstrip("/")


def _infer_type_and_label(url: str) -> tuple[str, str]:
    lowered = url.lower()
    if "instagram.com/explore/tags/" in lowered:
        tag = url.rsplit("/", 1)[-1]
        return "instagram_hashtag", f"#{tag}"
    if "instagram.com/" in lowered:
        handle = url.rsplit("/", 1)[-1]
        return "instagram_profile", f"@{handle}"
    return "profile", url.rsplit("/", 1)[-1] or url


def _normalize_state(state: str | None) -> str:
    return "candidate" if (state or "") in ("", "new") else state


def _source_view(source: Source) -> dict:
    return {
        "id": source.id,
        "label": source.label,
        "source_url": source.source_url,
        "source_type": source.source_type,
        "platform": source.platform,
        "enabled": bool(source.enabled),
        "priority": source.priority,
        "last_scraped_at": _iso(source.last_scraped_at),
        "last_run_status": source.last_run_status,
        "last_error_summary": source.last_error_summary,
    }


def _require_account(session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise SourcingError(f"No account with id {account_id}.")
    return account


def list_sources(account_id: int) -> list[dict]:
    """Source profiles for an account, by priority then id."""
    with get_session() as session:
        _require_account(session, account_id)
        rows = session.scalars(
            select(Source)
            .where(Source.account_id == account_id)
            .order_by(Source.priority.asc(), Source.id.asc())
        ).all()
        return [_source_view(row) for row in rows]


def add_source(account_id: int, source_url: str) -> dict:
    """Add a source profile/hashtag URL to an account (idempotent per URL)."""
    url = _normalize_url(source_url)
    if not url:
        raise SourcingError("Source URL is required.")
    with get_session() as session:
        account = _require_account(session, account_id)
        source_type, label = _infer_type_and_label(url)
        source = find_or_create_source(
            session,
            account=account,
            source_url=url,
            label=label,
            platform=account.platform or "instagram",
            source_type=source_type,
        )
        session.commit()
        return _source_view(source)


def set_source_enabled(source_id: int, enabled: bool) -> dict:
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise SourcingError(f"No source with id {source_id}.")
        source.enabled = 1 if enabled else 0
        session.commit()
        return _source_view(source)


def remove_source(source_id: int) -> dict:
    """Delete a source, detaching its candidates and removing its scrape runs."""
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise SourcingError(f"No source with id {source_id}.")
        for candidate in session.scalars(
            select(ScrapeCandidate).where(ScrapeCandidate.source_id == source_id)
        ).all():
            candidate.source_id = None
        for run in session.scalars(select(ScrapeRun).where(ScrapeRun.source_id == source_id)).all():
            session.delete(run)
        session.delete(source)
        session.commit()
        return {"removed_source_id": source_id}


def list_candidates(
    account_id: int, state: str = "all", limit: int = _CANDIDATE_LIMIT
) -> list[dict]:
    """Scrape candidates for an account's review queue, newest first."""
    with get_session() as session:
        _require_account(session, account_id)
        rows = session.scalars(
            select(ScrapeCandidate)
            .where(ScrapeCandidate.account_id == account_id)
            .order_by(ScrapeCandidate.id.desc())
            .limit(limit)
        ).all()
        result = []
        for row in rows:
            normalized = _normalize_state(row.state)
            if state != "all" and normalized != state:
                continue
            result.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "source_url": row.source_url,
                    "channel_name": row.channel_name,
                    "state": normalized,
                    "like_count": row.like_count,
                    "view_count": row.view_count,
                    "published_at": _iso(row.published_at),
                    "thumbnail_url": row.thumbnail_url,
                }
            )
        return result


def set_candidate_state(candidate_id: int, state: str) -> dict:
    """Toggle a candidate between ``candidate`` (ready to review) and ``ignored``.

    Other transitions (queue for download, accept into pool) are intentionally
    not exposed here; do those in the desktop app.
    """
    if state not in _REVIEW_STATES:
        raise SourcingError(
            "Only ignore/restore are available here. Queue a download or accept "
            "into the pool from the desktop app."
        )
    with get_session() as session:
        candidate = session.get(ScrapeCandidate, candidate_id)
        if candidate is None:
            raise SourcingError(f"No candidate with id {candidate_id}.")
        candidate.state = state
        session.commit()
        return {"candidate_id": candidate_id, "state": state}
