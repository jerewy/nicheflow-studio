"""Niche pool access — accepting clips into a pool and reading them back.

A pool is just "every :class:`MediaAsset` approved for the <niche> network".
Destination accounts in that niche draw from it (docs/SOURCING_POOLING_PLAN.md
§6.3, §12). This module owns two rules:

1. **Niche isolation** — a media asset normally belongs to ONE editorial niche.
   Re-accepting the same asset into a different niche is refused unless the
   caller explicitly confirms (`allow_cross_niche=True`), so history footage
   never silently leaks into the movie network (§8.3).
2. **Idempotent accept** — accepting the same asset into the same niche twice
   returns the existing pool item instead of duplicating it.

Kept in the db layer, dependency-light, so importer/UI/assignment code share one
consistent path.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from nicheflow_studio.core.niche import NICHE_HISTORY, NICHE_MOVIE
from nicheflow_studio.db.media_library import find_or_register_media_asset
from nicheflow_studio.db.models import (
    Account,
    Assignment,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
)

VALID_NICHES = frozenset({NICHE_HISTORY, NICHE_MOVIE})

# Candidate-first review states (docs/SOURCING_POOLING_PLAN.md §1, §2, §13).
# These belong to the candidate→pool flow and are intentionally distinct from
# the legacy download-first states ("queued"/"downloaded"): a candidate accepted
# here gets a PoolItem backed by a *pending* MediaAsset and never needs a
# download to enter the pool.
CANDIDATE_STATE_POOLED = "pooled"
# Reject reason (UI key) -> stored candidate state. A rejected candidate gets no
# PoolItem, so the rule "rejected candidates never distribute" holds by
# construction. The specific reason is preserved for auditing / ad filtering.
REJECT_REASONS: dict[str, str] = {
    "ad_campaign": "rejected_ad_campaign",
    "duplicate": "rejected_duplicate",
    "wrong_niche": "rejected_wrong_niche",
    "low_quality": "rejected_low_quality",
}


class CrossNicheError(RuntimeError):
    """Raised when a media asset already accepted in one niche is being
    accepted into a different niche without explicit confirmation."""


class DuplicateContentError(RuntimeError):
    """Raised when a clip whose footage already exists in the niche pool (same
    content fingerprint, different source) is accepted without confirmation."""

    def __init__(self, message: str, *, existing_asset_id: int) -> None:
        super().__init__(message)
        self.existing_asset_id = existing_asset_id


def _validate_niche(niche: str) -> str:
    value = (niche or "").strip().lower()
    if value not in VALID_NICHES:
        raise ValueError(f"niche must be one of {sorted(VALID_NICHES)}, got {niche!r}.")
    return value


def accept_into_pool(
    session: Session,
    *,
    media_asset: MediaAsset,
    niche: str,
    accepted_reason: str | None = None,
    topic_tag: str | None = None,
    is_evergreen_candidate: bool = False,
    allow_cross_niche: bool = False,
    allow_duplicate: bool = False,
) -> PoolItem:
    """Accept ``media_asset`` into the ``niche`` pool, returning the PoolItem.

    Idempotent within a niche. Refuses a cross-niche accept (raises
    :class:`CrossNicheError`) unless ``allow_cross_niche=True``, and refuses a
    content duplicate — the same footage already in this niche under a different
    source (raises :class:`DuplicateContentError`) unless ``allow_duplicate=True``.
    Does not commit; the caller owns the transaction.
    """
    niche = _validate_niche(niche)
    existing = (
        session.query(PoolItem).filter(PoolItem.media_asset_id == media_asset.id).all()
    )
    for item in existing:
        if item.niche == niche:
            return item  # already in this pool — idempotent
    if existing and not allow_cross_niche:
        other = existing[0].niche
        raise CrossNicheError(
            f"Media asset {media_asset.id} is already in the '{other}' pool. "
            f"Accepting it into '{niche}' would mix niches. Pass "
            f"allow_cross_niche=True to override after manual confirmation."
        )
    if not allow_duplicate and media_asset.content_hash:
        duplicate = _find_content_duplicate_in_niche(
            session, niche=niche, content_hash=media_asset.content_hash, exclude_asset_id=media_asset.id
        )
        if duplicate is not None:
            raise DuplicateContentError(
                f"The same footage is already in the '{niche}' pool as media asset "
                f"{duplicate}. This clip was likely reposted by another source. "
                f"Pass allow_duplicate=True to accept it anyway.",
                existing_asset_id=duplicate,
            )
    item = PoolItem(
        media_asset_id=media_asset.id,
        niche=niche,
        acceptance_status="accepted",
        accepted_at=dt.datetime.now(dt.timezone.utc),
        accepted_reason=accepted_reason,
        topic_tag=topic_tag,
        is_evergreen_candidate=1 if is_evergreen_candidate else 0,
    )
    session.add(item)
    session.flush()  # assign id without committing the caller's transaction
    return item


def accept_candidate_into_pool(
    session: Session,
    *,
    candidate: ScrapeCandidate,
    niche: str,
    accepted_reason: str | None = None,
    topic_tag: str | None = None,
    is_evergreen_candidate: bool = False,
    allow_cross_niche: bool = False,
    allow_duplicate: bool = False,
) -> PoolItem:
    """Accept a scraped candidate into a niche pool WITHOUT downloading it.

    Candidate-first flow (docs/SOURCING_POOLING_PLAN.md §1, §13 Phase 1):
    registers (or reuses) a *pending* :class:`MediaAsset` for the candidate's
    source URL, accepts it into the niche pool, and marks the candidate
    ``pooled``. The download happens later, only when the clip is assigned and
    actually needed.

    Dedup before download is by URL/shortcode (handled by
    :func:`find_or_register_media_asset`): the same reel pooled twice resolves to
    the same asset, and :func:`accept_into_pool` is idempotent within a niche, so
    re-accepting returns the existing :class:`PoolItem`. Content/fingerprint
    dedup only applies once the asset is downloaded (its ``content_hash`` is
    ``None`` while pending). Does not commit; the caller owns the transaction.
    """
    niche = _validate_niche(niche)
    if not (candidate.source_url or "").strip():
        raise ValueError("Candidate has no source_url to pool.")
    asset, _ = find_or_register_media_asset(
        session,
        source_url=candidate.source_url,
        shortcode=candidate.video_id,
        platform="instagram",
    )
    item = accept_into_pool(
        session,
        media_asset=asset,
        niche=niche,
        accepted_reason=accepted_reason,
        topic_tag=topic_tag,
        is_evergreen_candidate=is_evergreen_candidate,
        allow_cross_niche=allow_cross_niche,
        allow_duplicate=allow_duplicate,
    )
    candidate.state = CANDIDATE_STATE_POOLED
    session.flush()
    return item


def reject_candidate(
    session: Session,
    *,
    candidate: ScrapeCandidate,
    reason: str,
) -> str:
    """Reject a candidate with a specific reason so it never enters the pool.

    ``reason`` is one of :data:`REJECT_REASONS` keys (``ad_campaign``,
    ``duplicate``, ``wrong_niche``, ``low_quality``). Sets the candidate state to
    the matching ``rejected_*`` value and creates no pool item — protecting the
    network from AI ads / promo clips (docs/SOURCING_POOLING_PLAN.md §2). Returns
    the stored state. Does not commit; the caller owns the transaction.
    """
    key = (reason or "").strip().lower()
    if key not in REJECT_REASONS:
        raise ValueError(
            f"reason must be one of {sorted(REJECT_REASONS)}, got {reason!r}."
        )
    candidate.state = REJECT_REASONS[key]
    session.flush()
    return candidate.state


def _find_content_duplicate_in_niche(
    session: Session,
    *,
    niche: str,
    content_hash: str,
    exclude_asset_id: int | None = None,
) -> int | None:
    """Asset id of an already-accepted clip in ``niche`` whose footage matches.

    Compares the new fingerprint against the fingerprints of assets already in
    this niche pool. Returns the matching asset id, or ``None``.
    """
    from nicheflow_studio.processing.dedup import fingerprints_match

    accepted = (
        session.query(MediaAsset)
        .join(PoolItem, PoolItem.media_asset_id == MediaAsset.id)
        .filter(PoolItem.niche == niche)
        .filter(MediaAsset.content_hash.is_not(None))
        .all()
    )
    for asset in accepted:
        if exclude_asset_id is not None and asset.id == exclude_asset_id:
            continue
        if fingerprints_match(content_hash, asset.content_hash):
            return asset.id
    return None


def pool_items_for_niche(
    session: Session,
    niche: str,
    *,
    status: str = "accepted",
) -> list[PoolItem]:
    """All pool items in a niche (default: accepted), newest first. This is the
    inventory Phase 3 distributes across that niche's accounts."""
    niche = _validate_niche(niche)
    query = session.query(PoolItem).filter(PoolItem.niche == niche)
    if status is not None:
        query = query.filter(PoolItem.acceptance_status == status)
    return query.order_by(PoolItem.created_at.desc()).all()


