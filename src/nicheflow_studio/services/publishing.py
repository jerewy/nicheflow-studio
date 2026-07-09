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
import logging
import random as _random
import threading
import time

from sqlalchemy import select

from nicheflow_studio.core.scheduling import (
    catch_up_slot_time,
    most_recent_passed_slot_time,
    next_open_slot_time,
)
from nicheflow_studio.db.assignments import ACCOUNT_OPERATIONAL_STATUS_ACTIVE
from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

logger = logging.getLogger(__name__)

_DEFAULT_TIMEZONE = "Asia/Bangkok"
_DEFAULT_PRIVACY = "private"
_ITEM_LIST_LIMIT = 50
# Minimum spacing between two posts on the SAME account. Used both when
# auto-scheduling a new post and when deferring a due post that would otherwise
# land too soon after the account's last post (reach cannibalization / bot tell).
SAME_ACCOUNT_MIN_GAP_HOURS = 4
# Default gap when an account sets no override. Deliberately below a 4h slot
# spacing (240): the collision guard is inclusive and jitter-widened, so a flat
# 240 default rejects the *adjacent* slot of a 6/day-at-4h account and silently
# halves its cadence. 210 (3.5h) clears a 4h slot with room for the 15m jitter
# while still rejecting genuinely-too-close slots (e.g. a 2h gap stays blocked).
DEFAULT_SAME_ACCOUNT_MIN_GAP_MINUTES = 210
_CLOUD_HANDOFF_LOCKS: dict[int, threading.Lock] = {}
_CLOUD_HANDOFF_LOCKS_GUARD = threading.Lock()
# Bulk exports run one JobManager thread per item, so two auto-schedules for the
# SAME account can read the identical set of occupied slots and both claim it.
# Serializing slot computation + queue commit per account removes that race.
_ACCOUNT_SCHEDULE_LOCKS: dict[int, threading.Lock] = {}
_ACCOUNT_SCHEDULE_LOCKS_GUARD = threading.Lock()


class PublishError(ServiceError):
    """Raised for invalid publish-queue operations (not exported, no account…)."""


def _looks_like_placeholder_title(value: str | None) -> bool:
    title = (value or "").strip().lower()
    return title.startswith("video by ")


def _validate_scheduled_metadata(title: str | None, description: str | None) -> None:
    """Scheduled/live posts must use finalized draft text, not raw source labels."""
    if not (title or "").strip() or _looks_like_placeholder_title(title):
        raise PublishError("Choose a final title before scheduling this reel.")
    if not (description or "").strip():
        raise PublishError("Choose a final caption before scheduling this reel.")


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


