from __future__ import annotations

import datetime as dt
import random

import pytest
from sqlalchemy import select

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import publishing
from nicheflow_studio.services.publishing import PublishError


def _make_item(
    *,
    processed_path: str | None = "C:/processed/out.mp4",
    with_account: bool = True,
    title_draft: str | None = "Chosen title",
) -> int:
    with get_session() as session:
        account_id = None
        if with_account:
            account = Account(name="Acc", platform="instagram", niche_label="movie")
            session.add(account)
            session.commit()
            account_id = account.id
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Source",
            title_draft=title_draft,
            caption_draft="A caption.",
            file_path="C:/clips/x.mp4",
            processed_path=processed_path,
            status="completed",
            account_id=account_id,
        )
        session.add(item)
        session.commit()
        return item.id


def test_next_safe_slot_keeps_gap_from_recent_post() -> None:
    after = dt.datetime(2026, 6, 13, 10, 0, tzinfo=dt.timezone.utc)
    with get_session() as session:
        account = Account(
            name="Spaced",
            platform="instagram",
            niche_label="history",
            upload_schedule_slots="09:00, 11:00, 18:00",
        )
        session.add(account)
        session.commit()
        # Posted 09:00 -> the 11:00 slot is only 2h later (inside the 4h gap).
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path="C:/out.mp4",
                status="posted",
                posted_at=dt.datetime(2026, 6, 13, 9, 0, tzinfo=dt.timezone.utc),
            )
        )
        session.commit()

        slot = publishing.next_safe_slot_for_account(
            session, account, after=after, rng=random.Random(0)
        )

    assert slot is not None
    # 11:00 is rejected (2h gap); 18:00 (9h gap) is the first safe slot.
    assert slot.astimezone(dt.timezone.utc).hour == 18


def _account_id_of(item_id: int) -> int:
    with get_session() as session:
        return session.get(DownloadItem, item_id).account_id


def _enable_cloud(monkeypatch: pytest.MonkeyPatch, account_id: int, worker_key: str = "testkey") -> None:
    import json as _json

    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setenv("CLOUDFLARE_PUBLISH_ACCOUNTS", _json.dumps({str(account_id): worker_key}))


def test_scheduled_job_hands_off_to_cloud_when_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    account_id = _account_id_of(item_id)
    _enable_cloud(monkeypatch, account_id)
    captured: dict = {}

    def fake_schedule_reel(**kwargs):
        captured.update(kwargs)
        return {"id": "worker-job", "status": "scheduled"}

    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(cloud_publisher, "schedule_reel", fake_schedule_reel)

    result = publishing.queue_for_publish(item_id, scheduled_at="2026-06-16T02:00:00+00:00")

    assert result["status"] == "cloud"
    assert captured["account_key"] == "testkey"
    assert captured["external_id"].startswith(f"nf-{result['job_id']}-")
    assert captured["video_path"] == "C:/processed/out.mp4"
    with get_session() as session:
        assert session.get(UploadJob, result["job_id"]).status == "cloud"


def test_scheduled_job_stays_local_when_account_not_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    # Cloud is configured, but THIS account is not in the map.
    _enable_cloud(monkeypatch, _account_id_of(item_id) + 999)

    def boom(**kwargs):
        raise AssertionError("schedule_reel must not run for an unmapped account")

    monkeypatch.setattr(cloud_publisher, "schedule_reel", boom)

    result = publishing.queue_for_publish(item_id, scheduled_at="2026-06-16T02:00:00+00:00")

    assert result["status"] == "scheduled"
    with get_session() as session:
        assert session.get(UploadJob, result["job_id"]).status == "scheduled"


def test_cloud_handoff_failure_marks_job_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher
    from nicheflow_studio.services.cloud_publisher import CloudPublisherError

    item_id = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(item_id))

    def fail(**kwargs):
        raise CloudPublisherError("Cloud publisher HTTP 500: boom")

    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(cloud_publisher, "schedule_reel", fail)

    with pytest.raises(PublishError, match="Cloud handoff failed"):
        publishing.queue_for_publish(item_id, scheduled_at="2026-06-16T02:00:00+00:00")

    with get_session() as session:
        job = session.scalars(select(UploadJob)).first()
        assert job.status == "failed"
        assert "Cloud handoff failed" in (job.error_message or "")


