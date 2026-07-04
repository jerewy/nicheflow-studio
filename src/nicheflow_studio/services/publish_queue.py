"""Publish Queue management service (UI-independent).

Read + manage view over :class:`UploadJob` rows for the migrated Publishing
screen: list the queue across accounts, record a manual post result
(mark-posted with URL/metrics), reschedule/unschedule, and remove a job.

Scope: this manages queue *state*. The actual Instagram post (Playwright
publish-now) intentionally remains in the desktop app — recording a manual post
here just updates the row the same way the desktop "mark posted" flow does.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import or_, select

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.assignments import mark_assignment_posted_for_job
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

_LIST_LIMIT = 200
_METRIC_FIELDS = ("posted_views", "posted_likes", "posted_comments", "posted_shares")


class PublishQueueError(ServiceError):
    """Raised for invalid publish-queue operations (unknown job, bad time…)."""


def _iso(value: dt.datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset. SQLite returns naive datetimes;
    without the offset the frontend's Date() reads them as LOCAL time and
    every displayed schedule shifts by the machine's UTC offset."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def _parse_iso_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).astimezone(dt.timezone.utc)
    except ValueError as exc:
        raise PublishQueueError(f"Invalid time: {value!r}") from exc


def _coerce_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise PublishQueueError(f"Expected a number, got {value!r}.") from exc


def _clean_opt(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _account_name(session, account_id: int | None) -> str | None:
    if account_id is None:
        return None
    account = session.get(Account, account_id)
    return account.name if account else None


def _job_view(job: UploadJob, account_name: str | None) -> dict:
    return {
        "id": job.id,
        "account_id": job.account_id,
        "account_name": account_name,
        "download_item_id": job.download_item_id,
        "title": job.title,
        "status": job.status,
        "scheduled_at": _iso(job.scheduled_at),
        "posted_at": _iso(job.posted_at),
        "posted_url": job.posted_url,
        "posted_views": job.posted_views,
        "posted_likes": job.posted_likes,
        "posted_comments": job.posted_comments,
        "posted_shares": job.posted_shares,
        "content_type": job.content_type,
        "processed_path": job.processed_path,
    }


def _require_job(session, job_id: int) -> UploadJob:
    job = session.get(UploadJob, job_id)
    if job is None:
        raise PublishQueueError(f"No publish job with id {job_id}.")
    return job


def list_jobs(account_id: int | None = None) -> list[dict]:
    """Publish-queue jobs (newest first), optionally filtered by account."""
    with get_session() as session:
        names = {a.id: a.name for a in session.scalars(select(Account)).all()}
        query = select(UploadJob).order_by(UploadJob.id.desc()).limit(_LIST_LIMIT)
        if account_id is not None:
            query = query.where(UploadJob.account_id == account_id)
        rows = session.scalars(query).all()
        return [_job_view(job, names.get(job.account_id)) for job in rows]


def mark_posted(job_id: int, payload: dict | None = None) -> dict:
    """Record a manual post: set status ``posted`` (and ``posted_at`` if unset)
    and store the provided URL / metrics / content type."""
    payload = payload or {}
    with get_session() as session:
        job = _require_job(session, job_id)
        job.status = "posted"
        if job.posted_at is None:
            job.posted_at = dt.datetime.now(dt.timezone.utc)
        if job.download_item_id is not None:
            item = session.get(DownloadItem, job.download_item_id)
            if item is not None:
                item.status = "posted"
        mark_assignment_posted_for_job(session, job)
        if "posted_url" in payload:
            job.posted_url = _clean_opt(payload["posted_url"])
        if "content_type" in payload:
            job.content_type = _clean_opt(payload["content_type"])
        for field in _METRIC_FIELDS:
            if field in payload:
                setattr(job, field, _coerce_int(payload[field]))
        name = _account_name(session, job.account_id)
        session.commit()
        return _job_view(job, name)


def update_metrics(job_id: int, payload: dict | None = None) -> dict:
    """Update manually-entered IG Insights metrics for an already-posted job."""
    payload = payload or {}
    with get_session() as session:
        job = _require_job(session, job_id)
        if job.posted_at is None and job.status != "posted":
            raise PublishQueueError("Metrics can only be entered for a posted job.")
        for field in _METRIC_FIELDS:
            if field not in payload:
                continue
            value = _coerce_int(payload[field])
            if value is not None and value < 0:
                raise PublishQueueError("Metrics cannot be negative.")
            setattr(job, field, value)
        name = _account_name(session, job.account_id)
        session.commit()
        return _job_view(job, name)


_EDITABLE_PROCESSING_STATUSES = {"pending_review", "draft", "exported"}


def set_processing_status(item_id: int, status: str) -> dict:
    """Set one Processing row to a manually editable workflow status.

    When reopening a posted export, keep its historical posted row and create a
    separate draft attempt so publishing is unblocked for only this item.
    """
    status = str(status or "").strip().lower()
    # "posted" isn't a hand-set label (the real state is derived from the posted
    # UploadJob). Selecting it on a reopened item means "undo the reopen" — discard
    # the draft repost attempt so the derived status falls back to posted.
    if status == "posted":
        return _revert_reopen_to_posted(item_id)
    if status not in _EDITABLE_PROCESSING_STATUSES:
        raise PublishQueueError(f"Status {status!r} cannot be set manually.")
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise PublishQueueError(f"No Processing item with id {item_id}.")
        if status == "exported" and not item.processed_path:
            raise PublishQueueError("Export the reel before setting its status to Exported.")

        posted = session.scalars(
            select(UploadJob)
            .where(UploadJob.download_item_id == item.id)
            .where((UploadJob.posted_at.is_not(None)) | (UploadJob.status == "posted"))
            .order_by(UploadJob.id.desc())
            .limit(1)
        ).first()

        repost = session.scalars(
            select(UploadJob)
            .where(UploadJob.download_item_id == item.id)
            .where(UploadJob.posted_at.is_(None))
            .where(UploadJob.status != "posted")
            .order_by(UploadJob.id.desc())
            .limit(1)
        ).first()
        created = repost is None
        if posted is not None and repost is None:
            if not item.processed_path:
                raise PublishQueueError("Export the reel again before reopening this posted item.")
            repost = UploadJob(
                account_id=posted.account_id,
                download_item_id=item.id,
                processed_path=item.processed_path,
                title=(item.title_draft or item.title or posted.title or "").strip() or None,
                description=item.caption_draft if item.caption_draft is not None else posted.description,
                timezone=posted.timezone,
                privacy_status=posted.privacy_status,
                made_for_kids=posted.made_for_kids,
                contains_synthetic_media=posted.contains_synthetic_media,
                status="draft",
            )
            session.add(repost)
        elif repost is not None:
            if repost.status in {"scheduled", "cloud"}:
                raise PublishQueueError("Cancel the active schedule before changing this status.")
            repost.status = "draft"
            repost.scheduled_at = None
            repost.error_message = None

        item.status = status
        # A rejected/skipped item carries a review_state that _derive_status treats
        # as the source of truth — it overrides item.status. Returning it to
        # "pending_review" must clear that, or the derived status snaps straight
        # back to "rejected" on the next refresh and the change looks like a no-op.
        if status == "pending_review":
            item.review_state = "pending_review"
        session.commit()
        return {
            "item_id": item.id,
            "status": status,
            "repost_job_id": repost.id if repost is not None else None,
            "created": created and repost is not None,
        }


def _revert_reopen_to_posted(item_id: int) -> dict:
    """Undo a manual reopen: drop the draft repost attempt created when a posted
    item was switched to draft/exported, so its derived status returns to posted.

    Only valid for items that were genuinely posted (have a posted UploadJob). The
    posted job — and its post history — is kept; the per-account recency/cooldown
    math keeps keying off its ``posted_at``.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise PublishQueueError(f"No Processing item with id {item_id}.")
        posted = session.scalars(
            select(UploadJob)
            .where(UploadJob.download_item_id == item.id)
            .where((UploadJob.posted_at.is_not(None)) | (UploadJob.status == "posted"))
            .order_by(UploadJob.id.desc())
            .limit(1)
        ).first()
        if posted is None:
            raise PublishQueueError("This item was never posted, so it can't be set to Posted.")
        # The reopen attempt is any non-posted job newer than the posted one — this
        # is exactly the condition that marks the item "reopened" in the library
        # status derivation (see services/library.py).
        reopen_jobs = session.scalars(
            select(UploadJob)
            .where(UploadJob.download_item_id == item.id)
            .where(UploadJob.posted_at.is_(None))
            .where(UploadJob.status != "posted")
            .where(UploadJob.id > posted.id)
        ).all()
        for job in reopen_jobs:
            if job.status in {"scheduled", "cloud"}:
                raise PublishQueueError(
                    "Cancel the active schedule before setting this back to Posted."
                )
        for job in reopen_jobs:
            session.delete(job)
        # Mirror the natural posted state a live post writes (publish_now sets this).
        item.status = "posted"
        session.commit()
        return {
            "item_id": item.id,
            "status": "posted",
            "repost_job_id": None,
            "created": False,
        }


