from __future__ import annotations

import datetime as dt
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    MediaAsset,
    PoolItem,
    UploadJob,
)
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import publishing
from nicheflow_studio.services.publishing import PublishError


def _make_item(
    *,
    processed_path: str | None = "C:/processed/out.mp4",
    with_account: bool = True,
    title_draft: str | None = "Chosen title",
    caption_draft: str | None = "A caption.",
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
            caption_draft=caption_draft,
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


def test_account_min_gap_minutes_default_and_override() -> None:
    account = Account(name="Gap", platform="instagram")
    assert publishing.account_min_gap_minutes(account) == 210  # default 3.5h
    account.upload_min_gap_minutes = 300
    assert publishing.account_min_gap_minutes(account) == 300
    account.upload_min_gap_minutes = 0  # invalid -> fall back to default
    assert publishing.account_min_gap_minutes(account) == 210


def test_default_gap_clears_exact_4h_slots() -> None:
    # Regression: slots exactly 4h apart (the 6/day layout). A post at 09:00 sits
    # exactly 4h from the 13:00 slot. The old flat 240-min default rejected 13:00
    # (inclusive boundary, widened by jitter) and silently halved the cadence to
    # 3/day. The 210 default now clears the adjacent slot with NO per-account
    # override, while a wider override can still force posts to spread.
    after = dt.datetime(2026, 6, 13, 9, 30, tzinfo=dt.timezone.utc)
    with get_session() as session:
        account = Account(
            name="Six",
            platform="instagram",
            niche_label="history",
            upload_schedule_slots="09:00, 13:00, 17:00",
        )
        session.add(account)
        session.commit()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path="C:/out.mp4",
                status="posted",
                posted_at=dt.datetime(2026, 6, 13, 9, 0, tzinfo=dt.timezone.utc),
            )
        )
        session.commit()

        # Default gap: the adjacent 4h slot (13:00) is now reachable.
        default_slot = publishing.next_safe_slot_for_account(
            session, account, after=after, rng=random.Random(0)
        )
        assert default_slot.astimezone(dt.timezone.utc).hour == 13

        # A wider explicit override still spreads posts past the adjacent slot.
        account.upload_min_gap_minutes = 300  # 5h
        session.commit()
        wide_slot = publishing.next_safe_slot_for_account(
            session, account, after=after, rng=random.Random(0)
        )
        assert wide_slot.astimezone(dt.timezone.utc).hour == 17


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


def test_deferred_handoff_pushes_in_the_background_instead_of_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Batch exports pipeline the upload against the next render, so the push must
    # be spawned rather than awaited -- the job stays 'scheduled' until it lands.
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(item_id))
    monkeypatch.setattr(
        cloud_publisher,
        "schedule_reel",
        lambda **_kwargs: pytest.fail("the upload must not run on the calling thread"),
    )
    spawned: list[int] = []
    monkeypatch.setattr(publishing, "_spawn_handoff_retry", spawned.append)

    with publishing.deferred_cloud_handoff():
        result = publishing.queue_for_publish(item_id, scheduled_at="2026-06-16T02:00:00+00:00")

    assert spawned == [result["job_id"]]
    assert result["cloud_handoff"] == "deferred"
    assert result["status"] == "scheduled"
    with get_session() as session:
        assert session.get(UploadJob, result["job_id"]).status == "scheduled"


def test_deferred_handoff_does_not_leak_past_its_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An export outside a batch must still get its final 'cloud' status back
    # synchronously, including after a batch raised partway through.
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(item_id))
    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    monkeypatch.setattr(
        cloud_publisher, "schedule_reel", lambda **_kwargs: {"id": "w", "status": "scheduled"}
    )
    monkeypatch.setattr(
        publishing,
        "_spawn_handoff_retry",
        lambda job_id: pytest.fail("handoff must be synchronous outside the block"),
    )

    with pytest.raises(RuntimeError):
        with publishing.deferred_cloud_handoff():
            raise RuntimeError("batch blew up mid-loop")

    result = publishing.queue_for_publish(item_id, scheduled_at="2026-06-16T02:00:00+00:00")

    assert result["status"] == "cloud"


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


def test_cloud_handoff_push_failure_keeps_job_scheduled_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push failure (Worker unreachable, upload timeout) must NOT mark the job
    failed: the schedule is valid, only the push didn't land. It stays
    'scheduled' with the error recorded so the sync stray sweep re-pushes it."""
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
        assert job.status == "scheduled"
        assert "auto-retrying" in (job.error_message or "")


def test_cloud_handoff_raw_network_error_keeps_job_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a raw TimeoutError escaping the Worker client (read timeout
    during list_jobs) used to bypass all handling — the job silently stayed
    local 'scheduled' with no error and the whole export job crashed."""
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(item_id))

    def raw_timeout():
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(cloud_publisher, "list_jobs", raw_timeout)

    with pytest.raises(PublishError, match="Cloud handoff failed"):
        publishing.queue_for_publish(item_id, scheduled_at="2026-06-16T02:00:00+00:00")

    with get_session() as session:
        job = session.scalars(select(UploadJob)).first()
        assert job.status == "scheduled"
        assert "auto-retrying" in (job.error_message or "")


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