def test_cloud_handoff_replaces_existing_worker_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(item_id))
    canceled: list[str] = []
    captured: dict = {}

    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {
            "jobs": [
                {"id": "old", "external_id": "nf-1", "status": "scheduled"},
                {"id": "done", "external_id": "nf-1-100", "status": "published"},
            ]
        },
    )
    monkeypatch.setattr(cloud_publisher, "cancel_job", lambda worker_id: canceled.append(worker_id))
    monkeypatch.setattr(cloud_publisher, "schedule_reel", lambda **kwargs: captured.update(kwargs))

    result = publishing.queue_for_publish(item_id, scheduled_at="2026-06-16T03:00:00+00:00")

    assert result["status"] == "cloud"
    assert canceled == ["old"]
    assert captured["external_id"].startswith(f"nf-{result['job_id']}-")
    assert captured["external_id"] not in {"nf-1", "nf-1-100"}
    with get_session() as session:
        assert session.get(UploadJob, result["job_id"]).status == "cloud"


def test_sync_cloud_jobs_updates_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    with get_session() as session:
        account_id = session.get(DownloadItem, item_id).account_id
        j1 = UploadJob(account_id=account_id, download_item_id=item_id, processed_path="C:/a.mp4", status="cloud")
        j2 = UploadJob(account_id=account_id, download_item_id=item_id, processed_path="C:/b.mp4", status="cloud")
        session.add_all([j1, j2])
        session.commit()
        id1, id2 = j1.id, j2.id

    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")

    def fake_list_jobs():
        return {
            "jobs": [
                {"external_id": f"nf-{id1}-100", "status": "canceled"},
                {"external_id": f"nf-{id1}-200", "status": "published", "published_at": "2026-06-16T02:00:00Z"},
                {"external_id": f"nf-{id2}", "status": "failed", "error_message": "boom"},
            ]
        }

    monkeypatch.setattr(cloud_publisher, "list_jobs", fake_list_jobs)

    result = publishing.sync_cloud_jobs()

    assert result == {"synced": True, "updated": 2}
    with get_session() as session:
        posted = session.get(UploadJob, id1)
        assert posted.status == "posted"
        assert posted.posted_at is not None
        failed = session.get(UploadJob, id2)
        assert failed.status == "failed"
        assert "boom" in (failed.error_message or "")


def test_sync_cloud_jobs_noop_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_API_KEY", raising=False)
    assert publishing.sync_cloud_jobs() == {"synced": False, "updated": 0}


def test_queue_creates_draft_job() -> None:
    item_id = _make_item()

    result = publishing.queue_for_publish(item_id)

    assert result["created"] is True
    assert result["status"] == "draft"
    assert result["scheduled_at"] is None
    jobs = publishing.list_publish_jobs(item_id)
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Chosen title"


def test_queue_updates_existing_job_instead_of_duplicating() -> None:
    item_id = _make_item()

    publishing.queue_for_publish(item_id)
    second = publishing.queue_for_publish(item_id)

    assert second["created"] is False
    assert len(publishing.list_publish_jobs(item_id)) == 1


def test_queue_with_schedule_sets_scheduled_status() -> None:
    item_id = _make_item()

    result = publishing.queue_for_publish(item_id, scheduled_at="2026-07-01T20:00:00")

    assert result["status"] == "scheduled"
    assert result["scheduled_at"] is not None


def test_auto_schedule_uses_next_open_account_slot() -> None:
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.upload_schedule_slots = "09:00, 18:00"
        session.commit()

    result = publishing.auto_schedule_for_publish(item_id)

    assert result["status"] == "scheduled"
    scheduled = dt.datetime.fromisoformat(result["scheduled_at"])
    assert scheduled > dt.datetime.now(dt.timezone.utc)


def test_auto_schedule_hands_cloud_mapped_job_to_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    account_id = _account_id_of(item_id)
    with get_session() as session:
        session.get(Account, account_id).upload_schedule_slots = "09:00, 18:00"
        session.commit()
    _enable_cloud(monkeypatch, account_id)
    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(cloud_publisher, "schedule_reel", lambda **_kwargs: {})

    result = publishing.auto_schedule_for_publish(item_id)

    assert result["status"] == "cloud"
    with get_session() as session:
        assert session.get(UploadJob, result["job_id"]).status == "cloud"