def pool_size(session: Session, niche: str, *, status: str = "accepted") -> int:
    """Count of items in a niche pool — e.g. the '2000 URLs in HISTORY' figure."""
    return len(pool_items_for_niche(session, niche, status=status))


@dataclass(frozen=True)
class NichePoolStats:
    """At-a-glance counts for one niche's pool (SOURCING_POOLING_PLAN.md §12).

    ``pooled`` is the accepted inventory; ``assigned`` is how much of it is
    already allotted to accounts; ``unused`` is what's still free to distribute;
    ``rejected`` is how many scraped candidates in this niche were rejected with
    a reason (and therefore never enter the pool).
    """

    niche: str
    pooled: int
    assigned: int
    unused: int
    rejected: int


def niche_pool_stats(session: Session, niche: str) -> NichePoolStats:
    """Compute the pool summary counts for ``niche`` (see :class:`NichePoolStats`)."""
    niche = _validate_niche(niche)
    pooled = pool_size(session, niche)
    assigned_item_ids = {
        row[0]
        for row in session.query(Assignment.pool_item_id)
        .filter(Assignment.niche == niche)
        .all()
    }
    assigned = len(assigned_item_ids)
    rejected = (
        session.query(ScrapeCandidate)
        .join(Account, Account.id == ScrapeCandidate.account_id)
        .filter(Account.niche == niche)
        .filter(ScrapeCandidate.state.like("rejected%"))
        .count()
    )
    return NichePoolStats(
        niche=niche,
        pooled=pooled,
        assigned=assigned,
        unused=max(0, pooled - assigned),
        rejected=rejected,
    )


