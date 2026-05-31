"""Pure aggregation for the multi-account Publishing Dashboard.

The dashboard answers one question per account: *can this account publish right
now, and what is queued?* That depends on two independent facts the UI already
tracks separately — the account's **session health** (login status) and its
**publish jobs** (how many are due / scheduled). This module joins them into one
row per account.

Kept free of Qt and the ORM so it is trivially unit-testable and reusable by a
future web frontend (see ``PLAN.md`` §14). Callers pass plain values fetched from
the DB; this module does the counting and the publishability logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from nicheflow_studio.core.account_health import HealthState

# A session can still post while its login is OK or merely aging. Every other
# state (stale, no session, wrong account, throttled, cooling down, ...) means a
# publish would fail or is unsafe, so the dashboard treats it as "blocked".
PUBLISHABLE_SESSION_STATES = frozenset({HealthState.OK, HealthState.WARN})

# Jobs in these states are candidates to be posted (the rest are drafts, already
# posted, or failed). Mirrors ``UploadJob.status`` values used by the queue.
_DUE_STATES = frozenset({"ready", "scheduled"})


@dataclass(frozen=True)
class PublishJobView:
    """Minimal view of an ``UploadJob`` the dashboard needs. Decouples the
    aggregation from the ORM so tests don't need a database."""

    status: str
    posted_at: datetime | None
    scheduled_at: datetime | None


@dataclass(frozen=True)
class AccountJobCounts:
    """How many of an account's jobs are due now / scheduled / still drafts."""

    due_now: int
    scheduled: int
    drafts: int
    next_post_at: datetime | None


@dataclass(frozen=True)
class AccountDashboardRow:
    """One fully-resolved dashboard row: session + publish readiness joined."""

    account_id: int
    account_name: str
    profile: str | None
    session_state: str
    session_detail: str
    due_now: int
    scheduled: int
    drafts: int
    next_post_at: datetime | None
    slots: str | None
    publishable: bool
    blocked_reason: str | None


def _as_aware(value: datetime | None, *, tz) -> datetime | None:
    """Treat naive datetimes (legacy rows) as UTC so comparisons are safe."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def summarize_jobs(jobs: Iterable[PublishJobView], *, now: datetime) -> AccountJobCounts:
    """Bucket one account's jobs into due-now / scheduled / drafts.

    - **due_now**: not yet posted, in a postable state, and either unscheduled or
      scheduled for a time that has already arrived.
    - **scheduled**: not yet posted, postable, scheduled for the future.
    - **drafts**: not yet posted and not in a postable state (still ``draft``).

    ``next_post_at`` is the soonest *future* scheduled time across the account.
    """
    tz = now.tzinfo
    due_now = 0
    scheduled = 0
    drafts = 0
    upcoming: list[datetime] = []

    for job in jobs:
        if job.posted_at is not None:
            continue
        status = (job.status or "").lower()
        if status not in _DUE_STATES:
            drafts += 1
            continue
        scheduled_at = _as_aware(job.scheduled_at, tz=tz)
        if scheduled_at is not None and scheduled_at > now:
            scheduled += 1
            upcoming.append(scheduled_at)
        else:
            due_now += 1

    next_post_at = min(upcoming) if upcoming else None
    return AccountJobCounts(
        due_now=due_now,
        scheduled=scheduled,
        drafts=drafts,
        next_post_at=next_post_at,
    )


def derive_publishability(
    session_state: str,
    counts: AccountJobCounts,
    *,
    session_label: str,
) -> tuple[bool, str | None]:
    """Decide if an account is ready to publish now, and why not if it isn't.

    Returns ``(publishable, blocked_reason)``:
    - ``publishable`` is True only when the session can post AND something is due.
    - ``blocked_reason`` is set only when work is waiting but the session can't
      post it — the high-signal case the dashboard exists to surface (e.g.
      "4 due but login needs re-login"). It stays ``None`` when nothing is due,
      so a healthy idle account isn't flagged as a problem.
    """
    session_can_post = session_state in PUBLISHABLE_SESSION_STATES
    if session_can_post:
        return (counts.due_now > 0, None)
    if counts.due_now > 0:
        return (False, f"{counts.due_now} due but blocked — {session_label.lower()}")
    return (False, None)


def build_dashboard_row(
    *,
    account_id: int,
    account_name: str,
    profile: str | None,
    session_state: str,
    session_label: str,
    session_detail: str,
    jobs: Iterable[PublishJobView],
    slots: str | None,
    now: datetime,
) -> AccountDashboardRow:
    """Assemble one dashboard row from session health + this account's jobs."""
    counts = summarize_jobs(jobs, now=now)
    publishable, blocked_reason = derive_publishability(
        session_state, counts, session_label=session_label
    )
    return AccountDashboardRow(
        account_id=account_id,
        account_name=account_name,
        profile=profile,
        session_state=session_state,
        session_detail=session_detail,
        due_now=counts.due_now,
        scheduled=counts.scheduled,
        drafts=counts.drafts,
        next_post_at=counts.next_post_at,
        slots=slots,
        publishable=publishable,
        blocked_reason=blocked_reason,
    )


@dataclass(frozen=True)
class DashboardTotals:
    """Network-wide rollup shown in the dashboard's summary strip."""

    account_count: int
    total_due_now: int
    total_scheduled: int
    publishable_accounts: int
    blocked_accounts: int
    next_post_at: datetime | None


def summarize_dashboard(rows: Iterable[AccountDashboardRow]) -> DashboardTotals:
    """Roll per-account rows up into network totals for the summary strip."""
    rows = list(rows)
    upcoming = [r.next_post_at for r in rows if r.next_post_at is not None]
    return DashboardTotals(
        account_count=len(rows),
        total_due_now=sum(r.due_now for r in rows),
        total_scheduled=sum(r.scheduled for r in rows),
        publishable_accounts=sum(1 for r in rows if r.publishable),
        blocked_accounts=sum(1 for r in rows if r.blocked_reason is not None),
        next_post_at=min(upcoming) if upcoming else None,
    )
