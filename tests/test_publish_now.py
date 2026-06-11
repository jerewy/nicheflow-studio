from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nicheflow_studio.db.assignments import assignment_counts_by_account, distribute_niche
from nicheflow_studio.db.media_library import find_or_register_media_asset
from nicheflow_studio.db.models import Account, Assignment, DownloadItem, PoolItem, UploadJob
from nicheflow_studio.db.pools import accept_into_pool
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import publish_now
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.services.publish_now import PublishNowError


def _make_account(*, profile: str | None = "main") -> int:
    with get_session() as session:
        account = Account(
            name="Past Moments",
            platform="instagram",
            niche="history",
            instagram_profile=profile,
        )
        session.add(account)
        session.commit()
        return account.id


def _make_exported_item(account_id: int) -> int:
    with get_session() as session:
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="T",
            title_draft="Title",
            caption_draft="Caption",
            file_path="C:/v.mp4",
            processed_path="C:/out.mp4",
            status="completed",
            account_id=account_id,
        )
        session.add(item)
        session.commit()
        return item.id


def _fake_result(status: str, *, posted_url: str | None = None, error: str | None = None):
    return SimpleNamespace(status=status, posted_url=posted_url, error_message=error)


def test_publish_item_now_marks_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    seen: dict = {}

    def fake(profile, video, caption):
        seen.update(profile=profile, video=video, caption=caption)
        return _fake_result("posted", posted_url="https://instagram.com/p/XYZ/")

    monkeypatch.setattr(publish_now, "_do_publish_reel", fake)

    result = publish_now.publish_item_now(item_id)

    assert result["status"] == "posted"
    assert result["posted_url"] == "https://instagram.com/p/XYZ/"
    # Posted with the account's profile, the exported file, and the caption.
    assert seen == {"profile": "main", "video": "C:/out.mp4", "caption": "Caption"}
    with get_session() as session:
        job = session.scalars(select(UploadJob)).first()
        assert job.status == "posted"
        assert job.posted_at is not None
        assert job.posted_url == "https://instagram.com/p/XYZ/"


def test_posted_job_releases_assignment_and_next_distribute_backfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _make_account()
    with get_session() as session:
        assets = []
        for code in ("abc", "next"):
            asset, _ = find_or_register_media_asset(
                session,
                source_url=f"https://instagram.com/reel/{code}",
                shortcode=code,
            )
            accept_into_pool(session, media_asset=asset, niche="history")
            assets.append(asset)
        session.commit()
        first = distribute_niche(session, "history", max_per_account=1)
        session.commit()
        assert len(first) == 1
        assigned_pool = session.get(PoolItem, first[0].pool_item_id)
        assigned_asset = assigned_pool.media_asset
        assigned_url = assigned_asset.canonical_source_url
        assigned_shortcode = assigned_asset.source_shortcode
    item_id = _make_exported_item(account_id)
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.source_url = assigned_url
        item.video_id = assigned_shortcode
        session.commit()
    monkeypatch.setattr(
        publish_now,
        "_do_publish_reel",
        lambda *_args: _fake_result("posted", posted_url="https://instagram.com/p/posted/"),
    )

    publish_now.publish_item_now(item_id)

    with get_session() as session:
        assignment = session.query(Assignment).one()
        assert assignment.status == "posted"
        assert assignment_counts_by_account(session, "history").get(account_id, 0) == 0
        assert len(distribute_niche(session, "history", max_per_account=1)) == 1
        session.commit()
        assert assignment_counts_by_account(session, "history")[account_id] == 1

    # Re-post lifecycle sync is idempotent.
    with get_session() as session:
        job = session.query(UploadJob).filter(UploadJob.download_item_id == item_id).one()
        from nicheflow_studio.db.assignments import mark_assignment_posted_for_job

        assert mark_assignment_posted_for_job(session, job) == 0


def test_publish_item_now_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("failed", error="boom")
    )

    result = publish_now.publish_item_now(item_id)

    assert result["status"] == "failed"
    with get_session() as session:
        job = session.scalars(select(UploadJob)).first()
        assert job.status != "posted"
        assert job.posted_at is None
        assert "boom" in (job.error_message or "")


def test_publish_item_now_requires_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = _make_account(profile=None)
    item_id = _make_exported_item(account_id)
    monkeypatch.setattr(
        publish_now,
        "_do_publish_reel",
        lambda p, v, c: pytest.fail("must not post without a profile"),
    )
    with pytest.raises(PublishNowError):
        publish_now.publish_item_now(item_id)


