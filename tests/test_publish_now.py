from __future__ import annotations

import datetime as dt
import json
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


def _add_recent_post(account_id: int, *, minutes_ago: int) -> None:
    """Record a prior posted job for the account, ``minutes_ago`` in the past."""
    with get_session() as session:
        session.add(
            UploadJob(
                account_id=account_id,
                processed_path="C:/old.mp4",
                status="posted",
                posted_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago),
            )
        )
        session.commit()


def test_publish_item_now_defers_when_account_posted_recently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _make_account()
    _add_recent_post(account_id, minutes_ago=30)  # inside the 4h recency window
    item_id = _make_exported_item(account_id)
    posted = {"called": False}

    def fake(*_args):
        posted["called"] = True
        return _fake_result("posted")

    monkeypatch.setattr(publish_now, "_do_publish_reel", fake)

    result = publish_now.publish_item_now(item_id)

    # Refused: returns the recency details and posts nothing.
    assert result["status"] == "on_cooldown"
    assert result["account_name"] == "Past Moments"
    assert "recommended_next_at" in result
    assert posted["called"] is False
    with get_session() as session:
        job = session.scalars(
            select(UploadJob).where(UploadJob.download_item_id == item_id)
        ).first()
        assert job.posted_at is None


def test_publish_item_now_allow_recent_overrides_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = _make_account()
    _add_recent_post(account_id, minutes_ago=30)
    item_id = _make_exported_item(account_id)
    monkeypatch.setattr(
        publish_now,
        "_do_publish_reel",
        lambda *_args: _fake_result("posted", posted_url="https://instagram.com/p/OK/"),
    )

    result = publish_now.publish_item_now(item_id, allow_recent=True)

    assert result["status"] == "posted"
    assert result["posted_url"] == "https://instagram.com/p/OK/"


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


def _make_posted_job(account_id: int, item_id: int, *, posted_at: dt.datetime) -> int:
    with get_session() as session:
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path="C:/out.mp4",
            status="posted",
            posted_at=posted_at,
        )
        session.add(job)
        session.commit()
        return job.id


def test_item_publish_recency_flags_recent_post() -> None:
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    _make_posted_job(
        account_id, item_id, posted_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    )

    result = publish_now.item_publish_recency(item_id)

    assert result["on_cooldown"] is True
    assert result["account_name"] == "Past Moments"
    assert 55 <= result["minutes_since"] <= 65
    assert result["recommended_next_at"] is not None


def test_item_publish_recency_clear_when_old_or_never() -> None:
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    # Never posted -> safe to post.
    assert publish_now.item_publish_recency(item_id)["on_cooldown"] is False
    # Last post older than the 4h window -> safe to post.
    _make_posted_job(
        account_id, item_id, posted_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
    )
    assert publish_now.item_publish_recency(item_id)["on_cooldown"] is False


def test_item_publish_recency_flags_in_flight_post() -> None:
    # A live post already running for the account -> warn immediately (so the UI
    # doesn't queue a second one behind it for minutes).
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    publish_now._mark_account_in_flight(account_id)
    try:
        result = publish_now.item_publish_recency(item_id)
    finally:
        publish_now._clear_account_in_flight(account_id)

    assert result["on_cooldown"] is True
    assert result["in_progress"] is True
    assert result["account_id"] == account_id
    # Cleared once the post finishes.
    assert publish_now.item_publish_recency(item_id)["on_cooldown"] is False


def test_record_and_drain_publish_events_round_trip() -> None:
    publish_now.drain_publish_events()  # clear anything left by other tests
    publish_now.record_publish_event(
        {"status": "posted", "job_id": 1, "item_id": 5, "account_name": "A"}
    )
    publish_now.record_publish_event(
        {"status": "posted", "job_id": 2, "item_id": 6, "account_name": "B"}
    )

    events = publish_now.drain_publish_events()

    assert [e["item_id"] for e in events] == [5, 6]
    assert all("id" in e and "at" in e for e in events)
    # Draining clears the feed so each event toasts only once.
    assert publish_now.drain_publish_events() == []


