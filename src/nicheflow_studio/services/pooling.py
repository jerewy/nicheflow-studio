"""Pooling / Distribution read views (UI-independent).

A read-only overview of the shared niche pools and how their accepted clips are
distributed across accounts, backing the migrated Pool & Distribute screen.

Scope is deliberately read-only: pool intake, acceptance/rejection, dedup,
pruning, and distribution planning are complex, partly irreversible operations
that remain in the desktop app and the pool-admin scripts
(``scripts/pool_admin.py``). This screen surfaces state; it does not mutate pools.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

from nicheflow_studio.core.distribution import target_backlog
from nicheflow_studio.db import assignments as assignments_db, pools
from nicheflow_studio.db.models import Account, Assignment
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

# The shared-pool niches (docs/SOURCING_POOLING_PLAN.md). Kept explicit so the
# overview is stable even before any pool rows exist.
NICHES = ("history", "movie")


class PoolingError(ServiceError):
    """Raised for invalid pooling queries (e.g. an unknown niche)."""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def overview() -> dict:
    """Per-niche pool stats + per-account assignment counts."""
    with get_session() as session:
        names = {a.id: a.name for a in session.query(Account).all()}
        niches = []
        for niche in NICHES:
            stats = pools.niche_pool_stats(session, niche)
            counts = assignments_db.assignment_counts_by_account(session, niche)
            per_account = [
                {
                    "account_id": account_id,
                    "account_name": names.get(account_id, f"#{account_id}"),
                    "count": count,
                }
                for account_id, count in sorted(counts.items(), key=lambda kv: -kv[1])
            ]
            niches.append(
                {
                    "niche": niche,
                    "pooled": stats.pooled,
                    "assigned": stats.assigned,
                    "unused": stats.unused,
                    "rejected": stats.rejected,
                    "assignments_by_account": per_account,
                }
            )
        return {"niches": niches}


def list_pool_items(niche: str) -> list[dict]:
    """Accepted clips in a niche pool with their source and distribution state."""
    with get_session() as session:
        try:
            rows = pools.pool_contents(session, niche)
        except ValueError as exc:  # _validate_niche rejects unknown niches
            raise PoolingError(str(exc)) from exc
        return [
            {
                "pool_item_id": row.pool_item_id,
                "clip_label": row.clip_label,
                "source_label": row.source_label,
                "accepted_at": _iso(row.accepted_at),
                "distributed_to": list(row.distributed_to),
                "is_distributed": row.is_distributed,
            }
            for row in rows
        ]


def list_sources(niche: str) -> list[dict]:
    """Source summary rows for the selected niche pool."""
    with get_session() as session:
        try:
            rows = pools.pool_source_summary(session, niche)
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        return [
            {
                "source_label": row.source_label,
                "clip_count": row.clip_count,
                "newest_post_at": _iso(row.newest_post_at),
            }
            for row in rows
        ]


def list_source_clips(
    niche: str, source_label: str, include_removed: bool = False
) -> list[dict]:
    """Detailed clip rows for one source in a niche pool.

    ``original_download_path`` is the local original file; the bridge turns it
    into an in-app ``preview_url``. With ``include_removed`` the reversibly
    removed clips are included too (so they can be restored).
    """
    with get_session() as session:
        try:
            rows = pools.pool_clips_for_source(
                session, niche, source_label, include_removed=include_removed
            )
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        return [
            {
                "pool_item_id": row.pool_item_id,
                "shortcode": row.shortcode,
                "source_url": row.source_url,
                "caption": row.caption,
                "like_count": row.like_count,
                "published_at": _iso(row.published_at),
                "download_status": row.download_status,
                "acceptance_status": row.acceptance_status,
                "original_download_path": row.original_download_path,
                "distributed_to": list(row.distributed_to),
                "score": round(row.score, 3),
            }
            for row in rows
        ]


def remove_pool_item(pool_item_id: int, reason: str = "manual removal") -> dict:
    """Reversibly remove a clip from its niche pool so it stops distributing.

    Existing assignments are left untouched; restore the clip to undo it. The
    manual post-pool review gate (SOURCING_POOLING_PLAN.md §2).
    """
    with get_session() as session:
        if not pools.remove_pool_item(session, pool_item_id=pool_item_id, reason=reason):
            raise PoolingError(f"No pool item with id {pool_item_id}.")
        released = 0
        for assignment in session.query(Assignment).filter(
            Assignment.pool_item_id == pool_item_id,
            Assignment.status == assignments_db.ASSIGNMENT_STATUS_ASSIGNED,
        ).all():
            assignment.status = assignments_db.ASSIGNMENT_STATUS_REJECTED
            released += 1
        session.commit()
        return {
            "pool_item_id": pool_item_id,
            "acceptance_status": "removed",
            "released_assignments": released,
        }


def restore_pool_item(pool_item_id: int) -> dict:
    """Undo a removal — return the clip to the active pool."""
    with get_session() as session:
        if not pools.restore_pool_item(session, pool_item_id=pool_item_id):
            raise PoolingError(f"No pool item with id {pool_item_id}.")
        session.commit()
        return {"pool_item_id": pool_item_id, "acceptance_status": "accepted"}


def niche_accounts(niche: str) -> list[dict]:
    """Accounts in a niche (id + name), for the per-clip distribute picker."""
    with get_session() as session:
        try:
            ids = assignments_db.account_ids_for_niche(session, niche)
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        names = (
            {a.id: a.name for a in session.query(Account).filter(Account.id.in_(ids)).all()}
            if ids
            else {}
        )
        return [{"id": account_id, "name": names.get(account_id, f"#{account_id}")} for account_id in ids]


def distribute_clip(pool_item_id: int, account_ids: list[int]) -> dict:
    """Distribute one pooled clip to the chosen accounts (idempotent, same-niche).

    Returns how many new assignments were created (accounts that already had the
    clip are skipped).
    """
    cleaned = [int(account_id) for account_id in (account_ids or [])]
    with get_session() as session:
        try:
            created = assignments_db.assign_pool_item_to_accounts(
                session, pool_item_id=pool_item_id, account_ids=cleaned
            )
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        session.commit()
        return {"pool_item_id": pool_item_id, "assigned": len(created)}


def distribute_niche(niche: str, max_per_account: int | None = None) -> dict:
    """Auto-distribute a niche's undistributed pool across its accounts.

    Ranks the unassigned pool by intrinsic engagement (likes + recency), spreads
    the strongest clips one-per-account (volume-balanced, jittered within score
    tiers so accounts don't all get the same top clip), and tops each account up
    to each account's cadence-based total target. An explicit
    ``max_per_account`` remains a uniform override. Idempotent:
    re-running only places clips for accounts still under target and never
    double-books a clip. Returns how many assignments were created, a per-account
    breakdown, and a ``reason`` string when assigned is 0 so the caller can show
    a specific message instead of a generic one:
    - ``"no_accounts"``  — no accounts are in this niche yet.
    - ``"all_at_cap"``   — every account already holds its target clips.
    - ``"pool_empty"``   — all accepted clips are already assigned.
    """
    with get_session() as session:
        accounts = session.query(Account).filter(Account.niche == niche).all()
        targets = {
            account.id: (
                max_per_account
                if max_per_account is not None
                else target_backlog(account.daily_posts_target)
            )
            for account in accounts
        }
        try:
            created = assignments_db.distribute_niche(
                session,
                niche,
                max_per_account=max_per_account,
                targets_by_account=None if max_per_account is not None else targets,
            )
        except ValueError as exc:  # unknown niche from _validate_niche
            raise PoolingError(str(exc)) from exc

        reason: str | None = None
        if not created:
            acct_ids = assignments_db.account_ids_for_niche(session, niche)
            if not acct_ids:
                reason = "no_accounts"
            else:
                existing = assignments_db.assignment_counts_by_account(session, niche)
                reason = (
                    "all_at_cap"
                    if all(existing.get(a, 0) >= targets[a] for a in acct_ids)
                    else "pool_empty"
                )

        per_account = Counter(assignment.account_id for assignment in created)
        pinned_per_account = Counter(
            assignment.account_id
            for assignment in created
            if getattr(assignment, "distribution_reason", None) == "pinned"
        )
        names = (
            {
                account_id: name
                for account_id, name in session.query(Account.id, Account.name)
                .filter(Account.id.in_(list(per_account)))
                .all()
            }
            if per_account
            else {}
        )
        session.commit()
        unique_targets = set(targets.values())
        response_cap = (
            max_per_account
            if max_per_account is not None
            else next(iter(unique_targets))
            if len(unique_targets) == 1
            else target_backlog()
            if not unique_targets
            else None
        )
        result: dict = {
            "niche": niche,
            "assigned": len(created),
            "pinned": sum(pinned_per_account.values()),
            "max_per_account": response_cap,
            "accounts": [
                {
                    "account_id": account_id,
                    "account_name": names.get(account_id, f"#{account_id}"),
                    "count": count,
                    "pinned": pinned_per_account.get(account_id, 0),
                    "target": targets[account_id],
                }
                for account_id, count in sorted(per_account.items(), key=lambda kv: -kv[1])
            ],
        }
        if reason is not None:
            result["reason"] = reason
        return result


def distribute_niche_explicit(niche: str, targets_by_account: dict[int, int]) -> dict:
    """Add explicit clip counts to selected accounts, ignoring cadence targets."""
    requested = {
        int(account_id): max(0, int(target))
        for account_id, target in (targets_by_account or {}).items()
    }
    with get_session() as session:
        existing = assignments_db.assignment_counts_by_account(session, niche)
        total_targets = {
            account_id: existing.get(account_id, 0) + count
            for account_id, count in requested.items()
        }
        try:
            created = assignments_db.distribute_niche(
                session, niche, targets_by_account=total_targets
            )
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        assigned = Counter(row.account_id for row in created)
        pinned = Counter(
            row.account_id
            for row in created
            if getattr(row, "distribution_reason", None) == "pinned"
        )
        names = {
            account.id: account.name
            for account in session.query(Account).filter(Account.id.in_(list(requested))).all()
        }
        session.commit()
        return {
            "niche": niche,
            "assigned": len(created),
            "pinned": sum(pinned.values()),
            "max_per_account": None,
            "accounts": [
                {
                    "account_id": account_id,
                    "account_name": names.get(account_id, f"#{account_id}"),
                    "count": assigned.get(account_id, 0),
                    "pinned": pinned.get(account_id, 0),
                    "target": target,
                }
                for account_id, target in requested.items()
            ],
        }