def _parse_iso(value: str | None) -> dt.datetime | None:
    """Tolerant ISO-8601 -> aware UTC (or None). Accepts a trailing ``Z``."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


def _account_in_checkpoint_cooldown(account_id: int, *, now: dt.datetime) -> bool:
    # Lazy import avoids the publish_now -> publishing module dependency cycle.
    from nicheflow_studio.services.publish_now import _account_on_cooldown

    return _account_on_cooldown(account_id, now=now.astimezone(dt.timezone.utc))


def account_min_gap_minutes(account: Account) -> int:
    """Minimum minutes between two posts on THIS account.

    Uses the account's ``upload_min_gap_minutes`` override when set, otherwise the
    module default (``DEFAULT_SAME_ACCOUNT_MIN_GAP_MINUTES``). The default sits
    below a 4h slot spacing on purpose: an account running 6/day with slots
    exactly 4h apart would otherwise have every adjacent slot rejected by the
    scheduler's (inclusive, jitter-widened) collision guard and silently post at
    half cadence. It still comfortably rejects genuinely-too-close slots (e.g. a
    2h gap), preserving the reach-cannibalization / bot-tell guard.
    """
    value = account.upload_min_gap_minutes
    if value is None or value <= 0:
        return DEFAULT_SAME_ACCOUNT_MIN_GAP_MINUTES
    return int(value)


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
    min_gap_minutes = account_min_gap_minutes(account)
    gap = dt.timedelta(minutes=min_gap_minutes)
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
        collision_minutes=min_gap_minutes,
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


def _worker_jobs_for_local(worker_jobs: list[dict], job_id: int) -> list[dict]:
    """Worker jobs belonging to a local job id. external_id is ``nf-<id>-<time_ns>``
    (a fresh id per push); the legacy ``nf-<id>`` form is matched too."""
    legacy = f"nf-{job_id}"
    prefix = f"nf-{job_id}-"
    return [
        w
        for w in worker_jobs
        if (w.get("external_id") == legacy or (w.get("external_id") or "").startswith(prefix))
    ]


def _latest_worker_job_for_local(worker_jobs: list[dict], job_id: int) -> dict | None:
    """Newest Worker generation for a local job, with legacy ids sorting first."""
    matches = _worker_jobs_for_local(worker_jobs, job_id)
    prefix = f"nf-{job_id}-"

    def generation(worker: dict) -> int:
        external_id = worker.get("external_id") or ""
        if not external_id.startswith(prefix):
            return -1
        try:
            return int(external_id.removeprefix(prefix))
        except ValueError:
            return -1

    return max(matches, key=generation, default=None)


def _cloud_handoff_lock(job_id: int) -> threading.Lock:
    with _CLOUD_HANDOFF_LOCKS_GUARD:
        return _CLOUD_HANDOFF_LOCKS.setdefault(job_id, threading.Lock())


def handoff_scheduled_job_to_cloud(job_id: int) -> str | None:
    """Serialize cloud handoffs for one local job to avoid duplicate uploads."""
    with _cloud_handoff_lock(job_id):
        return _handoff_scheduled_job_to_cloud(job_id)


def _handoff_scheduled_job_to_cloud(job_id: int) -> str | None:
    """Push a scheduled job for a cloud-mapped account to the Cloudflare Worker.

    Each push uses a FRESH ``nf-<job id>-<time_ns>`` external id, and any existing
    Worker job for this local job is canceled first — so re-scheduling actually
    replaces the cloud job (with its new time) instead of silently no-op'ing
    against a stale one. Returns ``"cloud"`` when handed off (so the local publish
    loop, which only acts on ``status == "scheduled"``, skips it), or ``None`` when
    no handoff applies. A hard failure marks the job ``failed`` and raises.
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
        title = job.title
        caption = job.description or ""
        scheduled_iso = _aware(job.scheduled_at).isoformat()
        try:
            _validate_scheduled_metadata(title, caption)
        except PublishError as exc:
            job.status = "failed"
            job.error_message = str(exc)[:512]
            session.commit()
            raise

    try:
        # Cancel any prior active Worker job before pushing its replacement.
        # Failing closed here prevents the old and new schedules both publishing.
        for worker in _worker_jobs_for_local(cloud_publisher.list_jobs().get("jobs", []), job_id):
            if worker.get("status") in ("awaiting_upload", "scheduled", "processing"):
                cloud_publisher.cancel_job(worker["id"])
        cloud_publisher.schedule_reel(
            external_id=f"nf-{job_id}-{time.time_ns()}",
            account_key=worker_key,
            caption=caption,
            scheduled_at=scheduled_iso,
            video_path=video,
        )
    except cloud_publisher.CloudPublisherError as exc:
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


def cancel_cloud_handoff(job_id: int) -> int:
    """Cancel active Worker jobs for a local publish job.

    Used before a local schedule is cleared. If the Worker cannot be reached, let
    the caller fail closed so the UI does not show an unscheduled draft while the
    cloud copy may still publish.
    """
    from nicheflow_studio.services import cloud_publisher

    if not cloud_publisher.is_configured():
        return 0
    try:
        canceled = 0
        for worker in _worker_jobs_for_local(cloud_publisher.list_jobs().get("jobs", []), job_id):
            if worker.get("status") in ("awaiting_upload", "scheduled", "processing"):
                cloud_publisher.cancel_job(worker["id"])
                canceled += 1
        return canceled
    except cloud_publisher.CloudPublisherError as exc:
        raise PublishError(f"Cloud schedule cancel failed: {exc}") from exc


