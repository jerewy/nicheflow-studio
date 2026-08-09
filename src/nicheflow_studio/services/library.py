"""Downloads / Library service (UI-independent).

Read + light-management view over downloaded items, extracted from the PyQt
Downloads page so the React Library screen can list items, (re)assign them to an
account, and remove them. Heavy acquisition (the download queue/retry) stays in
the PyQt app for now; this slice covers the library management actions.

Remove mirrors the PyQt cleanup (reset linked scrape candidates) and also clears
this item's draft revisions and unlinks its publish-queue rows so nothing dangles
at the new draft-revision foreign key.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

from sqlalchemy import or_, select

from nicheflow_studio.db.blocklist import block_asset
from nicheflow_studio.db.media_library import (
    find_media_asset,
    find_or_register_media_asset,
    mark_media_asset_downloaded,
)
from nicheflow_studio.processing.dedup import safe_video_fingerprint
from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    DraftRevision,
    PoolItem,
    ScrapeCandidate,
    UploadJob,
)
from nicheflow_studio.db.pools import REJECT_REASONS, remove_pool_items_for_asset
from nicheflow_studio.db.pools import reject_candidate as _reject_candidate_db
from nicheflow_studio.db.assignments import reject_assignments_for_item
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import publish_queue
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.downloader.failures import (
    looks_like_auth_or_rate_limit,
    looks_like_missing_source,
    looks_like_offline,
)
from nicheflow_studio.downloader.instagram import download_instagram_url
from nicheflow_studio.core.instagram_session import best_instagram_yt_dlp_cookiefile
from nicheflow_studio.core.paths import downloads_dir

_LIST_LIMIT = 100
# How recently an item must have been added to still read as "New".
_NEW_WINDOW_HOURS = 24
# On-demand first-fetches hit Instagram live and occasionally fail on a transient
# reset, timeout, or rate-limit blip. Retry the whole attempt a few times with a
# growing backoff before surfacing the error to the user — but never for a gone
# source, which won't recover. yt-dlp also retries at the network layer (see
# yt_dlp_sidecar); this outer loop adds the real backoff that helps IG throttling.
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_BACKOFF = 1.5  # seconds, multiplied by the attempt number
# Prefetch warms the next few not-yet-downloaded review clips in the background so
# opening one is instant. Kept small, sequential, and paced — bulk/parallel
# fetches invite the Instagram throttling we're trying to smooth over.
_PREFETCH_MAX = 5
_PREFETCH_GAP = 1.0  # seconds between real network fetches
# Guards against two background prefetch jobs fetching the same clip at once.
_prefetch_lock = threading.Lock()
_prefetching: set[int] = set()
# Serializes downloads of the *same* clip across all callers (a prefetch job vs the
# user clicking that clip) so two yt-dlp runs never write the same .part file — on
# Windows that races into "WinError 32 ... used by another process" and corrupts
# ffmpeg postprocessing. Keyed by source URL; the output filename derives from the
# clip, so same URL == same file on disk.
_download_locks_guard = threading.Lock()
_download_locks: dict[str, threading.Lock] = {}


def _download_lock_for(source_url: str) -> threading.Lock:
    with _download_locks_guard:
        lock = _download_locks.get(source_url)
        if lock is None:
            lock = threading.Lock()
            _download_locks[source_url] = lock
        return lock
# review_state values that mean the user set the item aside.
_SKIPPED_REVIEW_STATES = {"ignored", "skipped", "declined", "canceled", "cancelled", "rejected"}


class LibraryError(ServiceError):
    """Raised for invalid library operations (unknown item/account)."""


class SourceUnavailableError(LibraryError):
    """The clip's original can't be fetched because the source post is gone
    (deleted, made private, or removed by Instagram) — not a fault on our side."""


class SessionExpiredError(LibraryError):
    """Instagram blocked the download because the account session is expired or
    rate-limited — the clip itself is fine, so the user should re-login/wait, not
    reject it."""


# Shared with queue.py / pooling.py so every download path classifies failures
# the same way (transient session trouble vs permanently-gone source vs offline).
_looks_like_auth_or_rate_limit = looks_like_auth_or_rate_limit
_looks_like_missing_source = looks_like_missing_source
_looks_like_offline = looks_like_offline


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _derive_status(
    item: DownloadItem,
    posted_item_ids: set[int],
    failed_item_ids: set[int],
    scheduled_item_ids: set[int],
    cloud_item_ids: set[int],
    reopened_item_ids: set[int],
    cloud_pending_item_ids: set[int] | None = None,
) -> str:
    """Workflow status for the Processing table: posted > failed > cloud >
    cloud_pending > scheduled > rejected/skipped > exported > draft >
    pending_review > new.

    ``scheduled``/``cloud`` outrank ``pending_review`` on purpose: auto-distributed
    clips are exported AND queued for a future post in the background while their
    ``download_items.status`` is still ``pending_review`` (awaiting the user's
    review). The live schedule is the more useful thing to surface, so it wins —
    a clip with no queued post still reads ``pending_review`` as before. ``cloud``
    is the same as ``scheduled`` but handed off to the Cloudflare Worker.

    ``exported``/``draft`` outrank ``pending_review`` for the same reason.
    ``apply_revision`` writes the chosen option onto ``title_draft`` /
    ``caption_draft`` but leaves ``status`` at whatever distribution set, so a
    reel carrying a finished, applied draft kept reporting ``pending_review`` —
    it looked untouched in Processing while actually sitting one step from
    export, which is how a batch could stall unnoticed between Import and
    Finish. A distributed clip nobody has drafted yet still reads
    ``pending_review``, because it has neither draft text nor a processed file.

    The review_state check must come before all of these so that a clip rejected
    while still in ``pending_review`` surfaces as ``rejected``."""
    if item.id in posted_item_ids and item.id not in reopened_item_ids:
        return "posted"
    if item.id in failed_item_ids:
        return "failed"
    if item.id in cloud_item_ids:
        return "cloud"
    # A locally-'scheduled' job on a cloud-mapped account is cloud-bound, not on
    # the local browser path: list_due_jobs excludes those accounts outright, so
    # this reel will only ever be posted by the Worker. It reads 'scheduled' only
    # because its upload hasn't landed yet. Labelling it plain "Scheduled" made
    # that look like it had fallen back to local publishing.
    if cloud_pending_item_ids and item.id in cloud_pending_item_ids:
        return "cloud_pending"
    if item.id in scheduled_item_ids:
        return "scheduled"
    review_state = (item.review_state or "").lower()
    if review_state in _SKIPPED_REVIEW_STATES:
        return "rejected" if review_state == "rejected" else "skipped"
    if item.status in {"draft", "exported"}:
        return item.status
    if item.processed_path:
        return "exported"
    if item.title_draft or item.caption_draft:
        return "draft"
    if item.status == "pending_review":
        return "pending_review"
    return "new"


def account_sequence_map(session) -> dict[int, int]:
    """Per-account rank of each non-blocked item (oldest = 1), keyed by item id.

    This is the "#N" shown in the Processing/Library table. Computed over ALL
    items (not a windowed page) so the number is stable, and shared with the
    batch-draft handoff so a pasted reply that echoes "(#143)" routes to the
    same video the user saw. Replaces exposing the global, gap-ridden id.
    """
    seq_by_item: dict[int, int] = {}
    account_counters: dict[int | None, int] = {}
    for seq_id, seq_account_id in session.execute(
        select(DownloadItem.id, DownloadItem.account_id)
        .where(DownloadItem.review_state != "blocked")
        .order_by(DownloadItem.id.asc())
    ):
        account_counters[seq_account_id] = account_counters.get(seq_account_id, 0) + 1
        seq_by_item[seq_id] = account_counters[seq_account_id]
    return seq_by_item


def posted_job_id_map(session) -> dict[int, int]:
    """Newest posted UploadJob id per item, for items that have ever posted.

    Publishing records the outcome on UploadJob, not on DownloadItem.status —
    the legacy direct-download path never flips the item row — so this, not
    ``status == "posted"``, is what actually means "already went out".
    """
    posted_job_ids: dict[int, int] = {}
    for job_id, item_id in session.execute(
        select(UploadJob.id, UploadJob.download_item_id)
            .where(UploadJob.download_item_id.is_not(None))
            .where((UploadJob.posted_at.is_not(None)) | (UploadJob.status == "posted"))
    ):
        posted_job_ids[item_id] = max(job_id, posted_job_ids.get(item_id, 0))
    return posted_job_ids


def reopened_item_ids(session, posted_job_ids: dict[int, int]) -> set[int]:
    """Items that posted once but have a NEWER unposted job — deliberately
    reopened for another attempt, so they count as unposted again."""
    active_job_ids: dict[int, int] = {}
    for job_id, item_id in session.execute(
        select(UploadJob.id, UploadJob.download_item_id)
            .where(UploadJob.download_item_id.is_not(None))
            .where(UploadJob.posted_at.is_(None))
            .where(UploadJob.status != "posted")
    ):
        active_job_ids[item_id] = max(job_id, active_job_ids.get(item_id, 0))
    return {
        item_id
        for item_id, active_job_id in active_job_ids.items()
        if active_job_id > posted_job_ids.get(item_id, active_job_id)
        and item_id in posted_job_ids
    }


def already_posted_item_ids(session) -> set[int]:
    """Items that have posted and have not since been reopened.

    The single answer to "is this reel already out?", shared by the Processing
    table and the cross-account batch screen so the two cannot disagree.
    """
    posted_job_ids = posted_job_id_map(session)
    return set(posted_job_ids) - reopened_item_ids(session, posted_job_ids)


def posted_source_video_ids(session) -> set[str]:
    """``video_id`` of every reel that has already posted from ANY account.

    The same source reel can reach two accounts — the pool dedups on its own
    items, but clips pulled in through the legacy direct-download path were
    never pool items, so the pool had no record that another account already
    held them. Posting one reel from two accounts in the same network is a
    footprint risk, so the batch screen uses this to skip a clip whose footage
    is already out, whichever account published it.
    """
    posted = already_posted_item_ids(session)
    if not posted:
        return set()
    return {
        video_id
        for video_id in session.scalars(
            select(DownloadItem.video_id)
            .where(DownloadItem.id.in_(posted))
            .where(DownloadItem.video_id.is_not(None))
        ).all()
        if video_id
    }


def list_items(account_id: int | None = None, limit: int = _LIST_LIMIT) -> list[dict]:
    """Recent library items (newest first), with account name, derived workflow
    status, and a recency flag. Optionally filtered to one account."""
    with get_session() as session:
        names = {a.id: a.name for a in session.scalars(select(Account)).all()}
        posted_job_ids = posted_job_id_map(session)
        posted_item_ids = set(posted_job_ids)
        # Items whose publish attempt failed and was never revived — the table
        # must surface these so the user knows to republish.
        failed_item_ids = {
            row
            for row in session.scalars(
                select(UploadJob.download_item_id)
                .where(UploadJob.download_item_id.is_not(None))
                .where(UploadJob.posted_at.is_(None))
                .where(UploadJob.status == "failed")
            ).all()
            if row is not None
        }
        # Items queued for a future post (not yet posted) — surfaced as
        # "scheduled" so the table distinguishes them from merely-exported clips.
        # Split by publish path: on a cloud-mapped account a 'scheduled' job is
        # awaiting its Worker upload, never the local browser, so it gets the
        # distinct 'cloud_pending' status instead (see _derive_status).
        scheduled_rows = session.execute(
            select(UploadJob.download_item_id, UploadJob.account_id)
            .where(UploadJob.download_item_id.is_not(None))
            .where(UploadJob.posted_at.is_(None))
            .where(UploadJob.status == "scheduled")
        ).all()
        # Lazy import: keeps library importable without the cloud publisher's env.
        from nicheflow_studio.services import cloud_publisher

        cloud_mapped: dict[int | None, bool] = {}

        def _is_cloud_mapped(account_id: int | None) -> bool:
            if account_id not in cloud_mapped:
                cloud_mapped[account_id] = bool(
                    account_id is not None and cloud_publisher.cloud_account_key_for(account_id)
                )
            return cloud_mapped[account_id]

        scheduled_item_ids = {
            item_id
            for item_id, account_id in scheduled_rows
            if item_id is not None and not _is_cloud_mapped(account_id)
        }
        cloud_pending_item_ids = {
            item_id
            for item_id, account_id in scheduled_rows
            if item_id is not None and _is_cloud_mapped(account_id)
        }
        # Items handed off to the Cloudflare Worker (status 'cloud', not yet
        # posted) — surfaced distinctly so the table shows they publish via cloud.
        cloud_item_ids = {
            row
            for row in session.scalars(
                select(UploadJob.download_item_id)
                .where(UploadJob.download_item_id.is_not(None))
                .where(UploadJob.posted_at.is_(None))
                .where(UploadJob.status == "cloud")
            ).all()
            if row is not None
        }
        reopened = reopened_item_ids(session, posted_job_ids)
        query = (
            select(DownloadItem)
            # Globally-rejected ("blocked") items are hidden from Processing.
            .where(DownloadItem.review_state != "blocked")
            .order_by(DownloadItem.id.desc())
            .limit(limit)
        )
        if account_id is not None:
            query = query.where(DownloadItem.account_id == account_id)
        rows = session.scalars(query).all()
        # Per-account "#N" shown in the Processing table (see account_sequence_map).
        seq_by_item = account_sequence_map(session)
        now = dt.datetime.now(dt.timezone.utc)
        items = []
        for row in rows:
            created = row.created_at
            # "NEW" means recently added AND not yet opened.
            is_new = False
            if created is not None and row.seen_at is None:
                aware = created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc)
                is_new = (now - aware) <= dt.timedelta(hours=_NEW_WINDOW_HOURS)
            items.append(
                {
                    "id": row.id,
                    "account_seq": seq_by_item.get(row.id),
                    "title": row.title,
                    "source_url": row.source_url,
                    "status": _derive_status(
                        row,
                        posted_item_ids,
                        failed_item_ids,
                        scheduled_item_ids,
                        cloud_item_ids,
                        reopened,
                        cloud_pending_item_ids,
                    ),
                    "raw_status": row.status,
                    # A posted item that was manually reopened (a newer draft repost
                    # attempt exists). The UI offers "Posted" only for these, to undo
                    # the reopen — see services/publish_queue._revert_reopen_to_posted.
                    "reopened": row.id in reopened,
                    "review_state": row.review_state,
                    "file_path": row.file_path,
                    "has_file": bool(row.file_path),
                    "has_processed": bool(row.processed_path),
                    "has_draft": bool(row.title_draft or row.caption_draft),
                    "account_id": row.account_id,
                    "account_name": names.get(row.account_id) if row.account_id else None,
                    "created_at": _iso(row.created_at),
                    "is_new": is_new,
                }
            )
        return items


def assign_account(item_id: int, account_id: int | None) -> dict:
    """Assign (or clear, with ``None``) the account for a download item."""
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise LibraryError(f"No download item with id {item_id}.")
        account_name = None
        if account_id is not None:
            account = session.get(Account, account_id)
            if account is None:
                raise LibraryError(f"No account with id {account_id}.")
            account_name = account.name
        item.account_id = account_id
        session.commit()
        return {"item_id": item_id, "account_id": account_id, "account_name": account_name}


def remove_item(item_id: int) -> dict:
    """Remove a library item and tidy up its dependents.

    Resets linked scrape candidates (so they return to the candidate pool),
    deletes this item's draft revisions, and unlinks its publish-queue rows.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise LibraryError(f"No download item with id {item_id}.")

        for candidate in session.scalars(
            select(ScrapeCandidate).where(ScrapeCandidate.queued_download_item_id == item_id)
        ).all():
            candidate.queued_download_item_id = None
            if candidate.state in {"queued", "downloaded"}:
                candidate.state = "candidate"

        revisions = 0
        for revision in session.scalars(
            select(DraftRevision).where(DraftRevision.download_item_id == item_id)
        ).all():
            session.delete(revision)
            revisions += 1

        for job in session.scalars(
            select(UploadJob).where(UploadJob.download_item_id == item_id)
        ).all():
            job.download_item_id = None

        session.delete(item)
        session.commit()
        return {"removed_item_id": item_id, "deleted_revisions": revisions}


