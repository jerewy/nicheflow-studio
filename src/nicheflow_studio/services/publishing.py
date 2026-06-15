"""Publish-queue handoff for the Processing screen (UI-independent).

Extraction of the PyQt "add this export to the publish queue / schedule it" path
(MainWindow._upsert_publish_job_for_export). It creates or updates an
:class:`UploadJob` for an item's exported file so the existing Publish Queue,
scheduler, and Playwright publisher pick it up unchanged.

Scope: this wires the *safe* DB handoff (queue + schedule). Triggering the actual
Instagram post (Playwright "publish now") is intentionally left to the existing
desktop flow for now — it is a real, irreversible network action and should not
be one click away in an early migration slice.
"""

from __future__ import annotations

import datetime as dt
import random as _random

from sqlalchemy import select

from nicheflow_studio.core.scheduling import (
    catch_up_slot_time,
    most_recent_passed_slot_time,
    next_open_slot_time,
)
from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

_DEFAULT_TIMEZONE = "Asia/Bangkok"
_DEFAULT_PRIVACY = "private"
_ITEM_LIST_LIMIT = 50
# Minimum spacing between two posts on the SAME account. Used both when
# auto-scheduling a new post and when deferring a due post that would otherwise
# land too soon after the account's last post (reach cannibalization / bot tell).
SAME_ACCOUNT_MIN_GAP_HOURS = 4


class PublishError(ServiceError):
    """Raised for invalid publish-queue operations (not exported, no account…)."""


