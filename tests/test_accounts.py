from __future__ import annotations

import pytest

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import accounts as accounts_svc
from nicheflow_studio.services.accounts import AccountError


def test_create_requires_name() -> None:
    with pytest.raises(AccountError):
        accounts_svc.create_account({"platform": "instagram"})


def test_create_sets_fields_and_strips_handle() -> None:
    detail = accounts_svc.create_account(
        {
            "name": "Past Moments Daily",
            "platform": "instagram",
            "niche_label": "history",
            "instagram_handle": "@pastmoments",
            "writing_tone": "curious, factual",
        }
    )

    assert detail["name"] == "Past Moments Daily"
    assert detail["instagram_handle"] == "pastmoments"  # leading @ stripped
    assert detail["writing_tone"] == "curious, factual"
    assert detail["download_item_count"] == 0


def test_list_accounts_includes_created() -> None:
    a = accounts_svc.create_account({"name": "Acc A"})
    b = accounts_svc.create_account({"name": "Acc B"})
    ids = [row["id"] for row in accounts_svc.list_accounts()]
    assert a["id"] in ids and b["id"] in ids


def test_update_is_partial_and_preserves_other_fields() -> None:
    created = accounts_svc.create_account(
        {"name": "Acc", "writing_tone": "factual", "credential_blob": "secret-notes"}
    )

    updated = accounts_svc.update_account(created["id"], {"writing_tone": "playful"})

    assert updated["writing_tone"] == "playful"
    # Omitted field preserved (not wiped).
    assert updated["credential_blob"] == "secret-notes"


def test_auto_schedule_on_export_round_trips() -> None:
    created = accounts_svc.create_account(
        {"name": "Auto Schedule", "auto_schedule_on_export": True}
    )
    assert created["auto_schedule_on_export"] is True

    updated = accounts_svc.update_account(created["id"], {"auto_schedule_on_export": False})
    assert updated["auto_schedule_on_export"] is False


def test_update_unknown_raises() -> None:
    with pytest.raises(AccountError):
        accounts_svc.update_account(99999, {"name": "x"})


def test_active_account_set_get_and_clear() -> None:
    created = accounts_svc.create_account({"name": "Active Acc"})
    account_id = created["id"]

    assert accounts_svc.get_active_account() == {"active_account_id": None}

    accounts_svc.set_active_account(account_id)
    assert accounts_svc.get_active_account() == {"active_account_id": account_id}

    accounts_svc.set_active_account(None)
    assert accounts_svc.get_active_account() == {"active_account_id": None}


def test_set_active_account_unknown_raises() -> None:
    with pytest.raises(AccountError):
        accounts_svc.set_active_account(99999)


def test_get_active_account_ignores_deleted() -> None:
    created = accounts_svc.create_account({"name": "Temp"})
    accounts_svc.set_active_account(created["id"])
    accounts_svc.delete_account(created["id"])
    assert accounts_svc.get_active_account() == {"active_account_id": None}


def test_delete_unassigns_items_and_removes_jobs() -> None:
    created = accounts_svc.create_account({"name": "Acc"})
    account_id = created["id"]
    with get_session() as session:
        item = DownloadItem(
            source_url="https://x",
            file_path="C:/x.mp4",
            status="completed",
            account_id=account_id,
        )
        session.add(item)
        session.add(UploadJob(account_id=account_id, processed_path="C:/out.mp4", status="draft"))
        session.commit()
        item_id = item.id

    result = accounts_svc.delete_account(account_id)

    assert result["unassigned_download_items"] == 1
    assert result["removed_upload_jobs"] == 1
    with get_session() as session:
        assert session.get(Account, account_id) is None
        # Download item survives, just unassigned.
        survived = session.get(DownloadItem, item_id)
        assert survived is not None
        assert survived.account_id is None
