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

from nicheflow_studio.db import assignments as assignments_db, pools
from nicheflow_studio.db.models import Account
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
