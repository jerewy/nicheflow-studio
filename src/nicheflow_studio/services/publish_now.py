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
import random as _random
import threading
import time
from collections import deque
from typing import Callable

from sqlalchemy import select

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.assignments import mark_assignment_posted_for_job
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.services.publishing import queue_for_publish

# publish_reel drives a real browser — never run two at once.
_PUBLISH_LOCK = threading.Lock()
# Cap how many due reels one auto-publish pass posts, so an unattended loop can
# never fire the whole backlog at once.
_DUE_BATCH_LIMIT = 3
# A transient failure gets exactly one delayed retry before the job is marked
# failed — never an endless re-attempt loop on every auto-publish pass, which
# escalates platform flags (especially after a checkpoint).
_RETRY_DELAY_MINUTES = (10, 20)
# Different accounts posting from the same IP within minutes of each other is a
# correlation tell; keep a randomized human-ish gap between posts to different
# accounts. Enforced centrally in ``_post_and_record`` so it applies to BOTH the
# manual Publish Now path and the batch auto-publish loop.
_INTER_POST_GAP_SECONDS = (120, 360)
# After Instagram shows a checkpoint, stop posting on that account entirely
# until the cooldown passes (the user should re-login / check the account).
_CHECKPOINT_COOLDOWN = dt.timedelta(hours=3)
_account_cooldowns: dict[int, dt.datetime] = {}
# Same-account recency window: a manual publish to an account that already posted
# within this window gets a confirmation warning (back-to-back posts cannibalize
# reach and read as bot-like). Advisory only — the user can still choose to post.
_RECENT_POST_WINDOW = dt.timedelta(hours=4)
# Seam for tests: patch to avoid real sleeping in the batch loop.
_sleep = time.sleep

# Accounts with a live post currently running (a real browser is open for them).
# Lets the UI's Publish Now pre-check warn that a post is already in progress for
# an account, instead of only catching it after the lock releases.
_in_flight_lock = threading.Lock()
_in_flight_accounts: set[int] = set()

# A small in-memory feed of completed live posts the UI hasn't shown yet. The
# background auto-publish loop is otherwise silent, so it records its posts here
# for the UI to pick up (and toast) on its next poll. Bounded so a long-running
# app with the UI closed can't grow it without limit.
_publish_events_lock = threading.Lock()
_publish_events: deque[dict] = deque(maxlen=50)
_publish_event_seq = 0


def _mark_account_in_flight(account_id: int) -> None:
    with _in_flight_lock:
        _in_flight_accounts.add(account_id)


def _clear_account_in_flight(account_id: int) -> None:
    with _in_flight_lock:
        _in_flight_accounts.discard(account_id)


def _account_in_flight(account_id: int) -> bool:
    with _in_flight_lock:
        return account_id in _in_flight_accounts


def record_publish_event(event: dict) -> None:
    """Append a completed-post event for the UI to pick up via
    :func:`drain_publish_events`. Stamped with a monotonic id and a UTC time so
    the UI can show *when* it posted even if it polls minutes later."""
    global _publish_event_seq
    with _publish_events_lock:
        _publish_event_seq += 1
        _publish_events.append(
            {
                "id": _publish_event_seq,
                "at": _iso(dt.datetime.now(dt.timezone.utc)),
                **event,
            }
        )


def drain_publish_events() -> list[dict]:
    """Return and clear the pending completed-post events (UI poll)."""
    with _publish_events_lock:
        events = list(_publish_events)
        _publish_events.clear()
        return events


class PublishNowError(ServiceError):
    """Raised when an item/job can't be posted (no profile, no file, etc.)."""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _account_on_cooldown(account_id: int, *, now: dt.datetime | None = None) -> bool:
    until = _account_cooldowns.get(account_id)
    if until is None:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    if now >= until:
        _account_cooldowns.pop(account_id, None)
        return False
    return True


def _start_account_cooldown(account_id: int) -> None:
    _account_cooldowns[account_id] = dt.datetime.now(dt.timezone.utc) + _CHECKPOINT_COOLDOWN


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