def test_due_publish_recency_lists_recent_accounts() -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    _make_posted_job(
        account_id, item_id, posted_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    )
    _make_due_job(account_id, item_id, path="C:/out2.mp4")

    warnings = publish_now.due_publish_recency()

    assert len(warnings) == 1
    assert warnings[0]["account_id"] == account_id
    assert warnings[0]["account_name"] == "Past Moments"


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
    # Two DIFFERENT accounts so both post — two same-account jobs would now defer.
    # The randomized gap is the anti-correlation pause between consecutive posts.
    account_a = _make_account()
    account_b = _make_account()
    item_a = _make_exported_item(account_a)
    item_b = _make_exported_item(account_b)
    _make_due_job(account_a, item_a, path="C:/out1.mp4")
    _make_due_job(account_b, item_b, path="C:/out2.mp4")
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


def test_publish_item_now_spaces_posts_across_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_now._account_cooldowns.clear()
    # A DIFFERENT account just posted from this machine; posting now must keep a
    # randomized cross-account gap so the two don't land back-to-back.
    other_id = _make_account()
    _make_posted_job(
        other_id, _make_exported_item(other_id), posted_at=dt.datetime.now(dt.timezone.utc)
    )
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    sleeps: list[float] = []
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("posted", posted_url="u")
    )
    monkeypatch.setattr(publish_now, "_sleep", sleeps.append)

    result = publish_now.publish_item_now(item_id)

    assert result["status"] == "posted"
    assert len(sleeps) == 1
    low, high = publish_now._INTER_POST_GAP_SECONDS
    assert low <= sleeps[0] <= high


def test_publish_item_now_no_spacing_when_other_account_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_now._account_cooldowns.clear()
    # The other account's last post is well outside the gap window -> no extra wait.
    other_id = _make_account()
    _make_posted_job(
        other_id,
        _make_exported_item(other_id),
        posted_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30),
    )
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    sleeps: list[float] = []
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("posted", posted_url="u")
    )
    monkeypatch.setattr(publish_now, "_sleep", sleeps.append)

    result = publish_now.publish_item_now(item_id)

    assert result["status"] == "posted"
    assert sleeps == []


def test_publish_due_defers_second_same_account_job(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_now._account_cooldowns.clear()
    # The backlog scenario: two due jobs for ONE account both come due at once.
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    _make_due_job(account_id, item_id, path="C:/out1.mp4")
    _make_due_job(account_id, item_id, path="C:/out2.mp4")
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("posted", posted_url="u")
    )
    monkeypatch.setattr(publish_now, "_sleep", lambda s: None)

    summary = publish_now.publish_due_jobs()

    # First posts; the second is deferred (not posted) to a safe future slot.
    assert summary["posted"] == 1
    assert summary["deferred"] == 1
    with get_session() as session:
        statuses = sorted(j.status for j in session.scalars(select(UploadJob)).all())
        assert statuses == ["posted", "scheduled"]
        deferred = session.scalars(
            select(UploadJob).where(UploadJob.status == "scheduled")
        ).first()
        assert deferred.scheduled_at.replace(tzinfo=dt.timezone.utc) > dt.datetime.now(
            dt.timezone.utc
        )


def test_publish_due_allow_recent_posts_all(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_now._account_cooldowns.clear()
    # "Publish anyway" from the dialog: the same-account gap is bypassed.
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    _make_due_job(account_id, item_id, path="C:/out1.mp4")
    _make_due_job(account_id, item_id, path="C:/out2.mp4")
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda p, v, c: _fake_result("posted", posted_url="u")
    )
    monkeypatch.setattr(publish_now, "_sleep", lambda s: None)

    summary = publish_now.publish_due_jobs(allow_recent=True)

    assert summary["posted"] == 2
    assert summary["deferred"] == 0


def _configure_cloud(monkeypatch: pytest.MonkeyPatch, account_id: int, *, key: str = "pastmomentsdaily") -> None:
    """Point the cloud client at a Worker and map ``account_id`` to a Worker key."""
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret-key")
    monkeypatch.setenv("CLOUDFLARE_PUBLISH_ACCOUNTS", json.dumps({str(account_id): key}))