def test_cloud_handoff_serializes_concurrent_requests_for_same_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    account_id = _account_id_of(item_id)
    _enable_cloud(monkeypatch, account_id)
    with get_session() as session:
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path="C:/processed/out.mp4",
            title="Chosen title",
            description="A caption.",
            status="scheduled",
            scheduled_at=dt.datetime(2026, 6, 21, 4, 0, tzinfo=dt.timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    first_upload_started = threading.Event()
    release_first_upload = threading.Event()
    schedule_calls: list[str] = []

    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})

    def schedule_reel(**kwargs) -> dict:
        schedule_calls.append(kwargs["external_id"])
        if len(schedule_calls) == 1:
            first_upload_started.set()
            assert release_first_upload.wait(timeout=2)
        return {}

    monkeypatch.setattr(cloud_publisher, "schedule_reel", schedule_reel)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publishing.handoff_scheduled_job_to_cloud, job_id)
        assert first_upload_started.wait(timeout=2)
        second = executor.submit(publishing.handoff_scheduled_job_to_cloud, job_id)
        time.sleep(0.1)
        release_first_upload.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert len(schedule_calls) == 1
    assert sorted(results, key=lambda value: value or "") == [None, "cloud"]
    with get_session() as session:
        assert session.get(UploadJob, job_id).status == "cloud"


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
        assert posted.cloud_status == "published"
        failed = session.get(UploadJob, id2)
        assert failed.status == "failed"
        assert "boom" in (failed.error_message or "")
        assert failed.cloud_status == "failed"
        assert failed.cloud_error == "boom"


def test_sync_cloud_jobs_marks_assignment_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cloud-published job closes its originating assignment, exactly like the
    local publish paths. Stuck 'assigned' rows inflate the per-account backlog
    count, so top-up stops feeding accounts that publish via the Worker."""
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account_id = item.account_id
        asset = MediaAsset(
            platform="instagram",
            canonical_source_url=item.source_url,
            download_status="downloaded",
        )
        session.add(asset)
        session.flush()
        pool_item = PoolItem(media_asset_id=asset.id, niche="history", acceptance_status="accepted")
        session.add(pool_item)
        session.flush()
        assignment = Assignment(
            pool_item_id=pool_item.id,
            account_id=account_id,
            niche="history",
            status="assigned",
        )
        session.add(assignment)
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path="C:/a.mp4",
            status="cloud",
        )
        session.add(job)
        session.commit()
        job_id, assignment_id = job.id, assignment.id

    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {
            "jobs": [
                {
                    "external_id": f"nf-{job_id}-100",
                    "status": "published",
                    "published_at": "2026-06-16T02:00:00Z",
                }
            ]
        },
    )

    result = publishing.sync_cloud_jobs()

    assert result == {"synced": True, "updated": 1}
    with get_session() as session:
        refreshed = session.get(Assignment, assignment_id)
        assert refreshed.status == "posted"
        assert refreshed.upload_job_id == job_id


def test_sync_cloud_jobs_persists_cloud_status_and_error_for_gated_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job blocked by the Worker's daily-limit/cooldown gate stays local status
    'cloud' (it's still pending, not a failure) but its raw Worker status/error
    must still be mirrored on every sync so the dashboard can show *why*."""
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    with get_session() as session:
        account_id = session.get(DownloadItem, item_id).account_id
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path="C:/gated.mp4",
            status="cloud",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {
            "jobs": [
                {
                    "external_id": f"nf-{job_id}-500",
                    "status": "scheduled",
                    "error_message": "daily limit reached (4/4 in 24h)",
                }
            ]
        },
    )

    result = publishing.sync_cloud_jobs()

    # `updated` counts the pre-existing "note changed" signal (still 'cloud',
    # not a failure); cloud_status/cloud_error are the new fields this test targets.
    assert result == {"synced": True, "updated": 1}
    with get_session() as session:
        gated = session.get(UploadJob, job_id)
        assert gated.status == "cloud"
        assert gated.cloud_status == "scheduled"
        assert gated.cloud_error == "daily limit reached (4/4 in 24h)"
        assert gated.error_message == "daily limit reached (4/4 in 24h)"