def _require_item(session, item_id: int) -> DownloadItem:
    item = session.get(DownloadItem, item_id)
    if item is None:
        raise LibraryError(f"No download item with id {item_id}.")
    return item


def _candidates_for_item(session, item: DownloadItem) -> list[ScrapeCandidate]:
    """The scrape candidate(s) this item came from: the one queued into it, else
    a same-account URL/shortcode match (for manually added clips)."""
    candidates = session.scalars(
        select(ScrapeCandidate).where(ScrapeCandidate.queued_download_item_id == item.id)
    ).all()
    if not candidates and item.account_id is not None:
        conds = [ScrapeCandidate.source_url == item.source_url]
        if item.video_id:
            conds.append(ScrapeCandidate.video_id == item.video_id)
        candidates = session.scalars(
            select(ScrapeCandidate).where(
                ScrapeCandidate.account_id == item.account_id, or_(*conds)
            )
        ).all()
    return list(candidates)


def mark_seen(item_id: int) -> None:
    """Record that an item has been opened in Processing (clears its NEW badge).
    Best-effort and idempotent — only sets ``seen_at`` the first time."""
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is not None and item.seen_at is None:
            item.seen_at = dt.datetime.now(dt.timezone.utc)
            session.commit()


def _existing_download_result(session, item_id: int, source_url: str, shortcode) -> dict | None:
    """Return a ``downloaded: False`` result if this clip is already on disk —
    directly on the item, or via a previously-downloaded global media asset — else
    ``None``. Shared by the lock-free fast path and the re-check under the per-clip
    download lock, so the two stay in sync."""
    item = session.get(DownloadItem, item_id)
    if item is None:
        return None
    if item.file_path and Path(item.file_path).exists():
        return {"item_id": item.id, "file_path": item.file_path, "downloaded": False}
    asset = find_media_asset(session, source_url=source_url, shortcode=shortcode)
    if asset is not None and asset.original_download_path and Path(asset.original_download_path).exists():
        item.file_path = asset.original_download_path
        item.status = "completed"
        session.commit()
        return {"item_id": item.id, "file_path": item.file_path, "downloaded": False}
    return None