def _account_last_posted(session, account_id: int) -> dt.datetime | None:
    """The most recent ``posted_at`` across an account's jobs (None if never)."""
    latest = session.scalars(
        select(UploadJob.posted_at)
        .where(UploadJob.account_id == account_id)
        .where(UploadJob.posted_at.is_not(None))
        .order_by(UploadJob.posted_at.desc())
        .limit(1)
    ).first()
    return _aware(latest) if latest is not None else None


def _last_other_account_post(session, account_id: int) -> dt.datetime | None:
    """Most recent ``posted_at`` across all OTHER accounts (None if none).

    Used for cross-account IP spacing: it answers "when did *any other* account
    last post from this machine", independent of this account's own history.
    """
    latest = session.scalars(
        select(UploadJob.posted_at)
        .where(UploadJob.account_id != account_id)
        .where(UploadJob.posted_at.is_not(None))
        .order_by(UploadJob.posted_at.desc())
        .limit(1)
    ).first()
    return _aware(latest) if latest is not None else None


def _wait_for_inter_account_gap(
    account_id: int, *, progress: Callable[[float, str], None] | None = None
) -> None:
    """Pause so this post doesn't land too soon after a post on a *different*
    account. Different accounts posting from one IP within minutes is a
    correlation tell, so keep a randomized human-ish gap between them.

    No-op when no other account has posted, or the last cross-account post is
    already older than the gap window — so a single post, or one made after the
    user has been working a while, never waits. Same-account spacing is the 4h
    recency window, handled separately. Caller holds ``_PUBLISH_LOCK``.
    """
    low, high = _INTER_POST_GAP_SECONDS
    with get_session() as session:
        last_other = _last_other_account_post(session, account_id)
    if last_other is None:
        return
    elapsed = (dt.datetime.now(dt.timezone.utc) - last_other).total_seconds()
    if elapsed >= low:
        return  # another account posted, but long enough ago — no extra spacing
    gap = _random.randint(low, high)
    if progress is not None:
        progress(0.2, f"Spacing posts across accounts — posting in ~{gap}s")
    _sleep(gap)


def _recency_warning(last_posted: dt.datetime | None, *, now: dt.datetime) -> dict | None:
    """Warning payload when ``last_posted`` is inside the recency window, else None."""
    if last_posted is None or now - last_posted >= _RECENT_POST_WINDOW:
        return None
    return {
        "last_posted_at": _iso(last_posted),
        "minutes_since": int((now - last_posted).total_seconds() // 60),
        "recommended_next_at": _iso(last_posted + _RECENT_POST_WINDOW),
    }


def _target_account_id(session, item_id: int) -> int | None:
    """The account an item's reel will post to: its latest unposted job's account,
    falling back to the item's own assignment."""
    account_id = session.scalars(
        select(UploadJob.account_id)
        .where(UploadJob.download_item_id == item_id)
        .where(UploadJob.posted_at.is_(None))
        .where(UploadJob.status != "posted")
        .order_by(UploadJob.id.desc())
        .limit(1)
    ).first()
    if account_id is not None:
        return account_id
    item = session.get(DownloadItem, item_id)
    return item.account_id if item is not None else None


def _defer_job_to_safe_slot(job_id: int) -> str | None:
    """Reschedule a due job to its account's next safe slot (one that keeps the
    same-account gap). Returns the new ISO time, or None if it couldn't defer."""
    from nicheflow_studio.services.publishing import next_safe_slot_for_account

    after = dt.datetime.now().astimezone()
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is None:
            return None
        account = session.get(Account, job.account_id)
        if account is None:
            return None
        safe = next_safe_slot_for_account(
            session, account, after=after, exclude_job_id=job_id
        )
        if safe is None:
            return None
        job.scheduled_at = safe.astimezone(dt.timezone.utc)
        job.status = "scheduled"
        session.commit()
        return _iso(_aware(job.scheduled_at))


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


def _post_and_record(
    job_id: int, *, progress: Callable[[float, str], None] | None = None
) -> dict:
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
        account_id = account.id
        account_name = account.name
        download_item_id = job.download_item_id
        profile, video, caption = _resolve_post_args(job, account, item)

    # 2. Keep a human-ish gap from the previous post on a DIFFERENT account
    #    (same-IP back-to-back posts read as bot-like). No-op for a lone post.
    _wait_for_inter_account_gap(account_id, progress=progress)

    # 3. The real, irreversible post (no DB session held while the browser runs).
    #    Mark the account in-flight so a concurrent recency check (the UI's
    #    Publish Now pre-check) can warn that a post is already running for it.
    if progress is not None:
        progress(0.5, "Posting to Instagram…")
    _mark_account_in_flight(account_id)
    try:
        result = _do_publish_reel(profile, video, caption)
    finally:
        _clear_account_in_flight(account_id)

    # 4. Record the outcome.
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
            mark_assignment_posted_for_job(session, job)
        elif result.status == "checkpoint":
            # Instagram flagged the account — retrying makes it worse. Fail the
            # job and pause the whole account until the cooldown passes.
            job.status = "failed"
            job.error_message = result.error_message or "checkpoint detected"
            _start_account_cooldown(job.account_id)
        else:
            # A scheduled job that already carries an error message has used its
            # one retry; anything else gets a single delayed re-attempt so a
            # transient hiccup recovers without hammering every loop pass.
            already_retried = bool(job.error_message)
            job.error_message = result.error_message or result.status
            if job.status == "scheduled" and not already_retried:
                delay = _random.randint(*_RETRY_DELAY_MINUTES)
                job.scheduled_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=delay)
            else:
                job.status = "failed"
        session.commit()
    return {
        "status": result.status,
        "job_id": job_id,
        "item_id": download_item_id,
        "account_id": account_id,
        "account_name": account_name,
        "posted_url": result.posted_url,
        "error": result.error_message,
    }