def test_sync_cloud_jobs_persists_cloud_status_change_with_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cloud_status must be written even when there's no error to report and the
    local `note`/status don't change -- e.g. 'scheduled' -> 'processing' with no
    error_message at all, which the old `updated` counter never tracked."""
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    with get_session() as session:
        account_id = session.get(DownloadItem, item_id).account_id
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path="C:/processing.mp4",
            status="cloud",
            cloud_status="scheduled",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {"jobs": [{"external_id": f"nf-{job_id}-600", "status": "processing"}]},
    )

    result = publishing.sync_cloud_jobs()

    assert result == {"synced": True, "updated": 0}
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        assert job.status == "cloud"
        assert job.cloud_status == "processing"
        assert job.cloud_error is None


def _make_stray_scheduled_job(
    item_id: int, *, scheduled_at: dt.datetime | None = None
) -> int:
    """A local 'scheduled' job on a cloud-mapped account whose push never landed."""
    with get_session() as session:
        job = UploadJob(
            account_id=_account_id_of(item_id),
            download_item_id=item_id,
            processed_path="C:/processed/out.mp4",
            title="Chosen title",
            description="A caption.",
            status="scheduled",
            scheduled_at=scheduled_at
            or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=4)),
        )
        session.add(job)
        session.commit()
        return job.id


def test_sync_cloud_jobs_adopts_stray_with_live_worker_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash AFTER the push landed but BEFORE the local 'cloud' flip committed:
    the sweep must adopt the stray (not re-push — that would double-book)."""
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(item_id))
    job_id = _make_stray_scheduled_job(item_id)

    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {"jobs": [{"external_id": f"nf-{job_id}-100", "status": "scheduled"}]},
    )
    spawned: list[int] = []
    monkeypatch.setattr(publishing, "_spawn_handoff_retry", spawned.append)

    result = publishing.sync_cloud_jobs()

    assert result == {"synced": True, "updated": 1}
    assert spawned == []
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        assert job.status == "cloud"
        assert job.cloud_status == "scheduled"


def test_sync_cloud_jobs_repushes_stray_without_worker_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash BEFORE the push landed (e.g. the read-timeout strand): the sweep
    re-pushes the stray in the background so it still reaches the Worker."""
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(item_id))
    job_id = _make_stray_scheduled_job(item_id)

    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    spawned: list[int] = []
    monkeypatch.setattr(publishing, "_spawn_handoff_retry", spawned.append)

    publishing.sync_cloud_jobs()

    assert spawned == [job_id]
    with get_session() as session:
        assert session.get(UploadJob, job_id).status == "scheduled"  # until the push lands


def test_sync_cloud_jobs_leaves_old_and_unmapped_strays_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No auto-push for (a) strays older than the 48h cutoff (stale content must
    not surprise-post) and (b) jobs on accounts that aren't cloud-mapped."""
    from nicheflow_studio.services import cloud_publisher

    stale_item = _make_item()
    _enable_cloud(monkeypatch, _account_id_of(stale_item))
    stale_id = _make_stray_scheduled_job(
        stale_item, scheduled_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)
    )
    unmapped_item = _make_item()  # separate account, not in the cloud map
    unmapped_id = _make_stray_scheduled_job(unmapped_item)

    monkeypatch.setattr(cloud_publisher, "list_jobs", lambda: {"jobs": []})
    spawned: list[int] = []
    monkeypatch.setattr(publishing, "_spawn_handoff_retry", spawned.append)

    publishing.sync_cloud_jobs()

    assert spawned == []
    with get_session() as session:
        assert session.get(UploadJob, stale_id).status == "scheduled"
        assert session.get(UploadJob, unmapped_id).status == "scheduled"


def test_list_cloud_jobs_joins_account_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    with get_session() as session:
        account = Account(name="Resurfaced History", platform="instagram")
        session.add(account)
        session.commit()
        account_id = account.id

    _enable_cloud(monkeypatch, account_id, worker_key="resurfacedhistory")
    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {
            "publish_mode": "live",
            "jobs": [
                {
                    "id": "worker-job-1",
                    "external_id": "nf-42-100",
                    "account_key": "resurfacedhistory",
                    "status": "scheduled",
                    "scheduled_at": "2026-07-03T02:03:00Z",
                    "attempts": 0,
                }
            ],
        },
    )

    result = publishing.list_cloud_jobs()

    assert result["publish_mode"] == "live"
    assert len(result["jobs"]) == 1
    job = result["jobs"][0]
    assert job["account_name"] == "Resurfaced History"
    assert job["upload_job_id"] == 42


def test_list_cloud_jobs_noop_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_API_KEY", raising=False)
    assert publishing.list_cloud_jobs() == {"jobs": [], "publish_mode": None}


