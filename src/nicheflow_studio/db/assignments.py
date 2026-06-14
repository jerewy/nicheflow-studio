"""Persisted pool→account assignments (the 'order-ticket rail', Option A).

Bridges the pure distribution algorithm (``core/distribution.py``) and the DB:
gather one niche's accounts + its still-unassigned accepted pool items, plan the
spread, and write :class:`Assignment` rows. Niche isolation holds by
construction — only same-niche accounts and pool items are ever passed to the
planner (docs/SOURCING_POOLING_PLAN.md Phase 3).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from nicheflow_studio.core.distribution import (
    engagement_score,
    plan_first_cycle,
    ranked_clip_order,
)
from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
    UploadJob,
)
from nicheflow_studio.db.media_library import find_media_asset
from nicheflow_studio.db.pools import VALID_NICHES, pool_items_for_niche

import random as _random

# SQLite caps a statement at ~999 bound variables; chunk IN() lists below that.
_SCORE_IN_CHUNK = 500

# An assignment whose clip turned out to be duplicate footage at download time.
# Excluded from per-account counts so the next Distribute refills the slot.
ASSIGNMENT_STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"
ASSIGNMENT_STATUS_ASSIGNED = "assigned"
ASSIGNMENT_STATUS_POSTED = "posted"
ASSIGNMENT_STATUS_REJECTED = "rejected"


def _validate_niche(niche: str) -> str:
    value = (niche or "").strip().lower()
    if value not in VALID_NICHES:
        raise ValueError(f"niche must be one of {sorted(VALID_NICHES)}, got {niche!r}.")
    return value


def assigned_pool_item_ids(session: Session, niche: str) -> set[int]:
    """Pool items in this niche that already have an assignment (any cycle).

    Used to skip already-distributed clips so the first cycle never double-books
    a clip onto a second account.
    """
    niche = _validate_niche(niche)
    rows = (
        session.query(Assignment.pool_item_id).filter(Assignment.niche == niche).all()
    )
    return {row[0] for row in rows}


def account_ids_for_niche(session: Session, niche: str) -> list[int]:
    niche = _validate_niche(niche)
    rows = (
        session.query(Account.id)
        .filter(Account.niche == niche)
        .order_by(Account.id.asc())
        .all()
    )
    return [row[0] for row in rows]


def _engagement_scores_for_pool_items(
    session: Session, pool_item_ids: list[int]
) -> dict[int, float]:
    """Engagement score per pool item, for ranked distribution.

    Two chunked IN() lookups (pool item -> source shortcode -> candidate
    like_count/published_at) instead of an N+1 per-item query, then the pure
    :func:`engagement_score`. Items whose footage carries no candidate metadata
    score 0.0 and sink to the bottom of the ranking.
    """
    if not pool_item_ids:
        return {}
    # pool item -> source shortcode (via its media asset)
    id_to_shortcode: dict[int, str] = {}
    for start in range(0, len(pool_item_ids), _SCORE_IN_CHUNK):
        chunk = pool_item_ids[start : start + _SCORE_IN_CHUNK]
        rows = (
            session.query(PoolItem.id, MediaAsset.source_shortcode)
            .join(MediaAsset, MediaAsset.id == PoolItem.media_asset_id)
            .filter(PoolItem.id.in_(chunk))
            .all()
        )
        for pool_item_id, shortcode in rows:
            if shortcode:
                id_to_shortcode[pool_item_id] = shortcode
    # source shortcode -> (like_count, published_at)
    shortcodes = list(set(id_to_shortcode.values()))
    meta: dict[str, tuple[int | None, dt.datetime | None]] = {}
    for start in range(0, len(shortcodes), _SCORE_IN_CHUNK):
        chunk = shortcodes[start : start + _SCORE_IN_CHUNK]
        rows = (
            session.query(
                ScrapeCandidate.video_id,
                ScrapeCandidate.like_count,
                ScrapeCandidate.published_at,
            )
            .filter(ScrapeCandidate.video_id.in_(chunk))
            .all()
        )
        for video_id, like_count, published_at in rows:
            meta.setdefault(video_id, (like_count, published_at))
    now = dt.datetime.now(dt.timezone.utc)
    scores: dict[int, float] = {}
    for pool_item_id in pool_item_ids:
        shortcode = id_to_shortcode.get(pool_item_id)
        like_count, published_at = (
            meta.get(shortcode, (None, None)) if shortcode else (None, None)
        )
        scores[pool_item_id] = engagement_score(
            like_count=like_count, published_at=published_at, now=now
        )
    return scores


def _create_pending_review_item(
    session: Session, *, pool_item_id: int, account_id: int
) -> None:
    """Expose a new assignment in Processing using the shared downloaded media."""
    pool_item = session.get(PoolItem, pool_item_id)
    asset = session.get(MediaAsset, pool_item.media_asset_id) if pool_item is not None else None
    if asset is None:
        return
    if session.query(DownloadItem.id).filter(
        DownloadItem.account_id == account_id,
        DownloadItem.source_url == asset.canonical_source_url,
    ).first() is not None:
        return
    candidate = (
        session.query(ScrapeCandidate)
        .filter(ScrapeCandidate.video_id == asset.source_shortcode)
        .first()
        if asset.source_shortcode
        else None
    )
    session.add(
        DownloadItem(
            source_url=asset.canonical_source_url,
            extractor=asset.platform,
            video_id=asset.source_shortcode,
            title=candidate.title if candidate is not None else asset.source_shortcode,
            source_description=candidate.description if candidate is not None else None,
            file_path=asset.original_download_path,
            status="pending_review",
            review_state="pending_review",
            account_id=account_id,
        )
    )


def distribute_niche(
    session: Session,
    niche: str,
    *,
    rng: _random.Random | None = None,
    max_per_account: int | None = None,
    targets_by_account: dict[int, int] | None = None,
    eligible_pool_item_ids: set[int] | None = None,
) -> list[Assignment]:
    """Distribute the niche's unassigned accepted pool across its accounts.

    Creates one :class:`Assignment` per planned pairing and returns them. Safe to
    re-run: already-assigned pool items are skipped, so a second call only places
    clips added since the first. Returns ``[]`` when there are no accounts or no
    unassigned clips. Does not commit; the caller owns the transaction.
    """
    niche = _validate_niche(niche)
    account_ids = account_ids_for_niche(session, niche)
    if targets_by_account is not None:
        account_ids = [account_id for account_id in account_ids if account_id in targets_by_account]
    if not account_ids:
        return []

    already = assigned_pool_item_ids(session, niche)
    unassigned_items = [
        item
        for item in pool_items_for_niche(session, niche)
        if item.id not in already
        and (eligible_pool_item_ids is None or item.id in eligible_pool_item_ids)
    ]
    unassigned_ids = [item.id for item in unassigned_items]
    if not unassigned_ids:
        return []

    rng = rng or _random.Random()
    created: list[Assignment] = []
    valid_niche_accounts = set(account_ids_for_niche(session, niche))
    selected_accounts = set(account_ids)
    for item in unassigned_items:
        if item.pinned_account_id not in selected_accounts:
            continue
        assignment = Assignment(
            pool_item_id=item.id,
            account_id=item.pinned_account_id,
            niche=niche,
            status=ASSIGNMENT_STATUS_ASSIGNED,
            reuse_iteration=0,
        )
        # Transient response metadata; lifecycle status remains "assigned".
        assignment.distribution_reason = "pinned"
        session.add(assignment)
        _create_pending_review_item(
            session, pool_item_id=item.id, account_id=item.pinned_account_id
        )
        created.append(assignment)
    pinned_item_ids = {
        item.id
        for item in unassigned_items
        if item.pinned_account_id in valid_niche_accounts
    }
    unassigned_ids = [item_id for item_id in unassigned_ids if item_id not in pinned_item_ids]
    if not unassigned_ids:
        session.flush()
        return created

    # Rank the undistributed pool by intrinsic engagement (likes + recency) so the
    # strongest clips go out first, jittered within tiers so the network doesn't
    # funnel the same top clip onto every account. plan_first_cycle then places
    # them one-per-account, best-first (shuffle_items=False preserves the rank).
    scores = _engagement_scores_for_pool_items(session, unassigned_ids)
    ranked_ids = ranked_clip_order(
        [(pool_item_id, scores.get(pool_item_id, 0.0)) for pool_item_id in unassigned_ids],
        rng=rng,
    )
    # Pass each account's existing backlog so max_per_account is a TOTAL target:
    # re-running "Distribute" only tops up accounts below target and never piles
    # a second full batch onto accounts that already reached it. With
    # max_per_account=None this has no effect (uncapped fill).
    existing_counts = assignment_counts_by_account(session, niche)
    plan = plan_first_cycle(
        ranked_ids,
        account_ids,
        rng=rng,
        max_per_account=max_per_account,
        targets_by_account=targets_by_account,
        existing_counts=existing_counts,
        shuffle_items=False,
    )

    for planned in plan:
        assignment = Assignment(
            pool_item_id=planned.pool_item_id,
            account_id=planned.account_id,
            niche=niche,
            status="assigned",
            reuse_iteration=0,
        )
        session.add(assignment)
        assignment.distribution_reason = "ranked"
        _create_pending_review_item(
            session, pool_item_id=planned.pool_item_id, account_id=planned.account_id
        )
        created.append(assignment)
    session.flush()
    return created


def assign_pool_item_to_accounts(
    session: Session, *, pool_item_id: int, account_ids: list[int]
) -> list[Assignment]:
    """Assign one accepted pool item to specific accounts in its niche.

    The manual counterpart to :func:`distribute_niche`: the user picks exactly
    which accounts get this clip. Niche isolation holds — accounts outside the
    clip's niche are ignored. Idempotent: accounts that already hold the clip are
    skipped, so re-distributing only adds the new ones. Returns the created
    assignments. Does not commit; the caller owns the transaction.
    """
    item = session.get(PoolItem, pool_item_id)
    if item is None:
        raise ValueError(f"No pool item with id {pool_item_id}.")
    niche = item.niche
    in_niche = set(account_ids_for_niche(session, niche))
    already = {
        account_id
        for (account_id,) in session.query(Assignment.account_id)
        .filter(Assignment.pool_item_id == pool_item_id)
        .all()
    }
    created: list[Assignment] = []
    for account_id in account_ids:
        if account_id not in in_niche or account_id in already:
            continue
        assignment = Assignment(
            pool_item_id=pool_item_id,
            account_id=account_id,
            niche=niche,
            status="assigned",
            reuse_iteration=0,
        )
        session.add(assignment)
        _create_pending_review_item(
            session, pool_item_id=pool_item_id, account_id=account_id
        )
        already.add(account_id)
        created.append(assignment)
    session.flush()
    return created


def assignment_counts_by_account(session: Session, niche: str) -> dict[int, int]:
    """How many ACTIVE clips each account in this niche holds.

    Excludes ``skipped_duplicate`` assignments so a clip dropped as duplicate
    footage at download leaves the account short — and the next Distribute tops
    it back up with a fresh clip (the dedupe 'replace' step).
    """
    niche = _validate_niche(niche)
    counts: dict[int, int] = {}
    for (account_id,) in (
        session.query(Assignment.account_id)
        .filter(Assignment.niche == niche)
        .filter(Assignment.status == ASSIGNMENT_STATUS_ASSIGNED)
        .all()
    ):
        counts[account_id] = counts.get(account_id, 0) + 1
    return counts


def _active_assignments_for_item(session: Session, item: DownloadItem) -> list[Assignment]:
    asset = find_media_asset(session, source_url=item.source_url, shortcode=item.video_id)
    if asset is None or item.account_id is None:
        return []
    pool_item_ids = [
        row[0]
        for row in session.query(PoolItem.id).filter(PoolItem.media_asset_id == asset.id).all()
    ]
    if not pool_item_ids:
        return []
    return session.query(Assignment).filter(
        Assignment.account_id == item.account_id,
        Assignment.pool_item_id.in_(pool_item_ids),
        Assignment.status == ASSIGNMENT_STATUS_ASSIGNED,
    ).all()


def mark_assignment_posted_for_job(session: Session, job: UploadJob) -> int:
    """Mark the originating active assignment posted; repeated calls are no-ops."""
    rows = session.query(Assignment).filter(
        Assignment.upload_job_id == job.id,
        Assignment.status == ASSIGNMENT_STATUS_ASSIGNED,
    ).all()
    if not rows and job.download_item_id is not None:
        item = session.get(DownloadItem, job.download_item_id)
        rows = _active_assignments_for_item(session, item) if item is not None else []
    for assignment in rows:
        assignment.status = ASSIGNMENT_STATUS_POSTED
        assignment.upload_job_id = job.id
    session.flush()
    return len(rows)


def reject_assignments_for_item(session: Session, item: DownloadItem) -> int:
    """Release this distributed item's active assignment after review reject."""
    rows = _active_assignments_for_item(session, item)
    for assignment in rows:
        assignment.status = ASSIGNMENT_STATUS_REJECTED
    session.flush()
    return len(rows)


