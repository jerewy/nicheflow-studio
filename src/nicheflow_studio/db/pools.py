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

from sqlalchemy.orm import Session, joinedload

from nicheflow_studio.core.distribution import engagement_score
from nicheflow_studio.core.niche import NICHE_HISTORY, NICHE_MOVIE
from nicheflow_studio.core.text_dedup import normalize_caption
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
    acceptance_status: str = "accepted",
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
        duplicate = find_niche_content_duplicate(
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
        acceptance_status=acceptance_status,
        accepted_at=(
            dt.datetime.now(dt.timezone.utc)
            if acceptance_status == POOL_STATUS_ACCEPTED
            else None
        ),
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
        acceptance_status=POOL_STATUS_PENDING_REVIEW,
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


def find_niche_content_duplicate(
    session: Session,
    *,
    niche: str,
    content_hash: str,
    exclude_asset_id: int | None = None,
) -> int | None:
    """Asset id of an ACCEPTED clip in ``niche`` whose footage matches, else None.

    The footage-level dedup key: catches the same clip reposted under a different
    shortcode (which URL dedup misses). Only compares against active (accepted)
    pool members so already-flagged duplicates don't chain.
    """
    from nicheflow_studio.processing.dedup import fingerprints_match

    accepted = (
        session.query(MediaAsset)
        .join(PoolItem, PoolItem.media_asset_id == MediaAsset.id)
        .filter(PoolItem.niche == niche)
        .filter(PoolItem.acceptance_status == POOL_STATUS_ACCEPTED)
        .filter(MediaAsset.content_hash.is_not(None))
        .all()
    )
    for asset in accepted:
        if exclude_asset_id is not None and asset.id == exclude_asset_id:
            continue
        if fingerprints_match(content_hash, asset.content_hash):
            return asset.id
    return None


def flag_pool_item_duplicate(
    session: Session, *, media_asset_id: int, niche: str, reason: str
) -> bool:
    """Flag the niche's pool item for ``media_asset_id`` as duplicate footage so
    it drops out of distribution. Returns True if a pool item was flagged."""
    niche = _validate_niche(niche)
    item = (
        session.query(PoolItem)
        .filter(PoolItem.media_asset_id == media_asset_id, PoolItem.niche == niche)
        .first()
    )
    if item is None:
        return False
    item.acceptance_status = "duplicate"
    item.accepted_reason = reason
    session.flush()
    return True


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
    pending: int = 0


@dataclass(frozen=True)
class PoolDedupResult:
    """Outcome of a caption-based pool dedup pass."""

    groups: int  # number of caption groups that had >1 member
    flagged: int  # pool items marked 'duplicate' (kept one per group)


def _pool_item_caption(session: Session, item: PoolItem) -> str | None:
    """Best-effort caption for a pool item, via its asset's originating
    scrape candidate (matched by shortcode). None when unattributed."""
    asset = item.media_asset
    shortcode = asset.source_shortcode if asset is not None else None
    if not shortcode:
        return None
    candidate = (
        session.query(ScrapeCandidate)
        .filter(ScrapeCandidate.video_id == shortcode)
        .first()
    )
    if candidate is None:
        return None
    return candidate.description or candidate.title


def dedupe_pool_by_caption(session: Session, niche: str) -> PoolDedupResult:
    """Flag accepted pool items that share a normalized caption with an earlier
    item in the niche as ``duplicate`` (keeping the oldest), so they never
    distribute. A cheap PRE-download cross-source pass — the reliable footage
    check happens at download. Items with no caption are left untouched. Does
    not commit; the caller owns the transaction.
    """
    niche = _validate_niche(niche)
    items = (
        session.query(PoolItem)
        .filter(PoolItem.niche == niche, PoolItem.acceptance_status == "accepted")
        .order_by(PoolItem.id.asc())
        .all()
    )
    kept_by_key: dict[str, int] = {}
    duplicate_keys: set[str] = set()
    flagged = 0
    for item in items:
        key = normalize_caption(_pool_item_caption(session, item))
        if not key:
            continue  # unattributed / no caption — can't text-dedup
        if key in kept_by_key:
            item.acceptance_status = "duplicate"
            item.accepted_reason = f"duplicate caption of pool item #{kept_by_key[key]}"
            duplicate_keys.add(key)
            flagged += 1
        else:
            kept_by_key[key] = item.id
    session.flush()
    return PoolDedupResult(groups=len(duplicate_keys), flagged=flagged)


def niche_pool_stats(session: Session, niche: str) -> NichePoolStats:
    """Compute the pool summary counts for ``niche`` (see :class:`NichePoolStats`)."""
    niche = _validate_niche(niche)
    pooled = pool_size(session, niche)
    assigned_item_ids = {
        row[0]
        for row in session.query(Assignment.pool_item_id)
        .filter(Assignment.niche == niche)
        # Only pending assignments hold active backlog slots.
        .filter(Assignment.status == "assigned")
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
    pending = pool_size(session, niche, status=POOL_STATUS_PENDING_REVIEW)
    return NichePoolStats(
        niche=niche,
        pooled=pooled,
        assigned=assigned,
        unused=max(0, pooled - assigned),
        rejected=rejected,
        pending=pending,
    )


# Pool item lifecycle (PoolItem.acceptance_status):
#   "accepted"  — active inventory, eligible to distribute
#   "duplicate" — flagged by caption dedup (Phase A)
#   "removed"   — manually pruned in review (the manual-check gate)
# Only "accepted" items are distributed; the others stay for audit/restore.
POOL_STATUS_ACCEPTED = "accepted"
POOL_STATUS_PENDING_REVIEW = "pending_review"
POOL_STATUS_REMOVED = "removed"


def remove_pool_item(
    session: Session, *, pool_item_id: int, reason: str = "manual removal"
) -> bool:
    """Manually remove a clip from the pool — the post-pool review gate.

    Sets ``acceptance_status='removed'`` so the clip no longer distributes
    (SOURCING_POOLING_PLAN.md §2 manual review, applied at the pool stage).
    Existing assignments, if any, are left untouched. Returns ``True`` when the
    item was found. Does not commit; the caller owns the transaction.
    """
    item = session.get(PoolItem, pool_item_id)
    if item is None:
        return False
    item.acceptance_status = POOL_STATUS_REMOVED
    item.accepted_reason = reason
    session.flush()
    return True


def restore_pool_item(session: Session, *, pool_item_id: int) -> bool:
    """Undo a removal/duplicate flag — return the item to the active pool.
    Returns ``True`` when found. Does not commit."""
    item = session.get(PoolItem, pool_item_id)
    if item is None:
        return False
    item.acceptance_status = POOL_STATUS_ACCEPTED
    item.accepted_reason = None
    session.flush()
    return True


def remove_pool_items_for_asset(
    session: Session, *, media_asset_id: int, reason: str
) -> int:
    """Reversibly remove every active pool item backing one media asset.

    The shared "pull this footage out of distribution" primitive: used when a
    candidate is rejected or a clip is cleaned out from Processing, so the same
    footage stops distributing in whichever niche pool it reached. Returns the
    number of items removed. Does not commit; the caller owns the transaction.
    """
    items = (
        session.query(PoolItem)
        .filter(
            PoolItem.media_asset_id == media_asset_id,
            PoolItem.acceptance_status != POOL_STATUS_REMOVED,
        )
        .all()
    )
    for item in items:
        item.acceptance_status = POOL_STATUS_REMOVED
        item.accepted_reason = reason
    session.flush()
    return len(items)


@dataclass(frozen=True)
class PoolReviewRow:
    """One pool item for the manual-review list: id, status, label, caption."""

    pool_item_id: int
    status: str
    shortcode: str | None
    caption: str | None
    distributed_to: tuple[str, ...]


def pool_review_rows(
    session: Session, niche: str, *, include_inactive: bool = False
) -> list[PoolReviewRow]:
    """Pool items in a niche for manual review, oldest first. By default only
    active (accepted) items; ``include_inactive`` also lists removed/duplicate
    ones so a mistaken removal can be spotted and restored."""
    niche = _validate_niche(niche)
    query = session.query(PoolItem).filter(PoolItem.niche == niche)
    if not include_inactive:
        query = query.filter(PoolItem.acceptance_status == POOL_STATUS_ACCEPTED)
    items = query.order_by(PoolItem.id.asc()).all()

    rows: list[PoolReviewRow] = []
    for item in items:
        asset = item.media_asset
        shortcode = asset.source_shortcode if asset is not None else None
        accounts = [
            session.get(Account, a.account_id).name
            if session.get(Account, a.account_id) and session.get(Account, a.account_id).name
            else f"#{a.account_id}"
            for a in item.assignments
        ]
        caption = _pool_item_caption(session, item)
        rows.append(
            PoolReviewRow(
                pool_item_id=item.id,
                status=item.acceptance_status,
                shortcode=shortcode,
                caption=(caption or "")[:80] or None,
                distributed_to=tuple(accounts),
            )
        )
    return rows


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


# --- Library view (niche -> sources -> clips) -------------------------------
# Backs the simplified Pool & Distribute screen: pick a niche, see its sources
# with counts, click a source to list its clips. Source is attributed from the
# originating scrape candidate's handle (channel_name); clips with no candidate
# fall under "—".


# SQLite caps bound parameters per statement (default 999); chunk IN() lists so a
# large pool's shortcode batch doesn't blow that limit.
_SQLITE_IN_CHUNK = 900


def _candidates_by_shortcode(
    session: Session, shortcodes: set[str]
) -> dict[str, tuple[str | None, dt.datetime | None, str | None, str | None, int | None]]:
    """Batch-load candidate metadata keyed by shortcode in a few chunked queries
    instead of one query per pool item (avoids the N+1 that made the pool page
    slow). Value tuple: (channel_name, published_at, description, title, like_count)."""
    codes = [code for code in shortcodes if code]
    out: dict[str, tuple] = {}
    for start in range(0, len(codes), _SQLITE_IN_CHUNK):
        chunk = codes[start : start + _SQLITE_IN_CHUNK]
        rows = (
            session.query(
                ScrapeCandidate.video_id,
                ScrapeCandidate.channel_name,
                ScrapeCandidate.published_at,
                ScrapeCandidate.description,
                ScrapeCandidate.title,
                ScrapeCandidate.like_count,
            )
            .filter(ScrapeCandidate.video_id.in_(chunk))
            .all()
        )
        for video_id, channel, published, description, title, likes in rows:
            out.setdefault(video_id, (channel, published, description, title, likes))
    return out


def _naive(value: dt.datetime | None) -> dt.datetime:
    """Sort key for nullable, possibly tz-naive (SQLite) datetimes: missing
    dates sort oldest, and tzinfo is dropped so naive/aware never compare."""
    if value is None:
        return dt.datetime.min
    return value.replace(tzinfo=None)


@dataclass(frozen=True)
class PoolSourceRow:
    """One source's footprint in a niche pool: how many accepted clips it
    contributed and the newest post date among them."""

    source_label: str
    clip_count: int
    newest_post_at: dt.datetime | None


@dataclass(frozen=True)
class PoolClipRow:
    """One pool clip with the metadata a library row needs: the original IG URL
    to open it, caption, likes, post date, download state, distribution, plus the
    pool acceptance status and the local original file (for an in-app preview)."""

    pool_item_id: int
    shortcode: str | None
    source_url: str | None
    caption: str | None
    like_count: int | None
    published_at: dt.datetime | None
    download_status: str
    acceptance_status: str
    original_download_path: str | None
    distributed_to: tuple[str, ...]
    # Intrinsic "worth distributing" score (log-damped likes + recency); drives
    # the engagement ranking in the Pool & Distribute clip list. 0.0 with no likes.
    score: float = 0.0

    @property
    def is_distributed(self) -> bool:
        return bool(self.distributed_to)


def pool_source_summary(session: Session, niche: str) -> list[PoolSourceRow]:
    """Per-source clip counts + newest post date for a niche's accepted pool,
    busiest source first."""
    niche = _validate_niche(niche)
    items = (
        session.query(PoolItem)
        .options(joinedload(PoolItem.media_asset))
        .filter(PoolItem.niche == niche, PoolItem.acceptance_status == POOL_STATUS_ACCEPTED)
        .all()
    )
    shortcodes = {
        item.media_asset.source_shortcode
        for item in items
        if item.media_asset is not None and item.media_asset.source_shortcode
    }
    candidates = _candidates_by_shortcode(session, shortcodes)
    counts: dict[str, int] = {}
    newest: dict[str, dt.datetime | None] = {}
    for item in items:
        asset = item.media_asset
        meta = candidates.get(asset.source_shortcode) if asset and asset.source_shortcode else None
        source = meta[0] if meta and meta[0] else "—"
        counts[source] = counts.get(source, 0) + 1
        published = meta[1] if meta else None
        if published is not None and (
            newest.get(source) is None or _naive(published) > _naive(newest[source])
        ):
            newest[source] = published
        elif source not in newest:
            newest[source] = None
    rows = [
        PoolSourceRow(source_label=src, clip_count=counts[src], newest_post_at=newest.get(src))
        for src in counts
    ]
    rows.sort(key=lambda r: (-r.clip_count, r.source_label))
    return rows


def pool_clips_for_source(
    session: Session, niche: str, source_label: str, *, include_removed: bool = False
) -> list[PoolClipRow]:
    """Clips in a niche pool contributed by ``source_label``, newest post first,
    with the metadata + IG URL the library row needs.

    By default only active (accepted) clips. With ``include_removed=True`` it also
    returns clips that were reversibly removed (so the UI can show and restore
    them), accepted first.
    """
    niche = _validate_niche(niche)
    allowed_statuses = (
        [POOL_STATUS_ACCEPTED, POOL_STATUS_REMOVED]
        if include_removed
        else [POOL_STATUS_ACCEPTED]
    )
    items = (
        session.query(PoolItem)
        .options(joinedload(PoolItem.media_asset), joinedload(PoolItem.assignments))
        .filter(PoolItem.niche == niche, PoolItem.acceptance_status.in_(allowed_statuses))
        .all()
    )
    shortcodes = {
        item.media_asset.source_shortcode
        for item in items
        if item.media_asset is not None and item.media_asset.source_shortcode
    }
    candidates = _candidates_by_shortcode(session, shortcodes)
    account_ids = {a.account_id for item in items for a in item.assignments}
    account_names: dict[int, str | None] = {}
    if account_ids:
        for account_id, name in (
            session.query(Account.id, Account.name)
            .filter(Account.id.in_(list(account_ids)))
            .all()
        ):
            account_names[account_id] = name

    rows: list[PoolClipRow] = []
    for item in items:
        asset = item.media_asset
        shortcode = asset.source_shortcode if asset else None
        meta = candidates.get(shortcode) if shortcode else None
        source = meta[0] if meta and meta[0] else "—"
        if source != source_label:
            continue
        accounts = [
            account_names.get(a.account_id) or f"#{a.account_id}" for a in item.assignments
        ]
        rows.append(
            PoolClipRow(
                pool_item_id=item.id,
                shortcode=shortcode,
                source_url=(asset.canonical_source_url if asset else None),
                caption=((meta[2] or meta[3]) if meta else None),
                like_count=(meta[4] if meta else None),
                published_at=(meta[1] if meta else None),
                download_status=(asset.download_status if asset else "—"),
                acceptance_status=item.acceptance_status,
                original_download_path=(asset.original_download_path if asset else None),
                distributed_to=tuple(accounts),
                score=engagement_score(
                    like_count=(meta[4] if meta else None),
                    published_at=(meta[1] if meta else None),
                ),
            )
        )
    # Strongest clips first (engagement-ranked), tie-broken by newest post, then
    # float active clips above reversibly-removed ones (all stable sorts).
    rows.sort(key=lambda r: _naive(r.published_at), reverse=True)
    rows.sort(key=lambda r: r.score, reverse=True)
    rows.sort(key=lambda r: r.acceptance_status != POOL_STATUS_ACCEPTED)
    return rows


def move_pool_item_niche(
    session: Session, *, pool_item_id: int, target_niche: str
) -> bool:
    """Move a pooled clip to the other niche — the library's 'edit category'.

    Validates ``target_niche`` is one of the two real niches and reassigns the
    pool item. Existing assignments (if the clip was already distributed) keep
    their original niche; the UI should warn before moving a distributed clip.
    Returns ``True`` when the item was found. Does not commit.
    """
    target = _validate_niche(target_niche)
    item = session.get(PoolItem, pool_item_id)
    if item is None:
        return False
    item.niche = target
    session.flush()
    return True