def _retire_gone_source_for_url(
    *, source_url: str, shortcode: str | None, detail: str
) -> None:
    """Best-effort: pull a permanently-gone source out of pools/assignments.

    Only acts when the URL already has a registered media asset (pooled clips);
    a plain account-scoped item has nothing to retire.
    """
    from nicheflow_studio.queue import retire_gone_source

    try:
        with get_session() as session:
            asset = find_media_asset(session, source_url=source_url, shortcode=shortcode)
            asset_id = asset.id if asset is not None else None
        if asset_id is not None:
            retire_gone_source(
                media_asset_id=asset_id,
                source_url=source_url,
                shortcode=shortcode,
                detail=detail,
            )
    except Exception:  # noqa: BLE001 - cleanup must never mask the real error
        pass


def ensure_item_downloaded(item_id: int, *, downloader=None, progress=None) -> dict:
    """Materialize a pending-review item on first real use, globally deduped.

    Concurrent callers for the same clip (a background prefetch job and the user
    clicking that same clip) are serialized on a per-clip lock so two yt-dlp runs
    never write the same .part file — on Windows that races into "WinError 32 ...
    used by another process" and corrupt ffmpeg postprocessing. The second caller
    re-checks under the lock and reuses the finished file instead of re-fetching.

    ``progress`` is an optional ``(fraction, message)`` callback (the JobManager
    injects one) so the UI can show that a live Instagram fetch is running.
    """

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    with get_session() as session:
        item = _require_item(session, item_id)
        existing = _existing_download_result(session, item_id, item.source_url, item.video_id)
        if existing is not None:
            return existing
        source_url = item.source_url
        shortcode = item.video_id
        account_id = item.account_id

    output_dir = downloads_dir() / f"acc_{account_id}"

    def _attempt():
        if downloader is not None:
            # Tests inject a full downloader replacement that provides its own
            # behavior and signature; don't resolve real cookies for it.
            return downloader(url=source_url, output_dir=output_dir)
        # Download as a SOURCING account only — never the clip's own publishing
        # account. Authenticating downloads as a real/important account is what
        # gets it flagged for automation, so best_instagram_yt_dlp_cookiefile
        # ignores profile_name and uses the sourcing allowlist instead.
        return download_instagram_url(
            url=source_url,
            output_dir=output_dir,
            cookiefile=best_instagram_yt_dlp_cookiefile(),
        )

    report(0.05, "Preparing download…")
    with _download_lock_for(source_url):
        # Another thread (e.g. a prefetch job) may have finished this exact clip
        # while we waited for the lock — reuse it rather than start a second run.
        with get_session() as session:
            existing = _existing_download_result(session, item_id, source_url, shortcode)
            if existing is not None:
                report(1.0, "Already on disk.")
                return existing

        result = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                report(
                    0.15 + 0.2 * (attempt - 1),
                    "Downloading original from Instagram…"
                    if attempt == 1
                    else f"Retrying download (attempt {attempt}/{_DOWNLOAD_ATTEMPTS})…",
                )
                result = _attempt()
                break
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - translate raw downloader failures into a clean, actionable UI message
                # A gone source never recovers — surface it immediately, no retry.
                if _looks_like_missing_source(str(exc)):
                    _retire_gone_source_for_url(
                        source_url=source_url, shortcode=shortcode, detail=str(exc)[:200]
                    )
                    raise SourceUnavailableError(
                        "Instagram returned no media for this clip — the original was most "
                        "likely deleted or made private. Reject it to clear it from the queue. "
                        "(If lots of clips fail to open, your Instagram session may have "
                        "expired — re-login from the dashboard.)"
                    ) from exc
                # Expired session / rate-limit won't clear within our backoff, and
                # retrying only adds load — surface it immediately with the right fix.
                if _looks_like_auth_or_rate_limit(str(exc)):
                    raise SessionExpiredError(
                        "Instagram blocked this download — the account's session is likely "
                        "expired or rate-limited. Re-login this account from the dashboard, "
                        "or wait a few minutes and try again. Don't reject the clip; it's fine."
                    ) from exc
                # Transient (reset/timeout/DNS blip): back off and retry a few times
                # before giving up so a single network blip doesn't fail the open.
                if attempt >= _DOWNLOAD_ATTEMPTS:
                    # DNS/offline is our side of the wire — pointing the user at
                    # their Instagram session or the clip would be the wrong fix.
                    if _looks_like_offline(str(exc)):
                        raise LibraryError(
                            "No internet connection — the download couldn't reach "
                            "Instagram (DNS lookup failed). Check your network, then "
                            "reopen the clip. The clip and your Instagram session are "
                            "fine; don't reject it."
                        ) from exc
                    raise LibraryError(
                        "Couldn't fetch this clip's original from Instagram. Check your "
                        "connection and Instagram session, then try again — if it keeps "
                        "failing, reject the clip."
                    ) from exc
                time.sleep(_DOWNLOAD_RETRY_BACKOFF * attempt)

        assert result is not None  # loop returns a result or raises
        report(0.85, "Saving clip…")
        file_path = str(result.file_path)
        # Perceptual fingerprint for cross-repost footage dedup; best-effort so an
        # unreadable file never blocks opening the clip.
        content_hash = safe_video_fingerprint(Path(file_path))
        with get_session() as session:
            asset, _ = find_or_register_media_asset(
                session, source_url=source_url, shortcode=shortcode, platform="instagram"
            )
            mark_media_asset_downloaded(
                asset, original_download_path=file_path, content_hash=content_hash
            )
            for row in session.scalars(select(DownloadItem).where(DownloadItem.source_url == source_url)).all():
                if row.file_path is None:
                    row.file_path = file_path
                    row.status = "completed"
            session.commit()
        report(1.0, "Ready.")
        return {"item_id": item_id, "file_path": file_path, "downloaded": True}


