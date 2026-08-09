from __future__ import annotations

import pytest

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import library, publish_queue, publishing
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


def test_mark_posted_marks_the_originating_assignment_posted() -> None:
    """The third publish path must free the account's slot like the other two.

    publish_now and the cloud sync each have their own coverage for this. When
    mark_posted didn't, assignments for clips posted this way stayed 'assigned'
    forever, so the account counted finished work against its distribute target
    and every later Distribute reported "all at cap" and placed nothing.
    """
    from nicheflow_studio.db.assignments import (
        assignment_counts_by_account,
        distribute_niche,
    )
    from nicheflow_studio.db.media_library import find_or_register_media_asset
    from nicheflow_studio.db.models import Assignment, PoolItem
    from nicheflow_studio.db.pools import accept_into_pool

    with get_session() as session:
        account = Account(name="HistoryQueue", platform="instagram", niche="history")
        session.add(account)
        session.commit()
        account_id = account.id

        asset, _ = find_or_register_media_asset(
            session, source_url="https://instagram.com/reel/queued", shortcode="queued"
        )
        accept_into_pool(session, media_asset=asset, niche="history")
        session.commit()
        assert len(distribute_niche(session, "history", max_per_account=1)) == 1
        session.commit()

        pool_item = session.query(PoolItem).one()
        item = DownloadItem(
            source_url=pool_item.media_asset.canonical_source_url,
            video_id=pool_item.media_asset.source_shortcode,
            account_id=account_id,
            file_path="C:/clips/queued.mp4",
            processed_path="C:/out/queued.mp4",
            status="exported",
        )
        session.add(item)
        session.commit()
        job = UploadJob(
            account_id=account_id,
            download_item_id=item.id,
            processed_path="C:/out/queued.mp4",
            status="scheduled",
        )
        session.add(job)
        session.commit()
        job_id = job.id
        assert assignment_counts_by_account(session, "history").get(account_id, 0) == 1

    publish_queue.mark_posted(job_id, {})

    with get_session() as session:
        assert session.query(Assignment).one().status == "posted"
        # Slot freed, so the next Distribute can top this account back up.
        assert assignment_counts_by_account(session, "history").get(account_id, 0) == 0


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


def test_reschedule_hands_cloud_mapped_job_to_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    job_id = _make_job()
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        account_id = job.account_id
        # The cloud handoff validates finalized draft text before uploading.
        job.description = "A final caption."
        session.commit()
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setenv("CLOUDFLARE_PUBLISH_ACCOUNTS", f'{{"{account_id}":"testkey"}}')
    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(cloud_publisher, "schedule_reel", lambda **_kwargs: {})

    result = publish_queue.reschedule(job_id, "2026-08-01T20:00:00+00:00")

    assert result["status"] == "cloud"
    with get_session() as session:
        assert session.get(UploadJob, job_id).status == "cloud"


def test_unschedule_cloud_job_cancels_worker_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    job_id = _make_job(status="cloud")
    canceled: list[str] = []
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {
            "jobs": [
                {"id": "worker-active", "external_id": f"nf-{job_id}-300", "status": "scheduled"},
                {"id": "worker-done", "external_id": f"nf-{job_id}-200", "status": "published"},
            ]
        },
    )
    monkeypatch.setattr(cloud_publisher, "cancel_job", lambda worker_id: canceled.append(worker_id))

    result = publish_queue.unschedule(job_id)

    assert canceled == ["worker-active"]
    assert result["status"] == "draft"
    assert result["scheduled_at"] is None
    with get_session() as session:
        assert session.get(UploadJob, job_id).status == "draft"


def test_unschedule_cloud_job_fails_closed_when_worker_cancel_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nicheflow_studio.services import cloud_publisher
    from nicheflow_studio.services.cloud_publisher import CloudPublisherError

    job_id = _make_job(status="cloud")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: (_ for _ in ()).throw(CloudPublisherError("worker unreachable")),
    )

    with pytest.raises(PublishQueueError, match="Cloud schedule cancel failed"):
        publish_queue.unschedule(job_id)

    with get_session() as session:
        assert session.get(UploadJob, job_id).status == "cloud"


def test_reschedule_posted_job_raises() -> None:
    job_id = _make_job()
    publish_queue.mark_posted(job_id, {})
    with pytest.raises(PublishQueueError):
        publish_queue.reschedule(job_id, "2026-08-01T20:00:00")


