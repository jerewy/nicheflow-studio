"""Apify scrape -> niche pool (background-job orchestration).

The React Scraping tab can pull a source's recent posts straight into the shared
niche pool. This runs the Apify ``instagram-scraper`` (so your own IG accounts
never log in or scrape), deduplicates pool-first by URL/shortcode via
:func:`add_reel_to_pool`, and records how many results Apify returned so the UI
can warn before the free tier is exhausted.

Heavy + billed, so the bridge runs :func:`scrape_source_to_pool` through the
JobManager and the UI polls it. Footage-level (stage 2) dedup still happens later
at download — a freshly pooled clip is a pending asset with no fingerprint yet.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable

from nicheflow_studio.core.apify_usage import monthly_apify_usage
from nicheflow_studio.core.paths import downloads_dir
from nicheflow_studio.db.media_library import (
    find_media_asset,
    find_or_register_media_asset,
    mark_media_asset_downloaded,
)
from nicheflow_studio.db.models import Account, DownloadItem, ScrapeCandidate, Source
from nicheflow_studio.db.pool_intake import ReelMetadata, add_reel_to_pool
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing.dedup import safe_video_fingerprint
from nicheflow_studio.downloader.instagram import download_instagram_url
from nicheflow_studio.scraper.instagram_apify import scrape_instagram_source_apify
from nicheflow_studio.services.errors import ServiceError

DEFAULT_MAX_ITEMS = 30
# On a re-scrape, look slightly before last_scraped_at so a post near the previous
# boundary isn't skipped; Apify's onlyPostsNewerThan keeps the bill down.
_RESCRAPE_BUFFER = dt.timedelta(days=1)


class ScrapingError(ServiceError):
    """Raised for invalid scrape requests (unknown source, no niche, Apify error)."""


def apify_usage() -> dict:
    """This month's Apify usage vs the free cap (for the Scraping-tab reminder)."""
    return monthly_apify_usage()


# How far the oldest returned post may sit beyond the previous watermark before
# the run is treated as incomplete. The rescrape buffer already re-asks for one
# day, so anything past that is either a genuine quiet spell or a truncated
# fetch — and we cannot tell which from the results alone. Erring toward "did
# not advance" costs a few duplicate rows on the next run (deduped, pennies);
# erring the other way loses the posts permanently.
_WATERMARK_GAP_TOLERANCE = dt.timedelta(days=3)


def _as_naive_utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite hands back naive datetimes while Apify's are aware; compare flat."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def _resolve_watermark(
    previous: dt.datetime | None, results: list
) -> tuple[dt.datetime | None, bool]:
    """New ``last_scraped_at`` for a completed run, and whether it looked partial.

    Returns ``(None, True)`` when the watermark must NOT move.

    ``last_scraped_at`` is a high-water mark: the next run asks Apify for posts
    newer than it, so advancing it declares "everything up to here is already
    pooled". A high-water mark cannot express a HOLE, and Apify returns
    newest-first — so a truncated fetch hands back the most recent few posts and
    nothing in between. Stamping wall-clock ``now`` after that (the old
    behaviour) silently made the skipped posts unreachable forever: a real run
    returned 17 posts covering 2 days, advanced the watermark by 54 days, and
    orphaned ~436 posts that were only recoverable because a duplicate source
    row happened to still hold the older watermark.

    So the run is only trusted when what came back CONTINUES from where we left
    off. If the oldest returned post sits well beyond the previous watermark,
    there is a gap between the two and the watermark stays put.
    """
    published = sorted(
        stamp
        for stamp in (_as_naive_utc(getattr(r, "published_at", None)) for r in results)
        if stamp is not None
    )
    if not published:
        # Nothing dated came back (empty run, or an actor that dropped
        # timestamps). Either way there is nothing to justify moving on.
        return None, bool(results)
    previous_naive = _as_naive_utc(previous)
    if previous_naive is not None and published[0] - previous_naive > _WATERMARK_GAP_TOLERANCE:
        return None, True
    return published[-1], False


def _record_run(
    source_id: int,
    *,
    ok: bool,
    error: str | None = None,
    watermark: dt.datetime | None = None,
    advance: bool = True,
) -> None:
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            return
        # Only move the high-water mark when the run actually proved coverage up
        # to that point (see _resolve_watermark). A failed or partial run leaves
        # it alone so the next attempt re-asks from the same place.
        if ok and advance and watermark is not None:
            source.last_scraped_at = watermark
        source.last_run_status = "completed" if ok else "error"
        source.last_error_summary = (error or None) and error[:500]
        session.commit()