def publish_item_now(
    item_id: int,
    *,
    allow_recent: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Post the item's exported reel to Instagram now (live).

    Ensures a publish-queue job exists for the item, posts it, and records the
    result. Raises :class:`PublishNowError` for fixable problems (not exported,
    no account, no profile).

    Unless ``allow_recent`` is set, posting is refused when the account already
    posted within the same-account recency window — the call returns a status
    ``"on_cooldown"`` with the recency details (and posts nothing) so the UI can
    offer reschedule / "post anyway" instead of silently double-posting. This is
    the backend backstop for the Publish Now confirmation; ``allow_recent=True``
    is the explicit override.
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
            account_id = job.account_id
        if not allow_recent:
            now = dt.datetime.now(dt.timezone.utc)
            with get_session() as session:
                warning = _recency_warning(_account_last_posted(session, account_id), now=now)
                account = session.get(Account, account_id)
            if warning is not None:
                return {
                    "item_id": item_id,
                    "status": "on_cooldown",
                    "account_id": account_id,
                    "account_name": account.name if account else None,
                    **warning,
                }
        return {"item_id": item_id, **_post_and_record(job_id, progress=progress)}


def list_due_jobs() -> list[dict]:
    """Scheduled jobs whose time has passed (status ``scheduled``, time <= now).

    Jobs on an account in checkpoint cooldown are excluded — nothing should
    post on a flagged account until the cooldown expires.
    """
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
            {
                "job_id": row.id,
                "account_id": row.account_id,
                "title": row.title,
                "scheduled_at": _iso(_aware(row.scheduled_at)),
            }
            for row in rows
            if row.scheduled_at is not None
            and _aware(row.scheduled_at) <= now
            and not _account_on_cooldown(row.account_id, now=now)
        ]


def due_count() -> int:
    """How many scheduled jobs are currently due (quick check for the UI)."""
    return len(list_due_jobs())


