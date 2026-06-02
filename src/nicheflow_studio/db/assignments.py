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

from nicheflow_studio.core.distribution import plan_first_cycle
from nicheflow_studio.db.models import Account, Assignment, MediaAsset, PoolItem
from nicheflow_studio.db.pools import VALID_NICHES, pool_items_for_niche

import random as _random


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


def distribute_niche(
    session: Session,
    niche: str,
    *,
    rng: _random.Random | None = None,
    max_per_account: int | None = None,
) -> list[Assignment]:
    """Distribute the niche's unassigned accepted pool across its accounts.

    Creates one :class:`Assignment` per planned pairing and returns them. Safe to
    re-run: already-assigned pool items are skipped, so a second call only places
    clips added since the first. Returns ``[]`` when there are no accounts or no
    unassigned clips. Does not commit; the caller owns the transaction.
    """
    niche = _validate_niche(niche)
    account_ids = account_ids_for_niche(session, niche)
    if not account_ids:
        return []

    already = assigned_pool_item_ids(session, niche)
    unassigned_ids = [
        item.id for item in pool_items_for_niche(session, niche) if item.id not in already
    ]
    if not unassigned_ids:
        return []

    # Pass each account's existing backlog so max_per_account is a TOTAL target:
    # re-running "Distribute" only tops up accounts below target and never piles
    # a second full batch onto accounts that already reached it. With
    # max_per_account=None this has no effect (uncapped fill).
    existing_counts = assignment_counts_by_account(session, niche)
    plan = plan_first_cycle(
        unassigned_ids,
        account_ids,
        rng=rng,
        max_per_account=max_per_account,
        existing_counts=existing_counts,
    )

    created: list[Assignment] = []
    for planned in plan:
        assignment = Assignment(
            pool_item_id=planned.pool_item_id,
            account_id=planned.account_id,
            niche=niche,
            status="assigned",
            reuse_iteration=0,
        )
        session.add(assignment)
        created.append(assignment)
    session.flush()
    return created


def assignment_counts_by_account(session: Session, niche: str) -> dict[int, int]:
    """How many clips each account in this niche has been assigned."""
    niche = _validate_niche(niche)
    counts: dict[int, int] = {}
    for (account_id,) in (
        session.query(Assignment.account_id).filter(Assignment.niche == niche).all()
    ):
        counts[account_id] = counts.get(account_id, 0) + 1
    return counts


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
