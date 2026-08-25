from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nicheflow_studio.core.paths import downloads_dir
from nicheflow_studio.db.assignments import (
    ASSIGNMENT_STATUS_SKIPPED_DUPLICATE,
    delete_pending_review_items_for_asset,
    fail_assignments_for_source_gone,
    pending_download_assignments,
)
from nicheflow_studio.db.blocklist import block_asset
from nicheflow_studio.db.media_library import (
    find_media_asset,
    find_or_register_media_asset,
    mark_media_asset_downloaded,
    mark_media_asset_unavailable,
)
from nicheflow_studio.db.models import Assignment, DownloadItem, MediaAsset
from nicheflow_studio.db.pools import (
    find_niche_content_duplicate,
    flag_pool_item_duplicate,
    remove_pool_items_for_asset,
)
from nicheflow_studio.db.session import get_session
from nicheflow_studio.downloader.failures import looks_like_missing_source
from nicheflow_studio.downloader.instagram import (
    download_instagram_url,
    instagram_shortcode_from_url,
)
from nicheflow_studio.downloader.youtube import download_youtube_url


@dataclass(frozen=True)
class QueueConfig:
    max_workers: int = 2


_logger = logging.getLogger(__name__)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _sanitize_error_message(exc: Exception) -> str:
    message = _ANSI_ESCAPE_RE.sub("", str(exc)).strip()
    if not message:
        return exc.__class__.__name__

    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if not first_line:
        first_line = exc.__class__.__name__

    normalized = first_line.removeprefix("ERROR:").strip()
    lowered = normalized.lower()

    if "video unavailable" in lowered:
        return "This video is unavailable on YouTube."
    if "private video" in lowered:
        return "This video is private and cannot be downloaded."
    if "sign in to confirm your age" in lowered or "confirm your age" in lowered:
        return "This video requires an age-confirmed YouTube session."
    if "members-only" in lowered or "join this channel" in lowered:
        return "This video is members-only and cannot be downloaded with the current setup."
    if "requested format is not available" in lowered:
        return "This video format is not available right now. Try updating yt-dlp."
    if "unable to download video data" in lowered or "http error 403" in lowered:
        return "YouTube blocked the download request. Try updating yt-dlp."
    if "ffmpeg is not installed" in lowered:
        return "ffmpeg is not installed."
    if "instaloader is not installed" in lowered:
        return "Legacy Instagram metadata extraction is unavailable. Use the Apify path instead."
    if "please wait a few minutes" in lowered or "too many requests" in lowered:
        return "Instagram rate-limited the request. Wait a few minutes and try again."

    return normalized[:200]


def _download_url(*, url: str):
    if instagram_shortcode_from_url(url) is not None:
        return download_instagram_url(url=url, output_dir=downloads_dir() / "instagram")
    return download_youtube_url(url=url, output_dir=downloads_dir())


