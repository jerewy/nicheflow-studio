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

from sqlalchemy.orm import Session

from nicheflow_studio.core.niche import NICHE_HISTORY, NICHE_MOVIE
from nicheflow_studio.db.models import MediaAsset, PoolItem

VALID_NICHES = frozenset({NICHE_HISTORY, NICHE_MOVIE})


class CrossNicheError(RuntimeError):
    """Raised when a media asset already accepted in one niche is being
    accepted into a different niche without explicit confirmation."""


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
) -> PoolItem:
    """Accept ``media_asset`` into the ``niche`` pool, returning the PoolItem.

    Idempotent within a niche. Refuses a cross-niche accept (raises
    :class:`CrossNicheError`) unless ``allow_cross_niche=True``. Does not commit;
    the caller owns the transaction.
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