@dataclass(frozen=True)
class PoolContentRow:
    """One accepted clip in a niche pool, with source + distribution status."""

    pool_item_id: int
    clip_label: str
    source_label: str
    accepted_at: dt.datetime | None
    distributed_to: tuple[str, ...]

    @property
    def is_distributed(self) -> bool:
        return bool(self.distributed_to)


def pool_contents(session: Session, niche: str) -> list[PoolContentRow]:
    """Accepted clips in a niche pool, newest first, with source + distribution.

    Source is attributed best-effort from the originating scrape candidate's
    owner handle (``channel_name``), matched by the asset's shortcode; manual
    imports with no candidate show ``"—"``. ``distributed_to`` lists the accounts
    this clip has been assigned to (empty = not yet distributed).
    """
    niche = _validate_niche(niche)
    items = (
        session.query(PoolItem)
        .filter(PoolItem.niche == niche, PoolItem.acceptance_status == "accepted")
        .order_by(PoolItem.accepted_at.desc(), PoolItem.created_at.desc())
        .all()
    )
    rows: list[PoolContentRow] = []
    for item in items:
        asset = item.media_asset
        shortcode = asset.source_shortcode if asset else None
        if shortcode:
            clip_label = shortcode
        elif asset and asset.original_download_path:
            clip_label = Path(asset.original_download_path).name
        elif asset and asset.canonical_source_url:
            clip_label = asset.canonical_source_url
        else:
            clip_label = f"item#{item.id}"

        source_label = "—"
        if shortcode:
            candidate = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.video_id == shortcode)
                .first()
            )
            if candidate and candidate.channel_name:
                source_label = candidate.channel_name

        accounts: list[str] = []
        for assignment in item.assignments:
            account = session.get(Account, assignment.account_id)
            accounts.append(account.name if account and account.name else f"#{assignment.account_id}")

        rows.append(
            PoolContentRow(
                pool_item_id=item.id,
                clip_label=clip_label,
                source_label=source_label,
                accepted_at=item.accepted_at,
                distributed_to=tuple(accounts),
            )
        )
    return rows