def prefetch_items(item_ids: list[int], *, downloader=None) -> dict:
    """Best-effort warm upcoming review clips' originals in the background so the
    next open is instant instead of a live Instagram fetch on click.

    Sequential and paced to stay gentle on Instagram (parallel/bulk fetches invite
    throttling). Per-item failures are swallowed — the on-open path still surfaces
    a real error for the clip the user actually clicks. Returns how many clips were
    freshly downloaded.
    """
    ids = [int(i) for i in item_ids][:_PREFETCH_MAX]
    warmed = 0
    for index, item_id in enumerate(ids):
        # Skip a clip another prefetch job is already fetching — don't double-hit
        # Instagram for the same original.
        with _prefetch_lock:
            if item_id in _prefetching:
                continue
            _prefetching.add(item_id)
        fetched = False
        try:
            result = ensure_item_downloaded(item_id, downloader=downloader)
            fetched = bool(result.get("downloaded"))
            if fetched:
                warmed += 1
        except Exception:  # noqa: BLE001 - prefetch is best-effort, never raise
            fetched = True  # a real attempt happened (and failed) — still pace
        finally:
            with _prefetch_lock:
                _prefetching.discard(item_id)
        # Pace only real network fetches; a cached clip returns instantly, no wait.
        if fetched and index + 1 < len(ids):
            time.sleep(_PREFETCH_GAP)
    return {"warmed": warmed, "requested": len(ids)}