def assignments_for_account(session: Session, account_id: int) -> list[Assignment]:
    """All assignments allotted to one account, newest first."""
    return (
        session.query(Assignment)
        .filter(Assignment.account_id == account_id)
        .order_by(Assignment.created_at.desc())
        .all()
    )


@dataclass(frozen=True)
class AccountAssignmentRow:
    """One clip allotted to an account, with the info needed to show its place in
    the backlog (SOURCING_POOLING_PLAN.md §13 Phase 5).

    ``download_status`` comes from the backing :class:`MediaAsset` — "pending"
    until the original is fetched (candidate-first means most of the backlog is
    pending), then "downloaded".
    """

    assignment_id: int
    pool_item_id: int
    clip_label: str
    niche: str
    status: str
    download_status: str
    scheduled_date: dt.datetime | None
    reuse_iteration: int


def _clip_label(asset: MediaAsset | None, pool_item_id: int) -> str:
    """Best-effort human label for a clip: shortcode, else file name, else URL."""
    if asset is not None:
        if asset.source_shortcode:
            return asset.source_shortcode
        if asset.original_download_path:
            return Path(asset.original_download_path).name
        if asset.canonical_source_url:
            return asset.canonical_source_url
    return f"item#{pool_item_id}"


@dataclass(frozen=True)
class PendingAssetDownload:
    """A media asset that has at least one assignment but isn't on disk yet.

    Deduped by asset so a clip shared across accounts is fetched ONCE
    (download-once, reference-many — SOURCING_POOLING_PLAN.md §8, §11).
    ``assignment_ids`` are the assignments waiting on this asset.
    """

    media_asset_id: int
    source_url: str
    shortcode: str | None
    niche: str
    assignment_ids: tuple[int, ...]