def test_auto_schedule_keeps_existing_schedule_on_reexport() -> None:
    """Re-running auto-schedule (e.g. after a re-export) must keep the job's
    slot and refresh its content — never silently move the post later."""
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.upload_schedule_slots = "09:00, 18:00"
        session.commit()

    first = publishing.auto_schedule_for_publish(item_id)
    first_time = dt.datetime.fromisoformat(first["scheduled_at"])

    # New draft text lands on the item, then the item is re-exported.
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.title_draft = "Better title"
        session.commit()

    second = publishing.auto_schedule_for_publish(item_id)

    assert second["schedule_path"] == "kept_existing"
    assert dt.datetime.fromisoformat(second["scheduled_at"]) == first_time
    jobs = publishing.list_publish_jobs(item_id)
    assert len(jobs) == 1  # updated in place, never duplicated
    assert jobs[0]["title"] == "Better title"


def test_auto_schedule_requires_account_slots() -> None:
    item_id = _make_item()

    with pytest.raises(PublishError, match="schedule slots"):
        publishing.auto_schedule_for_publish(item_id)


def test_auto_schedule_uses_catch_up_for_recent_unused_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 6, 11, 9, 23, tzinfo=dt.timezone.utc)
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.upload_schedule_slots = "09:00, 13:00"
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path="C:/processed/already-posted.mp4",
                status="posted",
                posted_at=now - dt.timedelta(hours=12),
            )
        )
        session.commit()
    monkeypatch.setattr(publishing, "_account_in_checkpoint_cooldown", lambda *_args, **_kwargs: False)

    result = publishing.auto_schedule_for_publish(item_id, now=now, rng=random.Random(7))

    scheduled = dt.datetime.fromisoformat(result["scheduled_at"])
    assert now + dt.timedelta(minutes=5) <= scheduled <= now + dt.timedelta(minutes=20)
    assert result["schedule_path"] == "catch_up"
    assert result["message"] == f"Catch-up: scheduled for {scheduled:%H:%M} (missed 09:00 slot)"


def test_auto_schedule_falls_through_to_next_forward_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 6, 11, 9, 23, tzinfo=dt.timezone.utc)
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.upload_schedule_slots = "09:00, 13:00"
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path="C:/processed/recently-posted.mp4",
                status="posted",
                posted_at=dt.datetime(2026, 6, 11, 8, 30, tzinfo=dt.timezone.utc),
            )
        )
        session.commit()
    monkeypatch.setattr(publishing, "_account_in_checkpoint_cooldown", lambda *_args, **_kwargs: False)

    result = publishing.auto_schedule_for_publish(item_id, now=now, rng=random.Random(7))

    scheduled = dt.datetime.fromisoformat(result["scheduled_at"])
    slot = dt.datetime(2026, 6, 11, 13, 0, tzinfo=dt.timezone.utc)
    assert slot <= scheduled <= slot + dt.timedelta(minutes=15)
    assert result["schedule_path"] == "next_open_slot"


def test_queue_requires_export() -> None:
    item_id = _make_item(processed_path=None)
    with pytest.raises(PublishError):
        publishing.queue_for_publish(item_id)


def test_queue_requires_account() -> None:
    item_id = _make_item(with_account=False)
    with pytest.raises(PublishError):
        publishing.queue_for_publish(item_id)


def test_queue_rejects_already_posted() -> None:
    item_id = _make_item()
    # Mark a posted job for the same account+path.
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        session.add(
            UploadJob(
                account_id=item.account_id,
                download_item_id=item.id,
                processed_path=item.processed_path,
                status="posted",
            )
        )
        session.commit()

    with pytest.raises(PublishError):
        publishing.queue_for_publish(item_id)


def test_queue_invalid_schedule_raises() -> None:
    item_id = _make_item()
    with pytest.raises(PublishError):
        publishing.queue_for_publish(item_id, scheduled_at="not-a-date")


def test_list_items_returns_items_with_files() -> None:
    a = _make_item()
    b = _make_item()

    items = publishing.list_items()

    ids = [it["id"] for it in items]
    assert a in ids and b in ids
    assert all("has_processed" in it for it in items)
