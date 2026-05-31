from datetime import datetime, timedelta, timezone

from nicheflow_studio.core.account_health import HealthState
from nicheflow_studio.core.publishing_dashboard import (
    PublishJobView,
    build_dashboard_row,
    derive_publishability,
    summarize_dashboard,
    summarize_jobs,
)

NOW = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)


def _ready(scheduled_at=None) -> PublishJobView:
    return PublishJobView(status="ready", posted_at=None, scheduled_at=scheduled_at)


def _scheduled(scheduled_at) -> PublishJobView:
    return PublishJobView(status="scheduled", posted_at=None, scheduled_at=scheduled_at)


# --- summarize_jobs ---------------------------------------------------------


def test_summarize_jobs_buckets_due_scheduled_and_drafts() -> None:
    jobs = [
        _ready(),  # due now (no schedule)
        _scheduled(NOW - timedelta(minutes=5)),  # scheduled but past -> due now
        _scheduled(NOW + timedelta(hours=2)),  # future -> scheduled
        PublishJobView(status="draft", posted_at=None, scheduled_at=None),  # draft
        PublishJobView(status="posted", posted_at=NOW, scheduled_at=None),  # ignored
    ]

    counts = summarize_jobs(jobs, now=NOW)

    assert counts.due_now == 2
    assert counts.scheduled == 1
    assert counts.drafts == 1
    assert counts.next_post_at == NOW + timedelta(hours=2)


def test_summarize_jobs_next_post_is_soonest_future() -> None:
    jobs = [
        _scheduled(NOW + timedelta(hours=5)),
        _scheduled(NOW + timedelta(hours=1)),
        _scheduled(NOW + timedelta(hours=3)),
    ]

    counts = summarize_jobs(jobs, now=NOW)

    assert counts.next_post_at == NOW + timedelta(hours=1)
    assert counts.scheduled == 3
    assert counts.due_now == 0


def test_summarize_jobs_treats_naive_scheduled_at_as_utc() -> None:
    # Legacy rows may store naive datetimes; a future naive time must count as
    # scheduled, not accidentally fall into due-now.
    naive_future = (NOW + timedelta(hours=1)).replace(tzinfo=None)

    counts = summarize_jobs([_scheduled(naive_future)], now=NOW)

    assert counts.scheduled == 1
    assert counts.due_now == 0


def test_summarize_jobs_empty() -> None:
    counts = summarize_jobs([], now=NOW)
    assert (counts.due_now, counts.scheduled, counts.drafts) == (0, 0, 0)
    assert counts.next_post_at is None


# --- derive_publishability --------------------------------------------------


def test_publishable_when_session_ok_and_work_due() -> None:
    counts = summarize_jobs([_ready()], now=NOW)
    publishable, reason = derive_publishability(
        HealthState.OK, counts, session_label="OK"
    )
    assert publishable is True
    assert reason is None


def test_aging_session_can_still_publish() -> None:
    counts = summarize_jobs([_ready()], now=NOW)
    publishable, reason = derive_publishability(
        HealthState.WARN, counts, session_label="Aging"
    )
    assert publishable is True
    assert reason is None


def test_due_work_with_dead_session_is_blocked_with_reason() -> None:
    counts = summarize_jobs([_ready(), _ready()], now=NOW)
    publishable, reason = derive_publishability(
        HealthState.STALE, counts, session_label="Re-login"
    )
    assert publishable is False
    assert reason == "2 due but blocked — re-login"


def test_idle_account_with_bad_session_is_not_flagged() -> None:
    # No due work -> not a problem to surface, even if the session is dead.
    counts = summarize_jobs([], now=NOW)
    publishable, reason = derive_publishability(
        HealthState.NO_SESSION, counts, session_label="No login"
    )
    assert publishable is False
    assert reason is None


def test_ok_session_with_no_due_work_is_not_publishable() -> None:
    counts = summarize_jobs([_scheduled(NOW + timedelta(hours=1))], now=NOW)
    publishable, reason = derive_publishability(
        HealthState.OK, counts, session_label="OK"
    )
    assert publishable is False
    assert reason is None


# --- build_dashboard_row + summarize_dashboard ------------------------------


def _row(account_id, name, state, label, jobs):
    return build_dashboard_row(
        account_id=account_id,
        account_name=name,
        profile=name.lower().replace(" ", ""),
        session_state=state,
        session_label=label,
        session_detail="",
        jobs=jobs,
        slots="09:00, 18:00",
        now=NOW,
    )


def test_build_dashboard_row_joins_session_and_jobs() -> None:
    row = _row(1, "Cinema Files Daily", HealthState.OK, "OK", [_ready()])
    assert row.account_id == 1
    assert row.due_now == 1
    assert row.publishable is True
    assert row.blocked_reason is None


def test_summarize_dashboard_rolls_up_totals() -> None:
    rows = [
        _row(1, "Cinema Files Daily", HealthState.OK, "OK", [_ready()]),
        _row(
            2,
            "Past Moments Daily",
            HealthState.STALE,
            "Re-login",
            [_ready(), _ready(), _ready(), _ready()],
        ),
        _row(
            3,
            "Memeists Daily",
            HealthState.OK,
            "OK",
            [_scheduled(NOW + timedelta(hours=4))],
        ),
    ]

    totals = summarize_dashboard(rows)

    assert totals.account_count == 3
    assert totals.total_due_now == 5  # 1 + 4
    assert totals.total_scheduled == 1
    assert totals.publishable_accounts == 1  # only Cinema (Past Moments is blocked)
    assert totals.blocked_accounts == 1  # Past Moments: 4 due but stale
    assert totals.next_post_at == NOW + timedelta(hours=4)
