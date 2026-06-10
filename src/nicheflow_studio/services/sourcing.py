"""Per-account scraping / source management (UI-independent).

Backs the migrated Scraping tab, which is scoped to the *active account*: manage
that account's source profiles and review its scrape candidates. Sources, scrape
runs, and candidates are per-account in the schema (``Source.account_id`` etc.),
so this stays naturally isolated to one account.

Scope: manage + review only. Running the actual Apify scrape, queuing a candidate
for download, and accepting a candidate into the shared niche pool remain in the
desktop app (heavy/external or cross-account pool mutations). The review actions
here are the reversible ``ignored`` <-> ``candidate`` toggle plus a reason-tagged
reject that also pulls any pooled copy of the clip out of distribution (both
reversible from the desktop app / Pool & Distribute).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import or_, select

from nicheflow_studio.db.media_library import find_media_asset
from nicheflow_studio.db.models import Account, ScrapeCandidate, ScrapeRun, Source
from nicheflow_studio.db.pools import (
    REJECT_REASONS,
    VALID_NICHES,
    CrossNicheError,
    DuplicateContentError,
    remove_pool_items_for_asset,
)
from nicheflow_studio.db.pools import accept_candidate_into_pool as _accept_candidate_into_pool_db
from nicheflow_studio.db.pools import reject_candidate as _reject_candidate_db
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
        query = (
            select(ScrapeCandidate)
            .where(ScrapeCandidate.account_id == account_id)
            .order_by(ScrapeCandidate.id.desc())
            .limit(limit)
        )
        if state == "candidate":
            query = query.where(
                or_(
                    ScrapeCandidate.state == "candidate",
                    ScrapeCandidate.state == "new",
                    ScrapeCandidate.state == "",
                    ScrapeCandidate.state.is_(None),
                )
            )
        elif state != "all":
            query = query.where(ScrapeCandidate.state == state)
        rows = session.scalars(query).all()
        result = []
        for row in rows:
            normalized = _normalize_state(row.state)
            result.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "source_url": row.source_url,
                    "channel_name": row.channel_name,
                    "state": normalized,
                    "like_count": row.like_count,
                    "view_count": row.view_count,
                    "comment_count": row.comment_count,
                    "duration_seconds": row.duration_seconds,
                    "description": row.description,
                    "published_at": _iso(row.published_at),
                    "created_at": _iso(row.created_at),
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


def accept_candidate(candidate_id: int) -> dict:
    """Accept a candidate into its account's niche pool."""
    with get_session() as session:
        candidate = session.get(ScrapeCandidate, candidate_id)
        if candidate is None:
            raise SourcingError(f"No candidate with id {candidate_id}.")
        if candidate.account_id is None:
            raise SourcingError("Candidate is not assigned to an account.")
        account = _require_account(session, candidate.account_id)
        # Pool isolation keys off the strict niche ("history"/"movie"), not the
        # free-text niche_label shown in the UI — mirroring the desktop accept
        # path (main_window._on_candidate_accept_pool_clicked). Using niche_label
        # here sent the whole label sentence into _validate_niche and surfaced as
        # a generic "Unexpected error".
        niche = (account.niche or "").strip().lower()
        if niche not in VALID_NICHES:
            raise SourcingError(
                "This candidate's account has no pool niche (history/movie) set. "
                "Set the account's niche in account settings first."
            )
        try:
            pool_item = _accept_candidate_into_pool_db(
                session,
                candidate=candidate,
                niche=niche,
            )
        except (CrossNicheError, DuplicateContentError, ValueError) as exc:
            # Real guardrails (niche mixing / duplicate footage / bad niche);
            # surface the reason instead of a generic "Unexpected error".
            raise SourcingError(str(exc)) from exc
        session.commit()
        return {
            "candidate_id": candidate_id,
            "state": candidate.state,
            "pool_item_id": pool_item.id,
            "niche": pool_item.niche,
        }


def reject_candidate(candidate_id: int, reason: str = "low_quality") -> dict:
    """Reject a bad candidate and pull any pooled copy out of distribution.

    For clips the dedup passes missed (a near-duplicate or a low-quality clip
    that slipped through), this both flags the candidate ``rejected_<reason>``
    and marks every pool item backing its footage ``removed`` so it stops
    distributing. ``reason`` is a :data:`REJECT_REASONS` key (``ad_campaign``,
    ``duplicate``, ``wrong_niche``, ``low_quality``).

    Reversible: the candidate state and the pool item can both be restored from
    the desktop app / Pool & Distribute. Returns the stored state and the number
    of pool items removed.
    """
    key = (reason or "").strip().lower()
    if key not in REJECT_REASONS:
        raise SourcingError(
            f"Unknown reject reason {reason!r}. Use one of {sorted(REJECT_REASONS)}."
        )
    with get_session() as session:
        candidate = session.get(ScrapeCandidate, candidate_id)
        if candidate is None:
            raise SourcingError(f"No candidate with id {candidate_id}.")
        state = _reject_candidate_db(session, candidate=candidate, reason=key)
        # Pull the same footage out of the pool, if it ever made it in. A
        # candidate links to its pool item(s) through its MediaAsset (by
        # shortcode/URL); look it up without creating a stray pending asset.
        removed = 0
        asset = find_media_asset(
            session, source_url=candidate.source_url, shortcode=candidate.video_id
        )
        if asset is not None:
            removed = remove_pool_items_for_asset(
                session, media_asset_id=asset.id, reason=f"candidate rejected: {key}"
            )
        session.commit()
        return {"candidate_id": candidate_id, "state": state, "removed_pool_items": removed}


def candidate_preview(candidate_id: int) -> dict:
    """Best available preview source for a candidate.

    Scraped IG thumbnail URLs are signed and expire within days, so for an older
    candidate the remote thumbnail no longer loads. When the footage has been
    downloaded (e.g. via "Add to Processing"), return its local file for a real
    video preview; otherwise fall back to the (possibly expired) thumbnail. The
    bridge turns ``local_path`` into an in-app ``preview_url``.
    """
    with get_session() as session:
        candidate = session.get(ScrapeCandidate, candidate_id)
        if candidate is None:
            raise SourcingError(f"No candidate with id {candidate_id}.")
        local_path = None
        asset = find_media_asset(
            session, source_url=candidate.source_url, shortcode=candidate.video_id
        )
        if asset is not None and asset.original_download_path:
            path = Path(asset.original_download_path)
            if path.exists():
                local_path = str(path)
        return {
            "candidate_id": candidate_id,
            "local_path": local_path,
            "thumbnail_url": candidate.thumbnail_url,
        }
