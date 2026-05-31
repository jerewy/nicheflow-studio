"""Persisted pool→account assignments (the 'order-ticket rail', Option A).

Bridges the pure distribution algorithm (``core/distribution.py``) and the DB:
gather one niche's accounts + its still-unassigned accepted pool items, plan the
spread, and write :class:`Assignment` rows. Niche isolation holds by
construction — only same-niche accounts and pool items are ever passed to the
planner (docs/SOURCING_POOLING_PLAN.md Phase 3).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from nicheflow_studio.core.distribution import plan_first_cycle
from nicheflow_studio.db.models import Account, Assignment, PoolItem
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

    plan = plan_first_cycle(
        unassigned_ids, account_ids, rng=rng, max_per_account=max_per_account
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
