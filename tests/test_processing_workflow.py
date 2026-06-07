from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import processing_workflow


def _make_item() -> int:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        session.add(account)
        session.commit()
        item = DownloadItem(
            source_url="https://instagram.com/reel/test",
            file_path="C:/clips/test.mp4",
            account_id=account.id,
        )
        session.add(item)
        session.commit()
        return item.id


def test_history_item_gets_history_workflow_defaults() -> None:
    settings = processing_workflow.get_settings(_make_item())

    assert settings["caption_style"] == "history_lost_archive"
    assert settings["template"] == "lost_archive_black"


def test_save_settings_and_final_draft_persist() -> None:
    item_id = _make_item()

    processing_workflow.save_settings(
        item_id,
        {
            "clip_premise": "Focus on the final reveal.",
            "caption_style": "history_lost_archive",
            "title_style": "history_lost_archive",
            "template": "lost_archive_black",
        },
    )
    processing_workflow.save_final_draft(item_id, "Final title", "Final caption")

    settings = processing_workflow.get_settings(item_id)
    assert settings["clip_premise"] == "Focus on the final reveal."
    assert settings["title_draft"] == "Final title"
    assert settings["caption_draft"] == "Final caption"
