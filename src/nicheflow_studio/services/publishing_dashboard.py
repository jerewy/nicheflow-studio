"""UI-independent Publishing Dashboard read models and safe actions."""

from __future__ import annotations

import datetime as dt
import os
import random
import time
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import joinedload

from nicheflow_studio.core.account_health import HealthState, live_health, local_health
from nicheflow_studio.core.instagram_profile_pool import ProfilePool
from nicheflow_studio.core.publishing_dashboard import (
    PublishJobView,
    build_dashboard_row,
    summarize_dashboard,
)
from nicheflow_studio.db.models import Account, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.publisher.instagram_web import launch_instagram_login
from nicheflow_studio.services.errors import ServiceError

_TOP_POSTS_LIMIT = 5

HEALTH_LABELS = {
    HealthState.OK: "OK",
    HealthState.WARN: "Aging",
    HealthState.STALE: "Re-login",
    HealthState.NO_SESSION: "No login",
    HealthState.COOLDOWN: "Cooldown",
    HealthState.THROTTLED: "Throttled",
    HealthState.LOGGED_OUT: "Logged out",
    HealthState.UNKNOWN: "Unknown",
    HealthState.NOT_CONFIGURED: "No profile",
    HealthState.MISMATCH: "Wrong account",
}


class PublishingDashboardError(ServiceError):
    """Raised for invalid dashboard operations."""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _due(job: UploadJob, now: dt.datetime) -> bool:
    if job.posted_at is not None or (job.status or "").lower() not in {"ready", "scheduled"}:
        return False
    scheduled_at = _aware(job.scheduled_at)
    return scheduled_at is None or scheduled_at <= now


def list_global_publish_jobs() -> dict:
    """Recent exported jobs plus every ready/scheduled job, matching PyQt."""
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=24)
    with get_session() as session:
        jobs = (
            session.query(UploadJob)
            .options(joinedload(UploadJob.account), joinedload(UploadJob.download_item))
            .filter(UploadJob.posted_at.is_(None), UploadJob.status != "posted")
            .order_by(UploadJob.created_at.desc())
            .all()
        )
        visible = []
        seen_paths: set[str] = set()
        for job in jobs:
            path = Path(job.processed_path)
            status = (job.status or "").lower()
            created = _aware(job.created_at)
            if not path.exists():
                continue
            if status not in {"ready", "scheduled"} and created is not None and created < cutoff:
                continue
            path_key = str(path.resolve()).casefold()
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            visible.append(
                {
                    "id": job.id,
                    "account_name": job.account.name if job.account else "Unassigned",
                    "video": (
                        job.download_item.title
                        if job.download_item is not None and job.download_item.title
                        else path.stem
                    ),
                    "title": job.title,
                    "status": status,
                    "is_due": _due(job, now),
                    "scheduled_at": _iso(job.scheduled_at),
                    "profile": (job.account.instagram_profile if job.account else None),
                    "output_name": path.name,
                    "processed_path": str(path),
                }
            )
    counts = {key: sum(1 for row in visible if row["status"] == key) for key in ("draft", "ready", "scheduled")}
    return {"jobs": visible, "due_count": sum(1 for row in visible if row["is_due"]), **counts}


def top_posts(account_id: int, *, limit: int = _TOP_POSTS_LIMIT) -> list[dict]:
    """Highest-engagement measured posts for exactly one account.

    V1 uses total interactions; learning account-specific ranking weights is
    deliberately deferred until an account has roughly 50 measured posts.
    """
    with get_session() as session:
        jobs = (
            session.query(UploadJob)
            .filter(
                UploadJob.account_id == account_id,
                (UploadJob.posted_at.is_not(None)) | (UploadJob.status == "posted"),
                (UploadJob.posted_views.is_not(None))
                | (UploadJob.posted_likes.is_not(None))
                | (UploadJob.posted_comments.is_not(None))
                | (UploadJob.posted_shares.is_not(None)),
            )
            .all()
        )
    ranked = sorted(
        jobs,
        key=lambda job: (
            int(job.posted_likes or 0)
            + int(job.posted_comments or 0)
            + int(job.posted_shares or 0),
            int(job.posted_views or 0),
            job.id,
        ),
        reverse=True,
    )
    return [
        {
            "id": job.id,
            "account_id": job.account_id,
            "title": job.title,
            "posted_views": job.posted_views,
            "posted_likes": job.posted_likes,
            "posted_comments": job.posted_comments,
            "posted_shares": job.posted_shares,
            "engagement": (
                int(job.posted_likes or 0)
                + int(job.posted_comments or 0)
                + int(job.posted_shares or 0)
            ),
        }
        for job in ranked[: max(0, limit)]
    ]