def scrape_source_to_pool(
    source_id: int,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Scrape one source via Apify and add its posts to the account's niche pool.

    Returns a summary: how many results Apify returned, how many were newly
    pooled vs skipped as duplicates, and the updated monthly Apify usage.
    """
    if max_items < 1:
        raise ScrapingError("Max items must be at least 1.")

    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise ScrapingError(f"No source with id {source_id}.")
        account = session.get(Account, source.account_id) if source.account_id else None
        niche = ((account.niche if account else "") or "").strip().lower()
        source_url = source.source_url
        label = source.label
        since = source.last_scraped_at
    if not niche:
        raise ScrapingError(
            "This source's account has no niche set. Set the account niche "
            "(history/movie) before scraping into the pool."
        )

    if progress:
        progress(0.05, f"Scraping {label} via Apify…")

    since_arg = (since - _RESCRAPE_BUFFER) if since else None
    try:
        results = scrape_instagram_source_apify(
            source_url=source_url, max_items=max_items, since=since_arg
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message, record the run
        _record_run(source_id, ok=False, error=str(exc))
        raise ScrapingError(f"Apify scrape failed: {exc}") from exc

    # Usage is recorded inside the Apify scraper itself (per billed dataset
    # row, not per usable candidate), so nothing to record here.

    if progress:
        progress(0.5, f"Pooling {len(results)} result(s)…")

    added = duplicates = blocked = no_account = 0
    with get_session() as session:
        for index, candidate in enumerate(results):
            metadata = ReelMetadata(
                source_url=candidate.source_url,
                shortcode=candidate.video_id,
                channel_name=candidate.channel_name,
                title=candidate.title,
                description=candidate.description,
                published_at=candidate.published_at,
                view_count=candidate.view_count,
                like_count=candidate.like_count,
                comment_count=candidate.comment_count,
                duration_seconds=candidate.duration_seconds,
                thumbnail_url=candidate.thumbnail_url,
            )
            result = add_reel_to_pool(session, niche=niche, metadata=metadata)
            if result.status == "added":
                added += 1
            elif result.status == "duplicate":
                duplicates += 1
            elif result.status == "blocked":
                blocked += 1
            else:  # "no_account"
                no_account += 1
            if progress and results:
                progress(0.5 + 0.45 * ((index + 1) / len(results)), "")
        session.commit()

    watermark, partial = _resolve_watermark(since, results)
    _record_run(source_id, ok=True, watermark=watermark, advance=not partial)

    if progress:
        progress(1.0, f"Done — {added} added, {duplicates} duplicate(s).")
    return {
        "source_id": source_id,
        "niche": niche,
        "scraped": len(results),
        "added": added,
        "duplicates": duplicates,
        "blocked": blocked,
        "no_account": no_account,
        # True when the results did not continue from the previous watermark, so
        # posts in between were missed and the watermark was left where it was.
        # Re-run to pick them up; the caller should say so rather than reporting
        # a clean "Done".
        "partial": partial,
        "apify_usage": monthly_apify_usage(),
    }


def _candidate_download_dir(account_id: int | None) -> Path:
    """Per-account folder so candidate downloads stay organized on disk."""
    return downloads_dir() / f"acc_{account_id if account_id is not None else 'unassigned'}"


def add_candidate_to_processing(
    candidate_id: int, *, progress: Callable[[float, str], None] | None = None
) -> dict:
    """Add a scrape candidate into Processing as a new download item.

    Reuses the footage if it's already on disk (no network, zero ban risk),
    otherwise downloads the reel via yt-dlp into a per-account folder. Then creates
    a ``new`` :class:`DownloadItem` (with the local file) assigned to the
    candidate's account, links the candidate to it, and marks the candidate
    ``downloaded``. Returns the new item id and whether it was reused/downloaded.
    """
    with get_session() as session:
        candidate = session.get(ScrapeCandidate, candidate_id)
        if candidate is None:
            raise ScrapingError(f"No candidate with id {candidate_id}.")
        account_id = candidate.account_id
        source_url = (candidate.source_url or "").strip()
        shortcode = candidate.video_id
        title = candidate.title
        description = candidate.description
        # Already in Processing — return the existing item instead of duplicating.
        if candidate.queued_download_item_id is not None:
            existing = session.get(DownloadItem, candidate.queued_download_item_id)
            if existing is not None:
                return {
                    "item_id": existing.id,
                    "reused": True,
                    "downloaded": False,
                    "message": "Already in Processing.",
                }
    if not source_url:
        raise ScrapingError("Candidate has no source URL to download.")

    # 1. Reuse the original if it's already on disk (download-once, reference-many).
    file_path: str | None = None
    with get_session() as session:
        asset = find_media_asset(session, source_url=source_url, shortcode=shortcode)
        if asset is not None and asset.original_download_path:
            candidate_path = Path(asset.original_download_path)
            if candidate_path.exists():
                file_path = str(candidate_path)
    reused = file_path is not None

    # 2. Otherwise download it into the account's folder and register the asset.
    downloaded = False
    if file_path is None:
        if progress:
            progress(0.1, "Downloading clip…")
        try:
            result = download_instagram_url(
                url=source_url, output_dir=_candidate_download_dir(account_id)
            )
        except Exception as exc:  # noqa: BLE001 - surface a clean message
            raise ScrapingError(f"Download failed: {exc}") from exc
        file_path = str(result.file_path)
        downloaded = True
        # Perceptual fingerprint for cross-repost footage dedup; best-effort so a
        # fingerprinting failure never fails the scrape. With the hash present at
        # intake, accept_into_pool's content-dedup can fire immediately.
        content_hash = safe_video_fingerprint(Path(file_path))
        with get_session() as session:
            asset, _ = find_or_register_media_asset(
                session, source_url=source_url, shortcode=shortcode, platform="instagram"
            )
            mark_media_asset_downloaded(
                asset, original_download_path=file_path, content_hash=content_hash
            )
            session.commit()

    # 3. Create the Processing item and link the candidate to it.
    if progress:
        progress(0.9, "Adding to Processing…")
    with get_session() as session:
        candidate = session.get(ScrapeCandidate, candidate_id)
        item = DownloadItem(
            source_url=source_url,
            extractor="instagram",
            video_id=shortcode,
            title=title,
            source_description=description,
            file_path=file_path,
            status="completed",
            review_state="new",
            account_id=candidate.account_id if candidate is not None else account_id,
        )
        session.add(item)
        session.flush()
        if candidate is not None:
            candidate.queued_download_item_id = item.id
            candidate.state = "downloaded"
        item_id = item.id
        session.commit()

    if progress:
        progress(1.0, "Done")
    return {
        "item_id": item_id,
        "reused": reused,
        "downloaded": downloaded,
        "message": "Reused the existing file." if reused else "Downloaded and added to Processing.",
    }