def _iso(value: dt.datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset (naive DB values are UTC).
    A naive string would be parsed as LOCAL time by the frontend's Date()."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def _parse_scheduled_at(value: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 time to an aware UTC datetime.

    The React UI now sends a UTC-offset string (e.g. ``"2026-06-09T05:05:00+00:00"``)
    so timezone conversion is a no-op for normal callers.  A naive value (no offset)
    is still accepted for backwards-compat and treated as the machine's local timezone.
    """
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PublishError(f"Invalid scheduled time: {value!r}") from exc
    # aware → convert to UTC; naive → assume local timezone (legacy path).
    return parsed.astimezone(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


def _account_in_checkpoint_cooldown(account_id: int, *, now: dt.datetime) -> bool:
    # Lazy import avoids the publish_now -> publishing module dependency cycle.
    from nicheflow_studio.services.publish_now import _account_on_cooldown

    return _account_on_cooldown(account_id, now=now.astimezone(dt.timezone.utc))


def next_safe_slot_for_account(
    session,
    account: Account,
    *,
    after: dt.datetime,
    exclude_job_id: int | None = None,
    rng: _random.Random | None = None,
    fallback_gap: bool = True,
) -> dt.datetime | None:
    """Next post time for an account that respects its slots AND keeps a
    ``SAME_ACCOUNT_MIN_GAP_HOURS`` gap from every existing same-account post
    (scheduled or already posted).

    Walks the account's configured slots forward from ``after`` and returns the
    first that no existing post sits within the gap of. ``exclude_job_id`` drops a
    job's own time from the occupied set (so deferring a job never collides with
    itself).

    With no usable slot: ``fallback_gap`` True returns ``latest_occupied + gap``
    (or ``after``) so an unattended defer always gets a concrete time; False
    returns ``None`` so the caller can surface a "configure your slots" message.
    """
    gap = dt.timedelta(hours=SAME_ACCOUNT_MIN_GAP_HOURS)
    jobs = session.scalars(
        select(UploadJob).where(UploadJob.account_id == account.id)
    ).all()
    occupied = [
        _aware(value)
        for row in jobs
        if row.id != exclude_job_id
        for value in (row.scheduled_at, row.posted_at)
        if value is not None
    ]
    slot = next_open_slot_time(
        account.upload_schedule_slots,
        after=after,
        occupied=occupied,
        rng=rng,
        collision_minutes=SAME_ACCOUNT_MIN_GAP_HOURS * 60,
    )
    if slot is not None:
        return slot
    if not fallback_gap:
        return None
    # No slots configured -> just enforce the raw gap from the latest known post.
    latest = max(occupied, default=None)
    base = (latest + gap) if latest is not None else after
    return max(base, after)


def list_items() -> list[dict]:
    """Recent downloaded items that can be worked on (have a local file)."""
    with get_session() as session:
        rows = session.scalars(
            select(DownloadItem)
            .where(DownloadItem.file_path.is_not(None))
            .order_by(DownloadItem.id.desc())
            .limit(_ITEM_LIST_LIMIT)
        ).all()
        return [
            {
                "id": row.id,
                "title": row.title,
                "source_url": row.source_url,
                "account_id": row.account_id,
                "status": row.status,
                "has_processed": bool(row.processed_path),
                "has_draft": bool(row.title_draft or row.caption_draft),
            }
            for row in rows
        ]


def list_publish_jobs(item_id: int) -> list[dict]:
    """Publish-queue rows linked to an item, newest first."""
    with get_session() as session:
        rows = session.scalars(
            select(UploadJob)
            .where(UploadJob.download_item_id == item_id)
            .order_by(UploadJob.id.desc())
        ).all()
        return [
            {
                "id": row.id,
                "status": row.status,
                "title": row.title,
                "scheduled_at": _iso(row.scheduled_at),
                "posted_at": _iso(row.posted_at),
                "posted_url": row.posted_url,
                "error_message": row.error_message,
                "processed_path": row.processed_path,
            }
            for row in rows
        ]


def _handoff_scheduled_job_to_cloud(job_id: int) -> str | None:
    """Push a scheduled job for a cloud-mapped account to the Cloudflare Worker.

    Returns ``"cloud"`` when the job was handed off — so the caller flips its
    result status and the local publish loop (which only acts on
    ``status == "scheduled"``) skips it. Returns ``None`` when no handoff applies
    (cloud not configured, account not mapped, or not a scheduled post). On a hard
    failure it marks the job ``failed`` and raises :class:`PublishError`; a
    duplicate (already on the Worker) is treated as success so re-scheduling is
    idempotent.
    """
    from nicheflow_studio.services import cloud_publisher

    if not cloud_publisher.is_configured():
        return None
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is None or job.status != "scheduled" or job.scheduled_at is None:
            return None
        worker_key = cloud_publisher.cloud_account_key_for(job.account_id)
        if not worker_key:
            return None
        video = job.processed_path
        caption = job.description or ""
        scheduled_iso = _aware(job.scheduled_at).isoformat()

    # Network upload to the Worker — outside the DB session.
    try:
        cloud_publisher.schedule_reel(
            external_id=f"nf-{job_id}",
            account_key=worker_key,
            caption=caption,
            scheduled_at=scheduled_iso,
            video_path=video,
        )
    except cloud_publisher.CloudPublisherError as exc:
        if "already exists" not in str(exc).lower():
            with get_session() as session:
                job = session.get(UploadJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error_message = f"Cloud handoff failed: {exc}"[:512]
                    session.commit()
            raise PublishError(f"Cloud handoff failed: {exc}") from exc

    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is not None and job.status == "scheduled":
            job.status = "cloud"
            job.error_message = None
            session.commit()
    return "cloud"


def queue_for_publish(item_id: int, *, scheduled_at: str | None = None) -> dict:
    """Add (or update) the item's exported reel in the publish queue.

    Requires the item to be exported (``processed_path`` set) and assigned to an
    account. With ``scheduled_at`` the job is ``scheduled``; otherwise it is a
    ``draft``. Mirrors the PyQt upsert: an existing non-posted job for the same
    account+file is updated, an already-posted one is rejected.
    """
    scheduled = _parse_scheduled_at(scheduled_at)

    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise PublishError(f"No download item with id {item_id}.")
        if not item.processed_path:
            raise PublishError("Export the reel before adding it to the publish queue.")
        if item.account_id is None:
            raise PublishError("Assign this item to an account before publishing.")
        account = session.get(Account, item.account_id)
        if account is None:
            raise PublishError("The item's account no longer exists.")

        title = (item.title_draft or item.title or "").strip() or None
        description = item.caption_draft
        status = "scheduled" if scheduled is not None else "draft"
        path_text = item.processed_path

        already_posted = session.scalars(
            select(UploadJob)
            .where(UploadJob.account_id == account.id)
            .where(UploadJob.processed_path == path_text)
            .where((UploadJob.posted_at.is_not(None)) | (UploadJob.status == "posted"))
            .order_by(UploadJob.id.desc())
            .limit(1)
        ).first()
        if already_posted is not None:
            raise PublishError("This export is already marked posted in the publish queue.")

        existing = session.scalars(
            select(UploadJob)
            .where(UploadJob.account_id == account.id)
            .where(UploadJob.processed_path == path_text)
            .where(UploadJob.posted_at.is_(None))
            .where(UploadJob.status != "posted")
            .order_by(UploadJob.id.desc())
            .limit(1)
        ).first()

        made_for_kids = int(account.upload_made_for_kids or 0)
        synthetic = int(account.upload_contains_synthetic_media or 0)
        timezone_label = account.upload_timezone or _DEFAULT_TIMEZONE
        privacy = account.upload_default_privacy or _DEFAULT_PRIVACY

        if existing is not None:
            existing.download_item_id = item.id
            existing.title = title
            existing.description = description
            existing.scheduled_at = scheduled
            existing.timezone = timezone_label
            existing.privacy_status = privacy
            existing.made_for_kids = made_for_kids
            existing.contains_synthetic_media = synthetic
            existing.status = status
            existing.error_message = None
            session.commit()
            result = {
                "job_id": existing.id,
                "status": status,
                "scheduled_at": _iso(scheduled),
                "created": False,
            }
        else:
            job = UploadJob(
                account_id=account.id,
                download_item_id=item.id,
                processed_path=path_text,
                title=title,
                description=description,
                scheduled_at=scheduled,
                timezone=timezone_label,
                privacy_status=privacy,
                made_for_kids=made_for_kids,
                contains_synthetic_media=synthetic,
                status=status,
            )
            session.add(job)
            session.commit()
            result = {
                "job_id": job.id,
                "status": status,
                "scheduled_at": _iso(scheduled),
                "created": True,
            }

    # Cloud handoff (outside the session): a scheduled post on a cloud-mapped
    # account is pushed to the Worker and flips to status 'cloud' so the local
    # publish loop skips it. Inert (returns None) unless the account is mapped.
    if status == "scheduled":
        cloud_status = _handoff_scheduled_job_to_cloud(result["job_id"])
        if cloud_status:
            result["status"] = cloud_status
    return result


def auto_schedule_for_publish(
    item_id: int,
    *,
    now: dt.datetime | None = None,
    rng: _random.Random | None = None,
) -> dict:
    """Queue an exported item in a safe catch-up or next open posting slot."""
    now_local = now or dt.datetime.now().astimezone()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise PublishError(f"No download item with id {item_id}.")
        if not item.processed_path:
            raise PublishError("Export the reel before scheduling it.")
        if item.account_id is None:
            raise PublishError("Assign this item to an account before scheduling it.")

        account = session.get(Account, item.account_id)
        if account is None:
            raise PublishError("The item's account no longer exists.")

        jobs = session.scalars(
            select(UploadJob).where(UploadJob.account_id == account.id)
        ).all()
        # Re-export of an already-scheduled item must KEEP its slot: the job's
        # own time would otherwise read as "occupied" and silently push the
        # post to the next open slot on every re-export. Refresh the job's
        # content (title/caption) at the existing time instead.
        existing_scheduled = next(
            (
                row
                for row in jobs
                if row.posted_at is None
                and row.status != "posted"
                and row.scheduled_at is not None
                and row.processed_path == item.processed_path
            ),
            None,
        )
        if existing_scheduled is not None:
            kept_time = _aware(existing_scheduled.scheduled_at)
            result = queue_for_publish(item_id, scheduled_at=kept_time.isoformat())
            result["schedule_path"] = "kept_existing"
            result["message"] = f"Kept existing schedule for {kept_time.astimezone():%H:%M}"
            return result
        catch_up_occupied = [
            _aware(value)
            for row in jobs
            for value in (row.scheduled_at, row.posted_at)
            if value is not None
        ]
        posted_times = [_aware(row.posted_at) for row in jobs if row.posted_at is not None]
        last_posted_at = max(posted_times, default=None)
        checkpoint_cooldown = _account_in_checkpoint_cooldown(account.id, now=now_local)

        scheduled_local = catch_up_slot_time(
            account.upload_schedule_slots,
            now=now_local,
            occupied=catch_up_occupied,
            last_posted_at=last_posted_at,
            checkpoint_cooldown=checkpoint_cooldown,
            min_gap_hours=SAME_ACCOUNT_MIN_GAP_HOURS,
            rng=rng,
        )
        missed_slot = (
            most_recent_passed_slot_time(account.upload_schedule_slots, now=now_local)
            if scheduled_local is not None
            else None
        )
        schedule_path = "catch_up" if scheduled_local is not None else "next_open_slot"
        if scheduled_local is None:
            # Enforce the same-account gap and respect already-posted times, not
            # just scheduled ones, so a new post never lands too soon after a
            # recent one.
            scheduled_local = next_safe_slot_for_account(
                session,
                account,
                after=now_local,
                rng=rng,
                fallback_gap=False,
            )

    if scheduled_local is None:
        raise PublishError(
            "Set this account's schedule slots first (e.g. 09:00, 18:00) "
            "in account settings."
        )
    result = queue_for_publish(item_id, scheduled_at=scheduled_local.isoformat())
    result["schedule_path"] = schedule_path
    if schedule_path == "catch_up" and missed_slot is not None:
        result["message"] = (
            f"Catch-up: scheduled for {scheduled_local:%H:%M} "
            f"(missed {missed_slot:%H:%M} slot)"
        )
    else:
        result["message"] = f"Scheduled for {scheduled_local:%H:%M}"
    return result