def reschedule(job_id: int, scheduled_at: str) -> dict:
    """Set a job's scheduled time (ISO-8601) and mark it ``scheduled``."""
    when = _parse_iso_utc(scheduled_at)
    if when is None:
        raise PublishQueueError("A scheduled time is required.")
    with get_session() as session:
        job = _require_job(session, job_id)
        if job.posted_at is not None or job.status == "posted":
            raise PublishQueueError("This job is already posted; it cannot be rescheduled.")
        job.scheduled_at = when
        job.status = "scheduled"
        # Rescheduling revives failed jobs too — clear the old error so the
        # next attempt gets its one automatic retry again.
        job.error_message = None
        session.commit()

    # Multi-Account Publish edits use this service rather than
    # publishing.queue_for_publish(), so explicitly perform the same cloud
    # replacement handoff after the local schedule is committed.
    from nicheflow_studio.services import publishing

    publishing.handoff_scheduled_job_to_cloud(job_id)
    with get_session() as session:
        job = _require_job(session, job_id)
        account = session.get(Account, job.account_id)
        return _job_view(job, account.name if account else None)


def unschedule(job_id: int) -> dict:
    """Clear a job's schedule and return it to ``draft``."""
    with get_session() as session:
        job = _require_job(session, job_id)
        if job.posted_at is not None or job.status == "posted":
            raise PublishQueueError("This job is already posted; it cannot be unscheduled.")
        should_cancel_cloud = job.status == "cloud"

    if should_cancel_cloud:
        from nicheflow_studio.services import publishing

        try:
            publishing.cancel_cloud_handoff(job_id)
        except publishing.PublishError as exc:
            raise PublishQueueError(str(exc)) from exc

    with get_session() as session:
        job = _require_job(session, job_id)
        job.scheduled_at = None
        job.status = "draft"
        job.error_message = None
        account = session.get(Account, job.account_id)
        session.commit()
        return _job_view(job, account.name if account else None)