def remove_item_from_pool(item_id: int, reason: str = "manual removal") -> dict:
    """Reversibly pull this clip's footage out of every niche pool it reached.

    Processing is where niche fit is judged, so this is the cleanup when a clip
    shouldn't distribute. Reversible from Pool & Distribute. Returns how many
    pool items were removed (0 if the footage was never pooled).
    """
    with get_session() as session:
        item = _require_item(session, item_id)
        removed = 0
        asset = find_media_asset(session, source_url=item.source_url, shortcode=item.video_id)
        if asset is not None:
            removed = remove_pool_items_for_asset(
                session, media_asset_id=asset.id, reason=reason
            )
        session.commit()
        return {"item_id": item_id, "removed_pool_items": removed}


def reject_item(item_id: int, reason: str = "low_quality") -> dict:
    """Reject a clip from Processing: reject its originating candidate(s), pull
    its footage from the pool, and mark the item skipped.

    For an off-niche clip, or one that slipped past dedup. ``reason`` is a
    :data:`REJECT_REASONS` key. Candidate rejection is scoped to this item's
    account. Every effect is reversible (restore the pool item / candidate).
    Returns the counts changed.
    """
    key = (reason or "").strip().lower()
    if key not in REJECT_REASONS:
        raise LibraryError(
            f"Unknown reject reason {reason!r}. Use one of {sorted(REJECT_REASONS)}."
        )

    # Stop the reel from posting before touching review state: cancel any pending
    # schedule/cloud job for this clip so a rejected item can't still auto-post
    # from the Worker. Fails closed — a cloud cancel that can't be confirmed
    # aborts the reject rather than leaving the cloud copy live. Reversible: jobs
    # drop back to 'draft', matching Multi-Account Publish's "Remove from schedule".
    unscheduled_jobs = publish_queue.unschedule_jobs_for_item(item_id)

    with get_session() as session:
        item = _require_item(session, item_id)

        candidates = _candidates_for_item(session, item)
        for candidate in candidates:
            _reject_candidate_db(session, candidate=candidate, reason=key)

        removed = 0
        asset = find_media_asset(session, source_url=item.source_url, shortcode=item.video_id)
        if asset is not None:
            removed = remove_pool_items_for_asset(
                session, media_asset_id=asset.id, reason=f"item rejected: {key}"
            )

        # Surface the decision on the item: 'rejected' reads as 'skipped' in the
        # Processing list (see _derive_status / _SKIPPED_REVIEW_STATES).
        item.review_state = "rejected"
        released_assignments = reject_assignments_for_item(session, item)
        session.commit()
        return {
            "item_id": item_id,
            "rejected_candidates": len(candidates),
            "removed_pool_items": removed,
            "review_state": item.review_state,
            "released_assignments": released_assignments,
            "unscheduled_jobs": len(unscheduled_jobs),
        }


