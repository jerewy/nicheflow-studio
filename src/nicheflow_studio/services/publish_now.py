"""Live Instagram posting for the React app — "Publish Now" + auto-publish-due.

This is the engine that actually posts. It resolves an item/job to its account
profile, exported file, and caption, then drives
:func:`publisher.instagram_publisher.publish_reel` — a **real, irreversible
network action** — and records the outcome on the :class:`UploadJob`.

Two safety properties:
- **Serialized.** ``publish_reel`` opens a real browser; a module lock guarantees
  only one post runs at a time (Publish Now and the auto-publish loop share it).
- **No DB session held across the post.** Args are read in a short session, the
  session is released, the (slow) browser post runs, then a second short session
  records the result — so the multi-minute post never holds a SQLite session open.

``publish_reel`` is imported lazily so importing this module (and the bridge) does
not require Playwright at import time, and tests can patch it cheaply.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Callable

from sqlalchemy import select

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.services.publishing import queue_for_publish

# publish_reel drives a real browser — never run two at once.
_PUBLISH_LOCK = threading.Lock()
# Cap how many due reels one auto-publish pass posts, so an unattended loop can
# never fire the whole backlog at once.
_DUE_BATCH_LIMIT = 3


class PublishNowError(ServiceError):
    """Raised when an item/job can't be posted (no profile, no file, etc.)."""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


def _resolve_post_args(job: UploadJob, account: Account, item: DownloadItem | None) -> tuple:
    """(profile, video, caption) for a job, or raise if something's missing."""
    profile = (account.instagram_profile or "").strip()
    if not profile:
        raise PublishNowError(
            f"Account '{account.name}' has no Instagram profile (login slot) set — "
            "set it in Accounts before publishing."
        )
    video = job.processed_path or (item.processed_path if item is not None else None)
    if not video:
        raise PublishNowError("No exported reel file to post — export the reel first.")
    caption = job.description or (item.caption_draft if item is not None else "") or ""
    return profile, video, caption


def _do_publish_reel(profile: str, video: str, caption: str):
    """Seam to the real publisher. Lazy import keeps Playwright optional at import
    time, and tests patch this single function instead of opening a browser."""
    from nicheflow_studio.publisher.instagram_publisher import publish_reel

    return publish_reel(profile, video, caption)


def _post_and_record(job_id: int) -> dict:
    """Post one queued job (live) and record the result. Caller holds the lock."""
    # 1. Read everything needed, then release the session before the slow post.
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is None:
            raise PublishNowError(f"No publish job with id {job_id}.")
        if job.posted_at is not None or job.status == "posted":
            return {"status": "already_posted", "job_id": job_id, "posted_url": job.posted_url}
        account = session.get(Account, job.account_id)
        if account is None:
            raise PublishNowError("The job's account no longer exists.")
        item = session.get(DownloadItem, job.download_item_id) if job.download_item_id else None
        profile, video, caption = _resolve_post_args(job, account, item)

    # 2. The real, irreversible post (no DB session held while the browser runs).
    result = _do_publish_reel(profile, video, caption)

    # 3. Record the outcome.
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is None:
            return {"status": result.status, "job_id": job_id, "posted_url": result.posted_url}
        if result.status == "posted":
            job.status = "posted"
            job.posted_at = dt.datetime.now(dt.timezone.utc)
            if result.posted_url:
                job.posted_url = result.posted_url
            job.error_message = None
        else:
            job.error_message = result.error_message or result.status
        session.commit()
    return {
        "status": result.status,
        "job_id": job_id,
        "posted_url": result.posted_url,
        "error": result.error_message,
    }


def publish_item_now(item_id: int) -> dict:
    """Post the item's exported reel to Instagram now (live).

    Ensures a publish-queue job exists for the item, posts it, and records the
    result. Raises :class:`PublishNowError` for fixable problems (not exported,
    no account, no profile).
    """
    # Reuse the queue upsert for validation + job creation (raises if the item
    # isn't exported / has no account / is already posted).
    queue_for_publish(item_id)
    with _PUBLISH_LOCK:
        with get_session() as session:
            job = session.scalars(
                select(UploadJob)
                .where(UploadJob.download_item_id == item_id)
                .where(UploadJob.posted_at.is_(None))
                .where(UploadJob.status != "posted")
                .order_by(UploadJob.id.desc())
                .limit(1)
            ).first()
            if job is None:
                raise PublishNowError("Could not find a publish job for this item.")
            job_id = job.id
        return {"item_id": item_id, **_post_and_record(job_id)}


def list_due_jobs() -> list[dict]:
    """Scheduled jobs whose time has passed (status ``scheduled``, time <= now)."""
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as session:
        rows = session.scalars(
            select(UploadJob)
            .where(UploadJob.status == "scheduled")
            .where(UploadJob.posted_at.is_(None))
            .where(UploadJob.scheduled_at.is_not(None))
            .order_by(UploadJob.scheduled_at.asc())
        ).all()
        return [
            {"job_id": row.id, "title": row.title, "scheduled_at": _iso(_aware(row.scheduled_at))}
            for row in rows
            if row.scheduled_at is not None and _aware(row.scheduled_at) <= now
        ]


def due_count() -> int:
    """How many scheduled jobs are currently due (quick check for the UI)."""
    return len(list_due_jobs())


def publish_due_jobs(*, limit: int = _DUE_BATCH_LIMIT) -> dict:
    """Post all currently-due scheduled jobs (up to ``limit``), one at a time.

    Backs both the manual "Publish due now" button and the opt-in auto-publish
    loop. Returns how many posted vs failed.
    """
    due = list_due_jobs()[: max(0, limit)]
    posted = 0
    failed = 0
    results: list[dict] = []
    with _PUBLISH_LOCK:
        for entry in due:
            try:
                outcome = _post_and_record(entry["job_id"])
            except PublishNowError as exc:
                failed += 1
                results.append({"job_id": entry["job_id"], "status": "failed", "error": str(exc)})
                continue
            if outcome.get("status") == "posted":
                posted += 1
            else:
                failed += 1
            results.append(outcome)
    return {"due": len(due), "posted": posted, "failed": failed, "results": results}


_AUTO_PUBLISH_PREF_KEY = "auto_publish_due_reels"


def auto_publish_enabled() -> bool:
    """Whether the opt-in auto-publish-due loop is on (persisted UI pref)."""
    from nicheflow_studio.core.ui_prefs import get_ui_pref

    return bool(get_ui_pref(_AUTO_PUBLISH_PREF_KEY, False))


def set_auto_publish_enabled(enabled: bool) -> bool:
    from nicheflow_studio.core.ui_prefs import set_ui_pref

    set_ui_pref(_AUTO_PUBLISH_PREF_KEY, bool(enabled))
    return bool(enabled)
