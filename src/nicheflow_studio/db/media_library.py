"""Global Media Library access — the global dedup gate.

Before downloading any accepted candidate, callers ask this module whether the
original already exists (by Instagram shortcode, else canonical URL). If it
does, they link to the existing :class:`MediaAsset` instead of downloading a
second physical copy (docs/SOURCING_POOLING_PLAN.md §8). Kept in the db layer
and dependency-light so importers/UI use one consistent dedup path.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from nicheflow_studio.db.models import MediaAsset


def normalize_source_url(url: str | None) -> str:
    """Canonical form of a post/reel URL for dedup: scheme/host lowercased,
    no query string, no trailing slash. Path case is preserved because the
    Instagram shortcode (in the path) is case-sensitive."""
    text = (url or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    # Lowercase only the scheme+host, keep the path (shortcode) as-is.
    if "://" in text:
        scheme, rest = text.split("://", 1)
        host, _, path = rest.partition("/")
        text = f"{scheme.lower()}://{host.lower()}/{path}" if path else f"{scheme.lower()}://{host.lower()}"
    return text.rstrip("/")


def extract_instagram_shortcode(url: str | None) -> str | None:
    """Pull the shortcode out of an Instagram /p/, /reel/, or /tv/ URL.

    Returns ``None`` for non-matching URLs so callers fall back to URL-based
    dedup. The shortcode is the strongest dedup key — the same reel reposted at
    a different URL still shares it when the URL exposes it.
    """
    normalized = normalize_source_url(url)
    if "instagram.com/" not in normalized.lower():
        return None
    parts = [p for p in normalized.split("/") if p]
    for marker in ("p", "reel", "reels", "tv"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return None


def find_media_asset(
    session: Session,
    *,
    source_url: str | None,
    shortcode: str | None = None,
) -> MediaAsset | None:
    """Return the existing global asset for this source, or ``None``.

    Matches by shortcode first (strongest key), then by canonical URL.
    """
    code = shortcode or extract_instagram_shortcode(source_url)
    if code:
        existing = (
            session.query(MediaAsset).filter(MediaAsset.source_shortcode == code).first()
        )
        if existing is not None:
            return existing
    canonical = normalize_source_url(source_url)
    if canonical:
        return (
            session.query(MediaAsset)
            .filter(MediaAsset.canonical_source_url == canonical)
            .first()
        )
    return None


def find_or_register_media_asset(
    session: Session,
    *,
    source_url: str,
    shortcode: str | None = None,
    platform: str = "instagram",
    platform_media_id: str | None = None,
) -> tuple[MediaAsset, bool]:
    """Look up the asset for ``source_url``; create a ``pending`` row if absent.

    Returns ``(asset, created)``. ``created`` is ``True`` only when a new row was
    registered — callers download the original only in that case, otherwise they
    reuse ``asset.original_download_path``. Does not commit; the caller owns the
    transaction.
    """
    if not source_url or not source_url.strip():
        raise ValueError("source_url is required to register a media asset.")
    existing = find_media_asset(session, source_url=source_url, shortcode=shortcode)
    if existing is not None:
        return existing, False
    asset = MediaAsset(
        platform=platform,
        canonical_source_url=normalize_source_url(source_url),
        source_shortcode=shortcode or extract_instagram_shortcode(source_url),
        platform_media_id=platform_media_id,
        download_status="pending",
    )
    session.add(asset)
    session.flush()  # assign id without committing the caller's transaction
    return asset, True


def mark_media_asset_downloaded(
    asset: MediaAsset,
    *,
    original_download_path: str,
    file_size_bytes: int | None = None,
    checksum: str | None = None,
) -> None:
    """Record that the original file is now on disk for this asset."""
    asset.original_download_path = original_download_path
    asset.file_size_bytes = file_size_bytes
    asset.checksum = checksum
    asset.download_status = "downloaded"
    asset.downloaded_at = dt.datetime.now(dt.timezone.utc)
