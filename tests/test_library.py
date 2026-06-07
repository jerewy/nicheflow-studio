from __future__ import annotations

import pytest
from sqlalchemy import select

from nicheflow_studio.db.models import (
    Account,
    DownloadItem,
    DraftRevision,
    ScrapeCandidate,
    UploadJob,
)
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import library
from nicheflow_studio.services.library import LibraryError


def _make_account(name: str = "Acc") -> int:
    with get_session() as session:
        account = Account(name=name, platform="instagram")
        session.add(account)
        session.commit()
        return account.id


def _make_item(*, account_id: int | None = None, file_path: str | None = "C:/x.mp4") -> int:
    with get_session() as session:
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Clip",
            file_path=file_path,
            status="completed",
            account_id=account_id,
        )
        session.add(item)
        session.commit()
        return item.id


def test_list_items_includes_account_name_and_flags() -> None:
    account_id = _make_account("Movies")
    item_id = _make_item(account_id=account_id)

    rows = library.list_items()

    row = next(r for r in rows if r["id"] == item_id)
    assert row["account_name"] == "Movies"
    assert row["has_file"] is True
    assert row["has_processed"] is False


def test_assign_and_clear_account() -> None:
    account_id = _make_account()
    item_id = _make_item(account_id=None)

    assigned = library.assign_account(item_id, account_id)
    assert assigned["account_id"] == account_id

    cleared = library.assign_account(item_id, None)
    assert cleared["account_id"] is None


def test_assign_unknown_account_raises() -> None:
    item_id = _make_item()
    with pytest.raises(LibraryError):
        library.assign_account(item_id, 99999)


def test_remove_item_cleans_dependents() -> None:
    account_id = _make_account()
    item_id = _make_item(account_id=account_id)
    with get_session() as session:
        session.add(
            ScrapeCandidate(
                scrape_source_url="https://s",
                source_url="https://x",
                state="queued",
                queued_download_item_id=item_id,
                account_id=account_id,
            )
        )
        session.add(
            DraftRevision(
                download_item_id=item_id,
                revision_number=1,
                title_options='["t1"]',
                caption_options='["c1"]',
            )
        )
        session.add(
            UploadJob(account_id=account_id, download_item_id=item_id, processed_path="C:/o.mp4")
        )
        session.commit()

    result = library.remove_item(item_id)

    assert result["removed_item_id"] == item_id
    assert result["deleted_revisions"] == 1
    with get_session() as session:
        assert session.get(DownloadItem, item_id) is None
        candidate = session.scalars(select(ScrapeCandidate)).first()
        assert candidate.queued_download_item_id is None
        assert candidate.state == "candidate"
        job = session.scalars(select(UploadJob)).first()
        assert job.download_item_id is None
