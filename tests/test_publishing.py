from __future__ import annotations

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