def test_publish_item_now_requires_export() -> None:
    account_id = _make_account()
    with get_session() as session:
        item = DownloadItem(
            source_url="u", file_path="C:/v.mp4", status="completed", account_id=account_id
        )
        session.add(item)
        session.commit()
        item_id = item.id
    with pytest.raises(ServiceError):  # queue_for_publish rejects an un-exported item
        publish_now.publish_item_now(item_id)


def test_due_count_and_publish_due(monkeypatch: pytest.MonkeyPatch) -> None:
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    with get_session() as session:
        session.add_all(
            [
                UploadJob(
                    account_id=account_id,
                    download_item_id=item_id,
                    processed_path="C:/out.mp4",
                    description="Cap",
                    status="scheduled",
                    scheduled_at=past,
                ),
                UploadJob(
                    account_id=account_id,
                    download_item_id=item_id,
                    processed_path="C:/out2.mp4",
                    status="scheduled",
                    scheduled_at=future,
                ),
            ]
        )
        session.commit()

    assert publish_now.due_count() == 1  # only the past one is due

    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("posted", posted_url="https://x/p/1/")
    )
    summary = publish_now.publish_due_jobs()

    assert summary["due"] == 1
    assert summary["posted"] == 1
    assert summary["failed"] == 0
    assert publish_now.due_count() == 0  # past job posted; future not due


def _make_due_job(account_id: int, item_id: int, *, path: str = "C:/out.mp4") -> int:
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    with get_session() as session:
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path=path,
            description="Cap",
            status="scheduled",
            scheduled_at=past,
        )
        session.add(job)
        session.commit()
        return job.id


def test_failed_scheduled_job_retries_once_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    job_id = _make_due_job(account_id, item_id)
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("failed", error="boom")
    )
    monkeypatch.setattr(publish_now, "_sleep", lambda s: None)

    publish_now.publish_due_jobs()

    # First failure: still scheduled, but pushed into the future (one retry).
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        assert job.status == "scheduled"
        assert "boom" in (job.error_message or "")
        retry_at = job.scheduled_at.replace(tzinfo=dt.timezone.utc)
    assert retry_at > dt.datetime.now(dt.timezone.utc)
    assert publish_now.due_count() == 0  # not due again immediately

    # Make the retry due and fail again: the job must go to "failed" for good.
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        job.scheduled_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        session.commit()
    publish_now.publish_due_jobs()
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        assert job.status == "failed"
    assert publish_now.due_count() == 0  # no retry hammer


def test_checkpoint_fails_job_and_pauses_account(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    first = _make_due_job(account_id, item_id, path="C:/out1.mp4")
    _make_due_job(account_id, item_id, path="C:/out2.mp4")
    calls: list[str] = []

    def fake(profile, video, caption):
        calls.append(video)
        return _fake_result("checkpoint", error="checkpoint detected: challenge")

    monkeypatch.setattr(publish_now, "_do_publish_reel", fake)
    monkeypatch.setattr(publish_now, "_sleep", lambda s: None)

    publish_now.publish_due_jobs()

    # Only the first job was attempted; the checkpoint paused the whole account.
    assert calls == ["C:/out1.mp4"]
    with get_session() as session:
        job = session.get(UploadJob, first)
        assert job.status == "failed"
        assert "checkpoint" in (job.error_message or "")
    assert publish_now.due_count() == 0  # second job hidden while cooling down

    publish_now._account_cooldowns.clear()
    assert publish_now.due_count() == 1  # cooldown over -> second job due again


def test_randomized_gap_between_batch_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    _make_due_job(account_id, item_id, path="C:/out1.mp4")
    _make_due_job(account_id, item_id, path="C:/out2.mp4")
    sleeps: list[float] = []
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("posted", posted_url="u")
    )
    monkeypatch.setattr(publish_now, "_sleep", sleeps.append)

    summary = publish_now.publish_due_jobs()

    assert summary["posted"] == 2
    # Exactly one gap between two posts, inside the 2-6 minute window.
    assert len(sleeps) == 1
    low, high = publish_now._INTER_POST_GAP_SECONDS
    assert low <= sleeps[0] <= high


def test_auto_publish_toggle() -> None:
    assert publish_now.auto_publish_enabled() is False
    publish_now.set_auto_publish_enabled(True)
    assert publish_now.auto_publish_enabled() is True
    publish_now.set_auto_publish_enabled(False)
    assert publish_now.auto_publish_enabled() is False