def queue_for_publish(item_id: int, *, scheduled_at: str | None = None) -> dict:
    """Add (or update) the item's exported reel in the publish queue.

    Requires the item to be exported (``processed_path`` set) and assigned to an
    account. With ``scheduled_at`` the job is ``scheduled``; otherwise it is a
    ``draft``. An existing non-posted job for the same account+file is updated.
    A posted export is rejected unless an explicit repost action has already
    created a fresh non-posted attempt.
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
        if account.operational_status != ACCOUNT_OPERATIONAL_STATUS_ACTIVE:
            logger.info(
                "Skipped queuing reel for account '%s' (id=%s): operational_status=%s",
                account.name,
                account.id,
                account.operational_status,
            )
            raise PublishError(
                f"Account '{account.name}' is {account.operational_status}, not active. "
                "Reactivate it before publishing."
            )

        title = (item.title_draft or item.title or "").strip() or None
        description = item.caption_draft
        status = "scheduled" if scheduled is not None else "draft"
        path_text = item.processed_path
        if status == "scheduled":
            _validate_scheduled_metadata(title, description)

        existing = session.scalars(
            select(UploadJob)
            .where(UploadJob.account_id == account.id)
            .where(UploadJob.processed_path == path_text)
            .where(UploadJob.posted_at.is_(None))
            .where(UploadJob.status != "posted")
            .order_by(UploadJob.id.desc())
            .limit(1)
        ).first()

        if existing is None:
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
        cloud_status = handoff_scheduled_job_to_cloud(result["job_id"])
        if cloud_status:
            result["status"] = cloud_status
    return result


def _account_schedule_lock(account_id: int) -> threading.Lock:
    with _ACCOUNT_SCHEDULE_LOCKS_GUARD:
        return _ACCOUNT_SCHEDULE_LOCKS.setdefault(account_id, threading.Lock())


def auto_schedule_for_publish(
    item_id: int,
    *,
    now: dt.datetime | None = None,
    rng: _random.Random | None = None,
) -> dict:
    """Queue an exported item in a safe catch-up or next open posting slot."""
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise PublishError(f"No download item with id {item_id}.")
        account_id = item.account_id
    if account_id is None:
        # No account -> no slots to race over; the impl raises the precise error.
        return _auto_schedule_for_publish(item_id, now=now, rng=rng)
    with _account_schedule_lock(account_id):
        return _auto_schedule_for_publish(item_id, now=now, rng=rng)


def _auto_schedule_for_publish(
    item_id: int,
    *,
    now: dt.datetime | None = None,
    rng: _random.Random | None = None,
) -> dict:
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
        #
        # Only keep a slot that is still genuinely pending in the FUTURE. A
        # failed/canceled job, or one whose time is already in the past, must
        # NOT anchor the re-queue: otherwise re-queuing a missed post reuses its
        # dead past time and the cloud fires it immediately instead of finding a
        # fresh slot.
        existing_scheduled = next(
            (
                row
                for row in jobs
                if row.posted_at is None
                and row.status not in ("posted", "failed", "canceled")
                and row.scheduled_at is not None
                and _aware(row.scheduled_at) > now_local
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
            min_gap_hours=account_min_gap_minutes(account) / 60,
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


def force_publish_cloud_job(job_id: int) -> dict:
    """Bypass the Worker's daily_limit/min_gap safety gate for one cloud job.

    A deliberate manual override for a job stuck 'scheduled' on the Worker
    because of the account-safety cooldown -- it never touches the account's
    settings or any other job. Raises :class:`PublishError` if there's no
    pending Worker job to force (already published, canceled, or never handed
    off).
    """
    from nicheflow_studio.services import cloud_publisher

    if not cloud_publisher.is_configured():
        raise PublishError("Cloud publisher is not configured.")
    try:
        worker = _latest_worker_job_for_local(cloud_publisher.list_jobs().get("jobs", []), job_id)
        if worker is None or worker.get("status") != "scheduled":
            raise PublishError("No pending cloud job to force for this item.")
        return cloud_publisher.force_run_job(worker["id"])
    except cloud_publisher.CloudPublisherError as exc:
        raise PublishError(f"Force publish failed: {exc}") from exc


def list_cloud_jobs() -> dict:
    """All Worker publish jobs, joined to local account names for display.

    Thin wrapper over :func:`cloud_publisher.list_jobs` for the Cloud Publisher
    Control panel -- adds ``account_name`` (via :func:`cloud_publisher.
    cloud_publish_map`, local account id -> Worker ``account_key``) and, when the
    job's ``external_id`` matches this app's ``nf-<local id>-...`` convention, the
    local ``upload_job_id`` so the panel can offer force-publish/cancel actions.
    """
    from nicheflow_studio.services import cloud_publisher

    if not cloud_publisher.is_configured():
        return {"jobs": [], "publish_mode": None}
    key_to_name: dict[str, str] = {}
    with get_session() as session:
        accounts = session.scalars(select(Account)).all()
        id_to_name = {account.id: account.name for account in accounts}
    for local_id, worker_key in cloud_publisher.cloud_publish_map().items():
        try:
            account_name = id_to_name.get(int(local_id))
        except ValueError:
            account_name = None
        if account_name:
            key_to_name[worker_key] = account_name
    try:
        payload = cloud_publisher.list_jobs()
    except cloud_publisher.CloudPublisherError as exc:
        raise PublishError(f"Could not list cloud jobs: {exc}") from exc
    jobs = []
    for worker in payload.get("jobs", []):
        external_id = worker.get("external_id") or ""
        upload_job_id: int | None = None
        if external_id.startswith("nf-"):
            candidate = external_id.removeprefix("nf-").split("-", 1)[0]
            if candidate.isdigit():
                upload_job_id = int(candidate)
        jobs.append(
            {
                **worker,
                "account_name": key_to_name.get(worker.get("account_key"), worker.get("account_key")),
                "upload_job_id": upload_job_id,
            }
        )
    return {"jobs": jobs, "publish_mode": payload.get("publish_mode")}


def sync_cloud_jobs() -> dict:
    """Pull job states from the Cloudflare Worker and update local ``cloud`` jobs.

    Matches the newest Worker generation by ``external_id`` prefix
    (``nf-<local job id>-<time_ns>``; legacy exact ids still work):
    ``published`` -> local ``posted`` (with ``posted_at``);
    ``manual_local_available``/``local_fallback``/``failed``/``canceled`` ->
    local ``failed`` (with the Worker's error). ``validated``/``scheduled``/
    ``processing`` stay ``cloud`` (still pending a real post). No-op when cloud
    publishing isn't configured or the Worker is unreachable.

    Every synced job also gets its raw ``cloud_status``/``cloud_error`` mirrored
    from the Worker on every poll (not just when the local ``status`` flips), so
    the dashboard can always show *why* a gate-blocked job hasn't posted yet
    instead of looking frozen.
    """
    from nicheflow_studio.services import cloud_publisher

    if not cloud_publisher.is_configured():
        return {"synced": False, "updated": 0}
    try:
        worker_jobs = cloud_publisher.list_jobs().get("jobs", [])
    except cloud_publisher.CloudPublisherError:
        return {"synced": False, "updated": 0}
    updated = 0
    dirty = False
    with get_session() as session:
        local_jobs = session.scalars(select(UploadJob).where(UploadJob.status == "cloud")).all()
        for job in local_jobs:
            worker = _latest_worker_job_for_local(worker_jobs, job.id)
            if worker is None:
                continue
            wstatus = (worker.get("status") or "").lower()
            werror = (worker.get("error_message") or "").strip()[:1024] or None
            if wstatus != (job.cloud_status or ""):
                job.cloud_status = wstatus or None
                dirty = True
            if werror != job.cloud_error:
                job.cloud_error = werror
                dirty = True
            if wstatus == "published":
                job.status = "posted"
                job.posted_at = _parse_iso(worker.get("published_at")) or dt.datetime.now(
                    dt.timezone.utc
                )
                if job.download_item_id is not None:
                    item = session.get(DownloadItem, job.download_item_id)
                    if item is not None:
                        item.status = "posted"
                job.error_message = None
                updated += 1
            elif wstatus in ("manual_local_available", "local_fallback"):
                job.status = "failed"
                job.error_message = (
                    worker.get("error_message")
                    or "Cloud publisher could not post this job. Use manual browser publish if needed."
                )[:512]
                updated += 1
            elif wstatus in ("failed", "canceled"):
                job.status = "failed"
                job.error_message = (worker.get("error_message") or f"cloud job {wstatus}")[:512]
                updated += 1
            elif wstatus in ("scheduled", "processing"):
                # Still pending, but the Worker may have set a reason it hasn't
                # posted yet (e.g. daily_limit/min_gap cooldown) -- surface it as
                # a note so the dashboard doesn't just show blank "Cloud" while
                # a job silently waits. Status stays 'cloud'; this is never a
                # failure, so other views (which key error display off
                # status == 'failed') are unaffected.
                note = (worker.get("error_message") or "").strip()[:512] or None
                if note != job.error_message:
                    job.error_message = note
                    updated += 1
            # validated -> still 'cloud' (no change yet)
        if updated or dirty:
            session.commit()
    return {"synced": True, "updated": updated}
