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

from sqlalchemy import select

from nicheflow_studio.core.scheduling import next_open_slot_time
from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

_DEFAULT_TIMEZONE = "Asia/Bangkok"
_DEFAULT_PRIVACY = "private"
_ITEM_LIST_LIMIT = 50


class PublishError(ServiceError):
    """Raised for invalid publish-queue operations (not exported, no account…)."""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_scheduled_at(value: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 time to an aware UTC datetime.

    A naive value (e.g. from an HTML datetime-local input) is interpreted in the
    machine's local timezone then converted to UTC, matching the PyQt behavior.
    """
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PublishError(f"Invalid scheduled time: {value!r}") from exc
    return parsed.astimezone(dt.timezone.utc)


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
                "processed_path": row.processed_path,
            }
            for row in rows
        ]


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
            return {
                "job_id": existing.id,
                "status": status,
                "scheduled_at": _iso(scheduled),
                "created": False,
            }

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
        return {
            "job_id": job.id,
            "status": status,
            "scheduled_at": _iso(scheduled),
            "created": True,
        }


def auto_schedule_for_publish(item_id: int) -> dict:
    """Queue an exported item in its account's next open posting slot."""
    now_local = dt.datetime.now().astimezone()
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

        occupied = [
            row.scheduled_at.replace(tzinfo=dt.timezone.utc)
            if row.scheduled_at.tzinfo is None
            else row.scheduled_at
            for row in session.scalars(
                select(UploadJob)
                .where(UploadJob.account_id == account.id)
                .where(UploadJob.posted_at.is_(None))
                .where(UploadJob.scheduled_at.is_not(None))
            ).all()
            if row.scheduled_at is not None
        ]
        scheduled_local = next_open_slot_time(
            account.upload_schedule_slots,
            after=now_local,
            occupied=occupied,
        )

    if scheduled_local is None:
        raise PublishError(
            "Set this account's schedule slots first (e.g. 09:00, 18:00) "
            "in account settings."
        )
    return queue_for_publish(item_id, scheduled_at=scheduled_local.isoformat())