def test_set_processing_status_reopens_only_selected_posted_item() -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram")
        session.add(account)
        session.flush()
        item = DownloadItem(
            account_id=account.id,
            source_url="https://instagram.com/reel/history",
            file_path="C:/source.mp4",
            processed_path="C:/history.mp4",
            title="Source title",
            title_draft="Improved title",
            caption_draft="Improved caption",
            status="posted",
        )
        session.add(item)
        session.flush()
        posted = UploadJob(
            account_id=account.id,
            download_item_id=item.id,
            processed_path=item.processed_path,
            title="Original title",
            description="Original caption",
            status="posted",
            posted_at=publish_queue.dt.datetime(2026, 6, 20, tzinfo=publish_queue.dt.timezone.utc),
            posted_url="https://instagram.com/p/original",
            posted_views=1200,
        )
        session.add(posted)
        session.commit()
        item_id = item.id
        posted_job_id = posted.id

    result = publish_queue.set_processing_status(item_id, "exported")

    assert result["status"] == "exported"
    assert result["created"] is True
    with get_session() as session:
        original = session.get(UploadJob, posted_job_id)
        repost = session.get(UploadJob, result["repost_job_id"])
        item = session.get(DownloadItem, item_id)
        assert original.status == "posted"
        assert original.posted_url == "https://instagram.com/p/original"
        assert original.posted_views == 1200
        assert repost.status == "draft"
        assert repost.posted_at is None
        assert repost.title == "Improved title"
        assert repost.description == "Improved caption"
        assert item.status == "exported"

    queued = publishing.queue_for_publish(item_id)
    assert queued["job_id"] == result["repost_job_id"]
    assert queued["created"] is False

    publish_queue.mark_posted(result["repost_job_id"])
    with get_session() as session:
        assert session.get(DownloadItem, item_id).status == "posted"


def test_set_processing_status_posted_reverts_a_reopened_item() -> None:
    """Selecting Posted on a reopened item undoes the reopen: the draft repost is
    discarded, the original posted job is kept, and the derived status returns to
    posted (the user's "I switched it to Exported by mistake" recovery)."""
    from nicheflow_studio.services import library

    with get_session() as session:
        account = Account(name="History", platform="instagram")
        session.add(account)
        session.flush()
        item = DownloadItem(
            account_id=account.id,
            source_url="https://instagram.com/reel/revert",
            file_path="C:/source.mp4",
            processed_path="C:/history.mp4",
            title="Source title",
            status="posted",
        )
        session.add(item)
        session.flush()
        posted = UploadJob(
            account_id=account.id,
            download_item_id=item.id,
            processed_path=item.processed_path,
            title="Original title",
            status="posted",
            posted_at=publish_queue.dt.datetime(2026, 6, 20, tzinfo=publish_queue.dt.timezone.utc),
            posted_url="https://instagram.com/p/original",
        )
        session.add(posted)
        session.commit()
        item_id = item.id
        posted_job_id = posted.id

    # Reopen it (the mistaken "Exported" click), then revert.
    reopened = publish_queue.set_processing_status(item_id, "exported")
    repost_job_id = reopened["repost_job_id"]
    assert repost_job_id is not None
    assert library.list_items(account_id=None)[0]["reopened"] is True

    result = publish_queue.set_processing_status(item_id, "posted")

    assert result["status"] == "posted"
    assert result["repost_job_id"] is None
    with get_session() as session:
        assert session.get(UploadJob, repost_job_id) is None  # draft repost discarded
        original = session.get(UploadJob, posted_job_id)
        assert original.status == "posted"  # post history kept
        assert original.posted_url == "https://instagram.com/p/original"
        assert session.get(DownloadItem, item_id).status == "posted"
    # Derived status the table shows is back to posted, no longer reopened.
    view = library.list_items(account_id=None)[0]
    assert view["status"] == "posted"
    assert view["reopened"] is False


def test_set_processing_status_posted_rejects_never_posted_item() -> None:
    job_id = _make_job()  # a draft job, no posted history
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        download = DownloadItem(
            account_id=job.account_id,
            source_url="https://instagram.com/reel/neverposted",
            processed_path="C:/out.mp4",
            status="exported",
        )
        session.add(download)
        session.flush()
        job.download_item_id = download.id
        session.commit()
        download_id = download.id
    with pytest.raises(PublishQueueError, match="never posted"):
        publish_queue.set_processing_status(download_id, "posted")


def test_set_processing_status_rejects_operational_cloud_status() -> None:
    with pytest.raises(PublishQueueError, match="cannot be set manually"):
        publish_queue.set_processing_status(1, "cloud")


def test_set_processing_status_pending_review_clears_rejected_state() -> None:
    """Reverting a rejected clip to Pending review must clear its review_state, or
    _derive_status (which reads review_state first) snaps it back to 'rejected' and
    the change looks like a no-op on the next refresh."""
    with get_session() as session:
        account = Account(name="History", platform="instagram")
        session.add(account)
        session.flush()
        download = DownloadItem(
            account_id=account.id,
            source_url="https://instagram.com/reel/wasrejected",
            status="completed",
            review_state="rejected",
        )
        session.add(download)
        session.commit()
        download_id = download.id
        account_id = account.id

    result = publish_queue.set_processing_status(download_id, "pending_review")
    assert result["status"] == "pending_review"

    with get_session() as session:
        assert session.get(DownloadItem, download_id).review_state == "pending_review"

    # And the Processing list no longer derives it as rejected.
    row = next(r for r in library.list_items(account_id) if r["id"] == download_id)
    assert row["status"] != "rejected"


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
