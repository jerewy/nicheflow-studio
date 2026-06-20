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
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.downloader.instagram import download_instagram_url
from nicheflow_studio.core.paths import downloads_dir

_LIST_LIMIT = 100
# How recently an item must have been added to still read as "New".
_NEW_WINDOW_HOURS = 24
# review_state values that mean the user set the item aside.
_SKIPPED_REVIEW_STATES = {"ignored", "skipped", "declined", "canceled", "cancelled", "rejected"}


class LibraryError(ServiceError):
    """Raised for invalid library operations (unknown item/account)."""


class SourceUnavailableError(LibraryError):
    """The clip's original can't be fetched because the source post is gone
    (deleted, made private, or removed by Instagram) — not a fault on our side."""


# yt-dlp reports a removed/private/deleted Instagram post with one of these phrases.
# Recognising them lets the UI show a clean "reject this clip" prompt instead of a
# generic "Unexpected error". Lowercase for case-insensitive matching. Note that
# "empty media response" can *also* mean an expired session, so the message hedges
# toward re-login when many clips fail at once.
_SOURCE_GONE_MARKERS = (
    "empty media response",
    "the post may have been deleted",
    "content isn't available",
    "requested content was not found",
    "video unavailable",
    "this account is private",
)


def _looks_like_missing_source(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _SOURCE_GONE_MARKERS)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _derive_status(
    item: DownloadItem,
    posted_item_ids: set[int],
    failed_item_ids: set[int],
    scheduled_item_ids: set[int],
    cloud_item_ids: set[int],
    reopened_item_ids: set[int],
) -> str:
    """Workflow status for the Processing table: posted > failed > cloud >
    scheduled > pending_review > skipped > exported > draft > new.

    ``scheduled``/``cloud`` outrank ``pending_review`` on purpose: auto-distributed
    clips are exported AND queued for a future post in the background while their
    ``download_items.status`` is still ``pending_review`` (awaiting the user's
    review). The live schedule is the more useful thing to surface, so it wins —
    a clip with no queued post still reads ``pending_review`` as before. ``cloud``
    is the same as ``scheduled`` but handed off to the Cloudflare Worker."""
    if item.id in posted_item_ids and item.id not in reopened_item_ids:
        return "posted"
    if item.id in failed_item_ids:
        return "failed"
    if item.id in cloud_item_ids:
        return "cloud"
    if item.id in scheduled_item_ids:
        return "scheduled"
    if item.status == "pending_review":
        return "pending_review"
    if item.status in {"draft", "exported"}:
        return item.status
    if (item.review_state or "").lower() in _SKIPPED_REVIEW_STATES:
        return "skipped"
    if item.processed_path:
        return "exported"
    if item.title_draft or item.caption_draft:
        return "draft"
    return "new"


def list_items(account_id: int | None = None, limit: int = _LIST_LIMIT) -> list[dict]:
    """Recent library items (newest first), with account name, derived workflow
    status, and a recency flag. Optionally filtered to one account."""
    with get_session() as session:
        names = {a.id: a.name for a in session.scalars(select(Account)).all()}
        posted_job_ids: dict[int, int] = {}
        for job_id, item_id in session.execute(
            select(UploadJob.id, UploadJob.download_item_id)
                .where(UploadJob.download_item_id.is_not(None))
                .where((UploadJob.posted_at.is_not(None)) | (UploadJob.status == "posted"))
        ):
            posted_job_ids[item_id] = max(job_id, posted_job_ids.get(item_id, 0))
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
        scheduled_item_ids = {
            row
            for row in session.scalars(
                select(UploadJob.download_item_id)
                .where(UploadJob.download_item_id.is_not(None))
                .where(UploadJob.posted_at.is_(None))
                .where(UploadJob.status == "scheduled")
            ).all()
            if row is not None
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
        active_job_ids: dict[int, int] = {}
        for job_id, item_id in session.execute(
            select(UploadJob.id, UploadJob.download_item_id)
                .where(UploadJob.download_item_id.is_not(None))
                .where(UploadJob.posted_at.is_(None))
                .where(UploadJob.status != "posted")
        ):
            active_job_ids[item_id] = max(job_id, active_job_ids.get(item_id, 0))
        reopened_item_ids = {
            item_id
            for item_id, active_job_id in active_job_ids.items()
            if active_job_id > posted_job_ids.get(item_id, active_job_id)
            and item_id in posted_job_ids
        }
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
        # Per-account sequence numbers shown as "#N" in the Processing table.
        # An item's number is its rank among its own account's (non-blocked)
        # items, oldest = 1, so each niche reads as a clean running count: the
        # newest item's number == how many clips that account has accumulated.
        # Computed over ALL items (not just the loaded window) so the number is
        # stable and accurate even though the list is capped at `limit`. This
        # replaces exposing the global, gap-ridden primary key.
        seq_by_item: dict[int, int] = {}
        account_counters: dict[int | None, int] = {}
        for seq_id, seq_account_id in session.execute(
            select(DownloadItem.id, DownloadItem.account_id)
            .where(DownloadItem.review_state != "blocked")
            .order_by(DownloadItem.id.asc())
        ):
            account_counters[seq_account_id] = account_counters.get(seq_account_id, 0) + 1
            seq_by_item[seq_id] = account_counters[seq_account_id]
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
                        reopened_item_ids,
                    ),
                    "raw_status": row.status,
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


def ensure_item_downloaded(item_id: int, *, downloader=None) -> dict:
    """Materialize a pending-review item on first real use, globally deduped."""
    with get_session() as session:
        item = _require_item(session, item_id)
        if item.file_path and Path(item.file_path).exists():
            return {"item_id": item.id, "file_path": item.file_path, "downloaded": False}
        asset = find_media_asset(session, source_url=item.source_url, shortcode=item.video_id)
        if asset is not None and asset.original_download_path and Path(asset.original_download_path).exists():
            item.file_path = asset.original_download_path
            item.status = "completed"
            session.commit()
            return {"item_id": item.id, "file_path": item.file_path, "downloaded": False}
        source_url = item.source_url
        shortcode = item.video_id
        account_id = item.account_id

    fetch = downloader or download_instagram_url
    try:
        result = fetch(url=source_url, output_dir=downloads_dir() / f"acc_{account_id}")
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 - translate raw downloader failures into a clean, actionable UI message
        if _looks_like_missing_source(str(exc)):
            raise SourceUnavailableError(
                "Instagram returned no media for this clip — the original was most "
                "likely deleted or made private. Reject it to clear it from the queue. "
                "(If lots of clips fail to open, your Instagram session may have "
                "expired — re-login from the dashboard.)"
            ) from exc
        raise LibraryError(
            "Couldn't fetch this clip's original from Instagram. Check your "
            "connection and Instagram session, then try again — if it keeps "
            "failing, reject the clip."
        ) from exc
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
    return {"item_id": item_id, "file_path": file_path, "downloaded": True}


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
        }