def reject_item_globally(item_id: int, reason: str = "globally rejected") -> dict:
    """Globally reject a clip — the strong "never see this again" action.

    Blocklists the footage (so future scrapes/dedup never re-pool it), removes it
    from every niche pool, drops its assignments across all accounts, rejects its
    originating candidate(s), and hides the item from Processing by marking it
    ``blocked``. Reversible in the sense that the row + local file are kept; the
    footage just won't distribute or re-enter the pool. Returns the counts changed.
    """
    clean_reason = (reason or "globally rejected").strip()[:256]

    # Same as reject_item: kill any pending schedule/cloud job first so a globally
    # rejected clip can't still auto-post. Fails closed on an unconfirmed cloud
    # cancel; reversible (jobs drop to 'draft').
    unscheduled_jobs = publish_queue.unschedule_jobs_for_item(item_id)

    with get_session() as session:
        item = _require_item(session, item_id)

        removed_pool = 0
        dropped_assignments = 0
        asset = find_media_asset(session, source_url=item.source_url, shortcode=item.video_id)
        if asset is not None:
            removed_pool = remove_pool_items_for_asset(
                session, media_asset_id=asset.id, reason=f"globally rejected: {clean_reason}"
            )
            pool_item_ids = [
                pool_item.id
                for pool_item in session.scalars(
                    select(PoolItem).where(PoolItem.media_asset_id == asset.id)
                ).all()
            ]
            if pool_item_ids:
                for assignment in session.scalars(
                    select(Assignment).where(Assignment.pool_item_id.in_(pool_item_ids))
                ).all():
                    session.delete(assignment)
                    dropped_assignments += 1

        # Record the footage so it can never be pooled again.
        block_asset(
            session,
            source_url=item.source_url,
            shortcode=item.video_id,
            reason=clean_reason,
        )

        for candidate in _candidates_for_item(session, item):
            candidate.state = "rejected_low_quality"

        # Hide the item from the Processing table (list_items filters 'blocked').
        item.review_state = "blocked"
        session.commit()
        return {
            "item_id": item_id,
            "removed_pool_items": removed_pool,
            "dropped_assignments": dropped_assignments,
            "review_state": "blocked",
            "blocked": True,
            "unscheduled_jobs": len(unscheduled_jobs),
        }