def unschedule_jobs_for_item(item_id: int) -> list[dict]:
    """Unschedule every pending (non-posted) publish job for a download item.

    Used when an item is rejected so a clip that was already scheduled — and
    possibly handed off to the cloud Worker — can't still auto-post. Reuses
    :func:`unschedule`, so each job's Worker-side cloud job is canceled and the
    local job drops back to ``draft`` (reversible). Posted jobs are left alone.
    Fails closed: a cloud cancel that can't be confirmed propagates as
    :class:`PublishQueueError` rather than silently leaving the cloud copy live.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        conditions = [UploadJob.download_item_id == item_id]
        processed_path = item.processed_path if item is not None else None
        if processed_path:
            # Legacy jobs created before download_item_id was linked still match
            # on the exported file path.
            conditions.append(UploadJob.processed_path == processed_path)
        job_ids = [
            row[0]
            for row in session.execute(
                select(UploadJob.id)
                .where(or_(*conditions))
                .where(UploadJob.posted_at.is_(None))
                .where(UploadJob.status != "posted")
                .order_by(UploadJob.id.asc())
            ).all()
        ]
    return [unschedule(job_id) for job_id in job_ids]


def remove_job(job_id: int) -> dict:
    """Delete a job from the publish queue."""
    with get_session() as session:
        job = _require_job(session, job_id)
        session.delete(job)
        session.commit()
        return {"removed_job_id": job_id}
