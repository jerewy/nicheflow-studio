from __future__ import annotations

import datetime as dt
import random

import pytest

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