def pending_download_assignments(
    session: Session, niche: str | None = None
) -> list[PendingAssetDownload]:
    """Unique media assets referenced by a (non-skipped) assignment whose
    original is still pending download, oldest assignment first. Optionally
    scoped to one niche."""
    query = (
        session.query(Assignment.id, Assignment.niche, MediaAsset)
        .join(PoolItem, PoolItem.id == Assignment.pool_item_id)
        .join(MediaAsset, MediaAsset.id == PoolItem.media_asset_id)
        .filter(MediaAsset.download_status != "downloaded")
        .filter(Assignment.status == ASSIGNMENT_STATUS_ASSIGNED)
    )
    if niche is not None:
        query = query.filter(Assignment.niche == _validate_niche(niche))

    ordered_asset_ids: list[int] = []
    by_asset: dict[int, dict] = {}
    for assignment_id, assignment_niche, asset in query.order_by(Assignment.id.asc()).all():
        entry = by_asset.get(asset.id)
        if entry is None:
            by_asset[asset.id] = {
                "source_url": asset.canonical_source_url,
                "shortcode": asset.source_shortcode,
                "niche": assignment_niche,
                "assignment_ids": [assignment_id],
            }
            ordered_asset_ids.append(asset.id)
        else:
            entry["assignment_ids"].append(assignment_id)

    return [
        PendingAssetDownload(
            media_asset_id=asset_id,
            source_url=by_asset[asset_id]["source_url"],
            shortcode=by_asset[asset_id]["shortcode"],
            niche=by_asset[asset_id]["niche"],
            assignment_ids=tuple(by_asset[asset_id]["assignment_ids"]),
        )
        for asset_id in ordered_asset_ids
    ]


def account_assignment_backlog(
    session: Session, account_id: int
) -> list[AccountAssignmentRow]:
    """The clips assigned to one account, newest first, with clip label and
    download state — the per-account backlog waiting for download/process."""
    rows: list[AccountAssignmentRow] = []
    assignments = (
        session.query(Assignment)
        .filter(Assignment.account_id == account_id)
        .order_by(Assignment.created_at.desc())
        .all()
    )
    for assignment in assignments:
        pool_item = session.get(PoolItem, assignment.pool_item_id)
        asset = pool_item.media_asset if pool_item is not None else None
        rows.append(
            AccountAssignmentRow(
                assignment_id=assignment.id,
                pool_item_id=assignment.pool_item_id,
                clip_label=_clip_label(asset, assignment.pool_item_id),
                niche=assignment.niche,
                status=assignment.status,
                download_status=asset.download_status if asset is not None else "—",
                scheduled_date=assignment.scheduled_date,
                reuse_iteration=assignment.reuse_iteration,
            )
        )
    return rows
