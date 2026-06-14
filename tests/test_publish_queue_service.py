from __future__ import annotations

import pytest

from nicheflow_studio.db.models import Account, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import publish_queue
from nicheflow_studio.services.publish_queue import PublishQueueError


def _make_job(*, status: str = "draft") -> int:
    with get_session() as session:
        account = Account(name="Movies", platform="instagram")
        session.add(account)
        session.commit()
        job = UploadJob(
            account_id=account.id,
            processed_path="C:/out.mp4",
            title="A reel",
            status=status,
        )
        session.add(job)
        session.commit()
        return job.id


def test_list_jobs_includes_account_name() -> None:
    job_id = _make_job()
    rows = publish_queue.list_jobs()
    row = next(r for r in rows if r["id"] == job_id)
    assert row["account_name"] == "Movies"
    assert row["status"] == "draft"


def test_mark_posted_sets_status_url_and_metrics() -> None:
    job_id = _make_job()

    result = publish_queue.mark_posted(
        job_id,
        {"posted_url": "https://instagram.com/p/abc", "posted_views": "1200", "posted_likes": 90},
    )

    assert result["status"] == "posted"
    assert result["posted_at"] is not None
    assert result["posted_url"] == "https://instagram.com/p/abc"
    assert result["posted_views"] == 1200
    assert result["posted_likes"] == 90


def test_mark_posted_bad_metric_raises() -> None:
    job_id = _make_job()
    with pytest.raises(PublishQueueError):
        publish_queue.mark_posted(job_id, {"posted_views": "lots"})


def test_update_metrics_edits_existing_post_without_changing_post_state() -> None:
    job_id = _make_job(status="posted")

    result = publish_queue.update_metrics(
        job_id,
        {
            "posted_views": "1,200",
            "posted_likes": "90",
            "posted_comments": 12,
            "posted_shares": 7,
        },
    )

    assert result["status"] == "posted"
    assert result["posted_at"] is None
    assert result["posted_views"] == 1200
    assert result["posted_likes"] == 90
    assert result["posted_comments"] == 12
    assert result["posted_shares"] == 7


def test_update_metrics_requires_posted_job() -> None:
    job_id = _make_job()

    with pytest.raises(PublishQueueError, match="posted"):
        publish_queue.update_metrics(job_id, {"posted_views": 1200})


def test_reschedule_then_unschedule() -> None:
    job_id = _make_job()

    scheduled = publish_queue.reschedule(job_id, "2026-08-01T20:00:00")
    assert scheduled["status"] == "scheduled"
    assert scheduled["scheduled_at"] is not None

    cleared = publish_queue.unschedule(job_id)
    assert cleared["status"] == "draft"
    assert cleared["scheduled_at"] is None


def test_reschedule_failed_job_revives_it_and_clears_error() -> None:
    job_id = _make_job(status="failed")
    with get_session() as session:
        session.get(UploadJob, job_id).error_message = "not logged in"
        session.commit()

    result = publish_queue.reschedule(job_id, "2026-08-01T20:00:00")

    assert result["status"] == "scheduled"
    with get_session() as session:
        # The retry budget in publish_now keys off error_message; a revived job
        # must start clean or its next failure skips the automatic retry.
        assert session.get(UploadJob, job_id).error_message is None


def test_reschedule_posted_job_raises() -> None:
    job_id = _make_job()
    publish_queue.mark_posted(job_id, {})
    with pytest.raises(PublishQueueError):
        publish_queue.reschedule(job_id, "2026-08-01T20:00:00")


def test_remove_job() -> None:
    job_id = _make_job()
    result = publish_queue.remove_job(job_id)
    assert result["removed_job_id"] == job_id
    with get_session() as session:
        assert session.get(UploadJob, job_id) is None


def test_unknown_job_raises() -> None:
    with pytest.raises(PublishQueueError):
        publish_queue.mark_posted(99999, {})

def test_iso_serialization_attaches_utc_offset_to_naive_db_datetimes() -> None:
    """SQLite returns naive datetimes; the API must emit an explicit UTC
    offset or the frontend parses the time as local and every displayed
    schedule shifts by the machine's UTC offset (reported 2026-06-11:
    a 10:11 UTC job rendered as 10:11 AM local in Multi-Account Publish)."""
    import datetime as dt

    from nicheflow_studio.services import publish_queue, publishing, publishing_dashboard

    naive = dt.datetime(2026, 6, 11, 10, 11, 53)
    for service in (publish_queue, publishing, publishing_dashboard):
        rendered = service._iso(naive)
        assert rendered is not None and rendered.endswith("+00:00"), (service.__name__, rendered)
        parsed = dt.datetime.fromisoformat(rendered)
        assert parsed.utcoffset() == dt.timedelta(0)
