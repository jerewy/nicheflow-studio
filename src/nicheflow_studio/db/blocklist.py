"""Globally-blocked footage — the 'never pool this again' gate.

When the user globally rejects a clip (an ad, off-niche, or a near-duplicate the
dedup passes missed), its dedup keys are recorded here. Pool intake checks this
list before accepting a clip, so blocked footage cannot keep returning on
re-scrapes. Keyed like :mod:`db.media_library` dedup: shortcode first (the
strongest key), then canonical URL.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from nicheflow_studio.db.media_library import extract_instagram_shortcode, normalize_source_url
from nicheflow_studio.db.models import BlockedAsset


def is_blocked(
    session: Session, *, source_url: str | None, shortcode: str | None = None
) -> bool:
    """True if this footage was globally rejected (by shortcode or canonical URL)."""
    code = shortcode or extract_instagram_shortcode(source_url)
    if code and (
        session.query(BlockedAsset).filter(BlockedAsset.source_shortcode == code).first()
    ):
        return True
    canonical = normalize_source_url(source_url)
    if canonical and (
        session.query(BlockedAsset)
        .filter(BlockedAsset.canonical_source_url == canonical)
        .first()
    ):
        return True
    return False


def block_asset(
    session: Session,
    *,
    source_url: str | None,
    shortcode: str | None = None,
    reason: str | None = None,
) -> BlockedAsset:
    """Add footage to the blocklist (idempotent by shortcode/URL). Does not commit."""
    code = shortcode or extract_instagram_shortcode(source_url)
    canonical = normalize_source_url(source_url) or None

    existing: BlockedAsset | None = None
    if code:
        existing = (
            session.query(BlockedAsset).filter(BlockedAsset.source_shortcode == code).first()
        )
    if existing is None and canonical:
        existing = (
            session.query(BlockedAsset)
            .filter(BlockedAsset.canonical_source_url == canonical)
            .first()
        )
    if existing is not None:
        if reason and not existing.reason:
            existing.reason = reason
        return existing

    blocked = BlockedAsset(
        canonical_source_url=canonical, source_shortcode=code, reason=reason
    )
    session.add(blocked)
    session.flush()
    return blocked