def item_publish_recency(item_id: int) -> dict:
    """Whether posting this item now would fall within its account's recent-post
    window. Read-only; backs the Publish Now confirmation. ``on_cooldown`` False
    means safe to post (never posted, or the last post is older than the window)."""
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as session:
        account_id = _target_account_id(session, item_id)
        if account_id is None:
            return {"on_cooldown": False}
        account = session.get(Account, account_id)
        account_name = account.name if account else None
        # A post to this account is already running — warn now rather than after
        # the user waits out the lock (the backend still serializes either way).
        if _account_in_flight(account_id):
            return {
                "on_cooldown": True,
                "in_progress": True,
                "account_id": account_id,
                "account_name": account_name,
            }
        warning = _recency_warning(_account_last_posted(session, account_id), now=now)
        if warning is None:
            return {"on_cooldown": False}
        return {
            "on_cooldown": True,
            "account_id": account_id,
            "account_name": account_name,
            **warning,
        }


def due_publish_recency() -> list[dict]:
    """For currently-due jobs, the accounts that posted within the recent-post
    window — one entry per affected account. Read-only; backs the Publish due now
    confirmation so the user sees which accounts would be posting again too soon."""
    now = dt.datetime.now(dt.timezone.utc)
    account_ids = {
        entry["account_id"] for entry in list_due_jobs() if entry.get("account_id") is not None
    }
    if not account_ids:
        return []
    warnings: list[dict] = []
    with get_session() as session:
        for account_id in sorted(account_ids):
            warning = _recency_warning(_account_last_posted(session, account_id), now=now)
            if warning is None:
                continue
            account = session.get(Account, account_id)
            warnings.append(
                {
                    "account_id": account_id,
                    "account_name": account.name if account else None,
                    **warning,
                }
            )
    return warnings


def _account_posted_recently(account_id: int) -> bool:
    """True if the account posted within the same-account window (DB truth — a
    post made earlier in the same batch has already committed)."""
    with get_session() as session:
        last = _account_last_posted(session, account_id)
    return last is not None and dt.datetime.now(dt.timezone.utc) - last < _RECENT_POST_WINDOW


def publish_due_jobs(*, limit: int = _DUE_BATCH_LIMIT, allow_recent: bool = False) -> dict:
    """Post all currently-due scheduled jobs (up to ``limit``), one at a time.

    Backs both the manual "Publish due now" button and the opt-in auto-publish
    loop. Returns how many posted vs failed vs deferred.

    Unless ``allow_recent`` is set, a due job whose account already posted within
    the same-account window is deferred to its next safe slot instead of posted.
    This is what stops a backlog (e.g. an overnight job and today's slot both
    becoming due when the app reopens) from firing two reels to one account
    minutes apart: the first posts, the rest for that account get rescheduled.
    The auto loop relies on this; the manual button passes ``allow_recent=True``
    only when the user explicitly chooses "publish anyway".
    """
    due = list_due_jobs()[: max(0, limit)]
    posted = 0
    failed = 0
    deferred = 0
    results: list[dict] = []
    with _PUBLISH_LOCK:
        for entry in due:
            account_id = entry.get("account_id")
            if account_id is not None and _account_on_cooldown(account_id):
                continue  # a checkpoint earlier in this batch paused the account
            if not allow_recent and account_id is not None and _account_posted_recently(account_id):
                new_time = _defer_job_to_safe_slot(entry["job_id"])
                deferred += 1
                results.append(
                    {"job_id": entry["job_id"], "status": "deferred", "scheduled_at": new_time}
                )
                continue
            # The randomized cross-account gap is applied inside _post_and_record,
            # so consecutive posts (often different accounts on the same IP) don't
            # land back-to-back like a bot.
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
    return {
        "due": len(due),
        "posted": posted,
        "failed": failed,
        "deferred": deferred,
        "results": results,
    }


_AUTO_PUBLISH_PREF_KEY = "auto_publish_due_reels"


def auto_publish_enabled() -> bool:
    """Whether the opt-in auto-publish-due loop is on (persisted UI pref)."""
    from nicheflow_studio.core.ui_prefs import get_ui_pref

    return bool(get_ui_pref(_AUTO_PUBLISH_PREF_KEY, False))


def set_auto_publish_enabled(enabled: bool) -> bool:
    from nicheflow_studio.core.ui_prefs import set_ui_pref

    set_ui_pref(_AUTO_PUBLISH_PREF_KEY, bool(enabled))
    return bool(enabled)