def test_sync_cloud_jobs_marks_clean_cloud_failure_for_manual_local_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    scheduled_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    with get_session() as session:
        account_id = session.get(DownloadItem, item_id).account_id
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path="C:/fallback.mp4",
            status="cloud",
            scheduled_at=scheduled_at,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret")
    monkeypatch.setattr(
        cloud_publisher,
        "list_jobs",
        lambda: {
            "jobs": [
                {
                    "external_id": f"nf-{job_id}-300",
                    "status": "manual_local_available",
                    "error_message": "cloud blocked; manual browser publish available: API access blocked",
                }
            ]
        },
    )

    result = publishing.sync_cloud_jobs()

    assert result == {"synced": True, "updated": 1}
    with get_session() as session:
        blocked = session.get(UploadJob, job_id)
        assert blocked.status == "failed"
        assert publishing._aware(blocked.scheduled_at) == scheduled_at
        assert "API access blocked" in (blocked.error_message or "")


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


def test_queue_with_schedule_requires_final_caption() -> None:
    item_id = _make_item(caption_draft=None)

    with pytest.raises(PublishError, match="final caption"):
        publishing.queue_for_publish(item_id, scheduled_at="2026-07-01T20:00:00")


def test_queue_with_schedule_rejects_placeholder_source_title() -> None:
    item_id = _make_item(title_draft=None)
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.title = "Video by theanomalists"
        session.commit()

    with pytest.raises(PublishError, match="final title"):
        publishing.queue_for_publish(item_id, scheduled_at="2026-07-01T20:00:00")


def test_queue_skips_resting_account() -> None:
    item_id = _make_item()
    account_id = _account_id_of(item_id)
    with get_session() as session:
        account = session.get(Account, account_id)
        account.operational_status = "resting"
        session.commit()

    with pytest.raises(PublishError, match="resting, not active"):
        publishing.queue_for_publish(item_id)

    assert publishing.list_publish_jobs(item_id) == []


def test_cloud_handoff_fails_stale_job_without_caption(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicheflow_studio.services import cloud_publisher

    item_id = _make_item()
    account_id = _account_id_of(item_id)
    _enable_cloud(monkeypatch, account_id)
    monkeypatch.setattr(cloud_publisher, "schedule_reel", lambda **_kwargs: pytest.fail("no handoff"))

    with get_session() as session:
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path="C:/stale.mp4",
            title="Video by theanomalists",
            description=None,
            status="scheduled",
            scheduled_at=dt.datetime(2026, 7, 1, 20, 0, tzinfo=dt.timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with pytest.raises(PublishError, match="final title"):
        publishing.handoff_scheduled_job_to_cloud(job_id)

    with get_session() as session:
        failed = session.get(UploadJob, job_id)
        assert failed.status == "failed"
        assert "final title" in (failed.error_message or "")


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


def test_concurrent_auto_schedule_never_double_books_a_slot() -> None:
    """Bulk export runs one thread per item; simultaneous auto-schedules for the
    same account must serialize so no two posts claim the same slot."""
    first_item = _make_item(processed_path="C:/processed/a.mp4")
    account_id = _account_id_of(first_item)
    with get_session() as session:
        session.get(Account, account_id).upload_schedule_slots = "09:00, 13:00, 18:00"
        session.commit()
        item_ids = [first_item]
        for index in range(3):
            item = DownloadItem(
                source_url=f"https://instagram.com/reel/race{index}",
                title="Source",
                title_draft="Chosen title",
                caption_draft="A caption.",
                file_path="C:/clips/x.mp4",
                processed_path=f"C:/processed/race{index}.mp4",
                status="completed",
                account_id=account_id,
            )
            session.add(item)
            session.commit()
            item_ids.append(item.id)

    with ThreadPoolExecutor(max_workers=len(item_ids)) as pool:
        results = list(pool.map(publishing.auto_schedule_for_publish, item_ids))

    scheduled_times = [result["scheduled_at"] for result in results]
    assert all(scheduled_times)
    assert len(set(scheduled_times)) == len(item_ids)


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


def test_auto_schedule_does_not_reuse_failed_past_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-queuing a failed post must NOT cling to its old, now-past slot — that
    made the cloud fire it immediately. It should get a fresh future slot."""
    now = dt.datetime(2026, 6, 16, 10, 0, tzinfo=dt.timezone.utc)
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.upload_schedule_slots = "09:00, 18:00"
        # Original post failed at a slot that is now hours in the past.
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=item.processed_path,
                status="failed",
                scheduled_at=dt.datetime(2026, 6, 16, 3, 2, tzinfo=dt.timezone.utc),
            )
        )
        session.commit()
    monkeypatch.setattr(publishing, "_account_in_checkpoint_cooldown", lambda *_a, **_k: False)

    result = publishing.auto_schedule_for_publish(item_id, now=now, rng=random.Random(7))

    scheduled = dt.datetime.fromisoformat(result["scheduled_at"])
    assert result["schedule_path"] != "kept_existing"
    assert scheduled > now  # never a past time, so the cloud won't fire it instantly


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