def top_post_titles(account_id: int, *, limit: int = _TOP_POSTS_LIMIT) -> list[str]:
    """Measured winner titles for few-shot prompting, strongest first."""
    titles: list[str] = []
    for row in top_posts(account_id, limit=max(limit * 20, limit)):
        title = str(row["title"] or "").strip()
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def mark_ready(job_ids: list[int]) -> dict:
    """Mark selected existing exported files ready for global publishing."""
    with get_session() as session:
        jobs = session.query(UploadJob).filter(UploadJob.id.in_(job_ids)).all()
        updated = 0
        for job in jobs:
            if job.posted_at is not None or job.status == "posted" or not Path(job.processed_path).exists():
                continue
            job.status = "ready"
            job.scheduled_at = None
            updated += 1
        session.commit()
    return {"updated": updated}


def open_output(job_id: int) -> dict:
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is None:
            raise PublishingDashboardError(f"No publish job with id {job_id}.")
        path = Path(job.processed_path)
    if not path.exists():
        raise PublishingDashboardError("Reel output file is missing.")
    os.startfile(str(path))
    return {"opened": str(path)}


def _not_configured(account_name: str) -> tuple[str, str]:
    return HealthState.NOT_CONFIGURED, (
        "No Instagram profile assigned - set one in account settings, then log in."
    )


def account_readiness() -> dict:
    """Network-free per-account readiness table and global totals."""
    now = dt.datetime.now(dt.timezone.utc)
    pool = ProfilePool.load()
    with get_session() as session:
        accounts = session.query(Account).order_by(Account.name.asc()).all()
        jobs_by_account: dict[int, list[PublishJobView]] = {}
        for account_id, status, posted_at, scheduled_at in session.query(
            UploadJob.account_id, UploadJob.status, UploadJob.posted_at, UploadJob.scheduled_at
        ).filter(UploadJob.posted_at.is_(None)).all():
            jobs_by_account.setdefault(account_id, []).append(
                PublishJobView(status=status, posted_at=posted_at, scheduled_at=scheduled_at)
            )
        rows = []
        dashboard_rows = []
        for account in accounts:
            profile = (account.instagram_profile or "").strip() or None
            if profile is None:
                state, detail = _not_configured(account.name)
            else:
                health = local_health(profile, account.name, pool=pool, now=now)
                state, detail = health.state, health.detail
            row = build_dashboard_row(
                account_id=account.id,
                account_name=account.name or "(unnamed)",
                profile=profile,
                session_state=state,
                session_label=HEALTH_LABELS.get(state, state),
                session_detail=detail,
                jobs=jobs_by_account.get(account.id, []),
                slots=account.upload_schedule_slots,
                now=now,
            )
            dashboard_rows.append(row)
            rows.append(
                {
                    "account_id": row.account_id,
                    "account_name": row.account_name,
                    "profile": row.profile,
                    "login_identifier": account.login_identifier,
                    "session_state": row.session_state,
                    "session_label": HEALTH_LABELS.get(row.session_state, row.session_state),
                    "detail": row.blocked_reason or row.session_detail,
                    "due_now": row.due_now,
                    "scheduled": row.scheduled,
                    "next_post_at": _iso(row.next_post_at),
                    "publishable": row.publishable,
                }
            )
    totals = summarize_dashboard(dashboard_rows)
    return {
        "rows": rows,
        "totals": {
            "account_count": totals.account_count,
            "total_due_now": totals.total_due_now,
            "total_scheduled": totals.total_scheduled,
            "blocked_accounts": totals.blocked_accounts,
            "next_post_at": _iso(totals.next_post_at),
        },
    }


def check_all_live(*, progress: Callable[[float, str], None] | None = None) -> dict:
    """Live-check configured accounts sequentially with gentle spacing."""
    with get_session() as session:
        targets = [
            (a.name, (a.instagram_profile or "").strip(), (a.instagram_handle or "").strip() or None)
            for a in session.query(Account).order_by(Account.name.asc()).all()
            if (a.instagram_profile or "").strip()
        ]
    results = []
    total = len(targets)
    for index, (name, profile, expected) in enumerate(targets):
        health = live_health(profile, name, expected_username=expected)
        results.append(
            {"account_name": name, "state": health.state, "label": HEALTH_LABELS.get(health.state, health.state), "detail": health.detail}
        )
        if progress:
            progress((index + 1) / max(total, 1), f"Checked {name}")
        if index + 1 < total:
            time.sleep(random.uniform(2.0, 4.0))
    return {"results": results}


def relogin(account_id: int) -> dict:
    with get_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise PublishingDashboardError(f"No account with id {account_id}.")
        profile = (account.instagram_profile or "").strip()
        if not profile:
            raise PublishingDashboardError("This account has no Instagram profile assigned.")
    launch_instagram_login(profile)
    return {"profile": profile}
