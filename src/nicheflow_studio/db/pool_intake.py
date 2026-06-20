"""Single-URL pool intake — the "Add reel URL → pool" path.

Pool-first dedup (docs/SOURCING_POOLING_PLAN.md §1, §13): before adding a manually
pasted reel, check whether that footage is already in a pool by URL/shortcode. If
it is, skip it as a duplicate instead of double-pooling. Otherwise register a
candidate carrying its metadata and accept it into the niche pool. The second
dedup stage (footage fingerprint) runs later at download — a freshly added pending
clip has no hash yet.

The network metadata fetch is deliberately NOT done here: this module is pure DB
logic so it stays fast and testable, and the caller (a background worker) fetches
metadata off the UI thread before calling in.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from nicheflow_studio.db.blocklist import is_blocked
from nicheflow_studio.db.media_library import find_or_register_media_asset
from nicheflow_studio.db.models import Account, PoolItem, ScrapeCandidate
from nicheflow_studio.db.pools import (
    POOL_STATUS_ACCEPTED,
    POOL_STATUS_PENDING_REVIEW,
    VALID_NICHES,
    CrossNicheError,
    DuplicateContentError,
    accept_candidate_into_pool,
)


@dataclass(frozen=True)
class ReelMetadata:
    """Metadata for one reel, as fetched (e.g. via yt-dlp) before intake."""

    source_url: str
    shortcode: str | None = None
    channel_name: str | None = None
    title: str | None = None
    description: str | None = None
    published_at: dt.datetime | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True)
class PoolIntakeResult:
    """Outcome of an add-to-pool attempt.

    ``status`` is one of:
      * ``"added"``     — newly pooled.
      * ``"duplicate"`` — already in a pool (its ``niche`` is reported); skipped.
      * ``"blocked"``   — footage on the global blocklist; skipped.
      * ``"no_account"`` — no account in the target niche to own the clip.
    """

    status: str
    niche: str
    shortcode: str | None
    message: str


def add_reel_to_pool(
    session: Session,
    *,
    niche: str,
    metadata: ReelMetadata,
    pinned_account_id: int | None = None,
) -> PoolIntakeResult:
    """Add a single reel to the ``niche`` pool, pool-first deduped. Does not commit."""
    value = (niche or "").strip().lower()
    if value not in VALID_NICHES:
        raise ValueError(f"niche must be one of {sorted(VALID_NICHES)}, got {niche!r}.")
    niche = value

    # Global blocklist: footage the user rejected must never re-pool.
    if is_blocked(session, source_url=metadata.source_url, shortcode=metadata.shortcode):
        return PoolIntakeResult(
            status="blocked",
            niche=niche,
            shortcode=metadata.shortcode,
            message="Globally rejected earlier — skipped.",
        )

    # Stage 1 dedup: is this footage already pooled (any niche)?
    asset, _ = find_or_register_media_asset(
        session,
        source_url=metadata.source_url,
        shortcode=metadata.shortcode,
        platform="instagram",
    )
    for item in (
        session.query(PoolItem).filter(PoolItem.media_asset_id == asset.id).all()
    ):
        if item.acceptance_status in {POOL_STATUS_ACCEPTED, POOL_STATUS_PENDING_REVIEW}:
            return PoolIntakeResult(
                status="duplicate",
                niche=item.niche,
                shortcode=metadata.shortcode,
                message=f"Already in the {item.niche.title()} pool — skipped.",
            )

    # A candidate needs an owning account; use the niche's first account.
    account = (
        session.query(Account)
        .filter(Account.niche == niche, Account.platform == "instagram")
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        return PoolIntakeResult(
            status="no_account",
            niche=niche,
            shortcode=metadata.shortcode,
            message=(
                f"No {niche.title()} account exists to own the clip. Set an "
                f"account's niche to '{niche}' first."
            ),
        )

    pinned_account: Account | None = None
    if pinned_account_id is not None:
        pinned_account = session.get(Account, pinned_account_id)
        if pinned_account is None or pinned_account.niche != niche:
            raise ValueError("Pinned account must belong to the selected pool niche.")

    candidate = _upsert_candidate(session, account_id=account.id, metadata=metadata)
    try:
        pool_item = accept_candidate_into_pool(
            session, candidate=candidate, niche=niche, accepted_reason="manual URL add"
        )
    except CrossNicheError:
        # The footage exists in the other niche under a non-active pool item.
        return PoolIntakeResult(
            status="duplicate",
            niche=niche,
            shortcode=metadata.shortcode,
            message="This footage is already pooled in the other niche — skipped.",
        )
    except DuplicateContentError:
        return PoolIntakeResult(
            status="duplicate",
            niche=niche,
            shortcode=metadata.shortcode,
            message="The same footage is already in this pool — skipped.",
        )
    pool_item.pinned_account_id = pinned_account.id if pinned_account is not None else None
    return PoolIntakeResult(
        status="added",
        niche=niche,
        shortcode=metadata.shortcode,
        message=f"Added to the {niche.title()} pool.",
    )


def _upsert_candidate(
    session: Session, *, account_id: int, metadata: ReelMetadata
) -> ScrapeCandidate:
    """Create or refresh the candidate row carrying the reel's metadata."""
    existing: ScrapeCandidate | None = None
    if metadata.shortcode:
        existing = (
            session.query(ScrapeCandidate)
            .filter(
                ScrapeCandidate.video_id == metadata.shortcode,
                ScrapeCandidate.account_id == account_id,
            )
            .first()
        )
    candidate = existing or ScrapeCandidate(
        account_id=account_id,
        video_id=metadata.shortcode,
        source_url=metadata.source_url,
    )
    candidate.scrape_source_url = metadata.source_url
    candidate.source_url = metadata.source_url
    candidate.extractor = "instagram"
    candidate.channel_name = metadata.channel_name
    candidate.title = metadata.title
    candidate.description = metadata.description
    candidate.published_at = metadata.published_at
    candidate.view_count = metadata.view_count
    candidate.like_count = metadata.like_count
    candidate.comment_count = metadata.comment_count
    candidate.duration_seconds = metadata.duration_seconds
    candidate.thumbnail_url = metadata.thumbnail_url
    if existing is None:
        session.add(candidate)
    session.flush()
    return candidate