def test_publish_item_now_routes_cloud_account_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    _configure_cloud(monkeypatch, account_id)

    from nicheflow_studio.services import cloud_publisher

    scheduled: dict = {}

    def fake_schedule(**kwargs):
        scheduled.update(kwargs)
        return {"id": "w1"}

    ran = {"called": False}

    def fake_run_due():
        ran["called"] = True
        return {"processed": 1, "mode": "live"}

    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(cloud_publisher, "schedule_reel", fake_schedule)
    monkeypatch.setattr(cloud_publisher, "run_due", fake_run_due)
    monkeypatch.setattr(
        publish_now,
        "_do_publish_reel",
        lambda *_a: pytest.fail("cloud-mapped account must not use the local browser"),
    )

    result = publish_now.publish_item_now(item_id)

    # Handed to the cloud (Meta Graph API via the Worker), not posted locally.
    assert result["status"] == "cloud"
    assert result["cloud"] is True
    assert result["account_name"] == "Past Moments"
    assert scheduled["account_key"] == "pastmomentsdaily"
    assert ran["called"] is True  # nudged the Worker to start immediately
    with get_session() as session:
        job = session.scalars(
            select(UploadJob).where(UploadJob.download_item_id == item_id)
        ).first()
        assert job.status == "cloud"  # local loop skips it; cloud-sync flips to posted
        assert job.posted_at is None


def test_publish_item_now_cloud_respects_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    _add_recent_post(account_id, minutes_ago=30)  # inside the 4h recency window
    item_id = _make_exported_item(account_id)
    _configure_cloud(monkeypatch, account_id)

    from nicheflow_studio.services import cloud_publisher

    monkeypatch.setattr(
        cloud_publisher, "schedule_reel", lambda **_k: pytest.fail("no cloud handoff on cooldown")
    )
    monkeypatch.setattr(
        cloud_publisher, "run_due", lambda: pytest.fail("no cloud run on cooldown")
    )
    monkeypatch.setattr(
        publish_now, "_do_publish_reel", lambda *_a: pytest.fail("must not post on cooldown")
    )

    result = publish_now.publish_item_now(item_id)

    assert result["status"] == "on_cooldown"
    assert result["account_name"] == "Past Moments"


def test_publish_item_now_cloud_allow_recent_overrides_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    _add_recent_post(account_id, minutes_ago=30)
    item_id = _make_exported_item(account_id)
    _configure_cloud(monkeypatch, account_id)

    from nicheflow_studio.services import cloud_publisher

    scheduled: dict = {}
    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(
        cloud_publisher, "schedule_reel", lambda **k: scheduled.update(k) or {"id": "w2"}
    )
    monkeypatch.setattr(cloud_publisher, "run_due", lambda: {"processed": 1, "mode": "live"})

    result = publish_now.publish_item_now(item_id, allow_recent=True)

    # Override bypasses the local recency backstop and hands off to the cloud.
    assert result["status"] == "cloud"
    assert scheduled["account_key"] == "pastmomentsdaily"


def test_publish_item_now_force_local_overrides_cloud_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_now._account_cooldowns.clear()
    account_id = _make_account()
    item_id = _make_exported_item(account_id)
    _configure_cloud(monkeypatch, account_id)

    from nicheflow_studio.services import cloud_publisher

    monkeypatch.setattr(
        cloud_publisher,
        "schedule_reel",
        lambda **_k: pytest.fail("force_local must not hand off to cloud"),
    )
    monkeypatch.setattr(
        publish_now,
        "_do_publish_reel",
        lambda *_args: _fake_result("posted", posted_url="https://instagram.com/p/local/"),
    )

    result = publish_now.publish_item_now(item_id, force_local=True)

    assert result["status"] == "posted"
    assert result["posted_url"] == "https://instagram.com/p/local/"


def test_auto_publish_toggle() -> None:
    assert publish_now.auto_publish_enabled() is False
    publish_now.set_auto_publish_enabled(True)
    assert publish_now.auto_publish_enabled() is True
    publish_now.set_auto_publish_enabled(False)
    assert publish_now.auto_publish_enabled() is False