def _file_size_bytes(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _register_downloaded_media_asset(session, *, url: str, result) -> None:
    """Record a successful Instagram download in the Global Media Library.

    Idempotent by shortcode/URL, so re-downloading the same reel (e.g. for a
    second account) reuses the one asset instead of creating duplicates. Only
    Instagram originals enter the pantry — YouTube is a legacy intake path and
    not part of the shared-pool network.
    """
    if instagram_shortcode_from_url(url) is None:
        return
    asset, _created = find_or_register_media_asset(
        session, source_url=url, shortcode=result.video_id, platform="instagram"
    )
    # Perceptual fingerprint for cross-repost content dedup. Best-effort: a
    # failure here must never break the download or asset registration.
    content_hash = None
    try:
        from nicheflow_studio.processing.dedup import compute_video_fingerprint

        content_hash = compute_video_fingerprint(Path(result.file_path))
    except Exception:  # noqa: BLE001
        content_hash = None
    mark_media_asset_downloaded(
        asset,
        original_download_path=str(result.file_path),
        file_size_bytes=_file_size_bytes(Path(result.file_path)),
        content_hash=content_hash,
    )


def _safe_fingerprint(path: Path) -> str | None:
    """Perceptual fingerprint, best-effort (never raises)."""
    try:
        from nicheflow_studio.processing.dedup import compute_video_fingerprint

        return compute_video_fingerprint(path)
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class AssignmentDownloadSummary:
    downloaded: int
    reused: int
    failed: int
    duplicates: int
    errors: tuple[str, ...]
    # Assets whose source post is permanently gone: pulled from pools and their
    # assignments released so the next Distribute refills the freed slots.
    unavailable: int = 0


def download_assigned_pending(
    *,
    niche: str | None = None,
    downloader: Callable[[str], object] | None = None,
    fingerprinter: Callable[[Path], str | None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> AssignmentDownloadSummary:
    """Download originals for assigned clips that aren't on disk yet.

    Candidate-first means most of the assignment backlog is ``pending``; this
    fetches only assigned-but-pending assets (download-once, reference-many),
    marks each downloaded, and records a perceptual fingerprint. The fingerprint
    then powers footage dedup (Phase B): if a freshly downloaded clip is the same
    footage as one already in the niche pool (a re-branded repost), its
    assignment is marked ``skipped_duplicate`` and its pool item flagged
    duplicate, so the next Distribute refills the slot. An asset already on disk
    is reused. ``downloader``/``fingerprinter``/``progress`` are injectable so the
    orchestration is testable without hitting the network.
    """
    download = downloader or (lambda url: _download_url(url=url))
    fingerprint = fingerprinter or _safe_fingerprint

    with get_session() as session:
        targets = pending_download_assignments(session, niche=niche)

    downloaded = reused = failed = duplicates = unavailable = 0
    errors: list[str] = []
    total = len(targets)
    for index, target in enumerate(targets):
        if progress is not None:
            progress(index, total)
        try:
            with get_session() as session:
                asset = session.get(MediaAsset, target.media_asset_id)
                if asset is None:
                    continue
                already_on_disk = (
                    asset.download_status == "downloaded"
                    and bool(asset.original_download_path)
                    and Path(asset.original_download_path).exists()
                )
            if already_on_disk:
                reused += 1
                continue

            result = download(target.source_url)
            file_path = Path(str(result.file_path))
            content_hash = fingerprint(file_path)
            with get_session() as session:
                asset = session.get(MediaAsset, target.media_asset_id)
                if asset is None:
                    continue
                mark_media_asset_downloaded(
                    asset,
                    original_download_path=str(file_path),
                    file_size_bytes=_file_size_bytes(file_path),
                    content_hash=content_hash,
                )
                # Phase B: footage dedup against what's already in the niche pool.
                dup_asset_id = (
                    find_niche_content_duplicate(
                        session,
                        niche=target.niche,
                        content_hash=content_hash,
                        exclude_asset_id=asset.id,
                    )
                    if content_hash
                    else None
                )
                if dup_asset_id is not None:
                    flag_pool_item_duplicate(
                        session,
                        media_asset_id=asset.id,
                        niche=target.niche,
                        reason=f"duplicate footage of asset #{dup_asset_id}",
                    )
                    for assignment in (
                        session.query(Assignment)
                        .filter(Assignment.id.in_(target.assignment_ids))
                        .all()
                    ):
                        assignment.status = ASSIGNMENT_STATUS_SKIPPED_DUPLICATE
                    session.commit()
                    duplicates += 1
                    continue
                session.commit()
            downloaded += 1
        except Exception as exc:  # noqa: BLE001
            if looks_like_missing_source(str(exc)):
                # Permanent: the post is deleted/private. Retiring the asset
                # (instead of leaving the assignment stuck "assigned" forever)
                # frees the account slot so the next Distribute refills it, and
                # the blocklist keeps a re-scrape from pooling the dead reel.
                retire_gone_source(
                    media_asset_id=target.media_asset_id,
                    source_url=target.source_url,
                    shortcode=target.shortcode,
                    detail=_sanitize_error_message(exc),
                )
                unavailable += 1
                errors.append(
                    f"{target.shortcode or target.source_url}: source is gone — "
                    "removed from the pool; re-run Distribute to refill the slot."
                )
                continue
            failed += 1
            errors.append(
                f"{target.shortcode or target.source_url}: {_sanitize_error_message(exc)}"
            )
    if progress is not None:
        progress(total, total)
    return AssignmentDownloadSummary(
        downloaded=downloaded,
        reused=reused,
        failed=failed,
        duplicates=duplicates,
        errors=tuple(errors),
        unavailable=unavailable,
    )


def retire_gone_source(
    *,
    media_asset_id: int,
    source_url: str | None,
    shortcode: str | None,
    detail: str,
) -> None:
    """Pull a permanently-gone source out of circulation (best-effort).

    Marks the asset unavailable, removes its pool items, releases its active
    assignments, deletes the untouched pending-review Processing rows those
    assignments created, and blocklists its dedup keys. Never raises: the
    download loop must keep processing the remaining clips.
    """
    try:
        with get_session() as session:
            asset = session.get(MediaAsset, media_asset_id)
            if asset is None:
                return
            mark_media_asset_unavailable(asset)
            remove_pool_items_for_asset(
                session, media_asset_id=media_asset_id, reason=f"source gone: {detail}"
            )
            fail_assignments_for_source_gone(session, media_asset_id=media_asset_id)
            # Releasing the assignment is not enough: distribution already
            # exposed the clip in Processing, and a row with no downloadable
            # footage is undraftable dead weight that makes the account look
            # short of what Distribute reported.
            delete_pending_review_items_for_asset(session, media_asset_id=media_asset_id)
            block_asset(
                session,
                source_url=source_url,
                shortcode=shortcode,
                reason=f"source gone: {detail}",
            )
            session.commit()
    except Exception:  # noqa: BLE001 - cleanup is best-effort by design
        _logger.exception("Retiring gone source for asset %s failed", media_asset_id)


class QueueManager:
    _executor = ThreadPoolExecutor(max_workers=QueueConfig().max_workers)

    @classmethod
    def enqueue_download(
        cls,
        url: str,
        callback: Callable[[DownloadItem], None] | None = None,
        account_id: int | None = None,
        source_description: str | None = None,
    ) -> int:
        shortcode = instagram_shortcode_from_url(url)
        with get_session() as session:
            if shortcode is not None:
                asset = find_media_asset(session, source_url=url, shortcode=shortcode)
                if (
                    asset is not None
                    and asset.download_status == "downloaded"
                    and asset.original_download_path
                    and Path(asset.original_download_path).exists()
                ):
                    item = DownloadItem(
                        source_url=url,
                        extractor="instagram",
                        video_id=asset.source_shortcode or shortcode,
                        title=Path(asset.original_download_path).stem,
                        file_path=asset.original_download_path,
                        status="downloaded",
                        account_id=account_id,
                        source_description=source_description,
                    )
                    session.add(item)
                    session.commit()
                    item_id = item.id
                    if callback is not None:
                        callback(item)
                    return item_id

            item = DownloadItem(
                source_url=url,
                status="queued",
                account_id=account_id,
                source_description=source_description,
            )
            session.add(item)
            session.commit()
            item_id = item.id

        cls._executor.submit(cls._run_download, item_id, url, callback)
        return item_id

    @classmethod
    def retry_item(cls, item_id: int) -> bool:
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is None:
                return False
            source_url = item.source_url
            item.status = "queued"
            item.file_path = None
            item.extractor = None
            item.video_id = None
            item.title = None
            item.error_message = None
            session.commit()

        cls._executor.submit(cls._run_download, item_id, source_url, None)
        return True

    @classmethod
    def _run_download(
        cls, item_id: int, url: str, callback: Callable[[DownloadItem], None] | None
    ) -> None:
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is None:
                _logger.warning("download item %s disappeared before processing", item_id)
                return
            item.status = "downloading"
            session.commit()

        try:
            result = _download_url(url=url)
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None:
                    _logger.warning("download item %s missing after download", item_id)
                    return
                item.extractor = result.extractor
                item.video_id = result.video_id
                item.title = result.title
                item.file_path = str(result.file_path)
                item.error_message = None
                item.status = "downloaded"
                _register_downloaded_media_asset(session, url=url, result=result)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            _logger.exception("download failed for %s", url)
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None:
                    return
                item.status = "failed"
                item.error_message = _sanitize_error_message(exc)
                session.commit()
        finally:
            if callback:
                with get_session() as session:
                    item = session.get(DownloadItem, item_id)
                    if item:
                        callback(item)
