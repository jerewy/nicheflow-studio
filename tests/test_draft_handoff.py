from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import draft_handoff, draft_revisions


def _make_item() -> int:
    with get_session() as session:
        account = Account(
            name="History",
            platform="instagram",
            niche_label="history",
            title_style_notes="Use specific subject-first hooks.",
        )
        session.add(account)
        session.commit()
        item = DownloadItem(
            source_url="https://instagram.com/reel/test",
            title="Source clip",
            file_path="C:/clips/source.mp4",
            account_id=account.id,
            status="downloaded",
        )
        session.add(item)
        session.commit()
        return item.id


def test_build_chat_prompt_contains_selected_item_context() -> None:
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "C:\\clips\\source.mp4" in prompt
    assert "Niche: history" in prompt
    assert "Title Option 1:" in prompt
    assert "Cinema Bold Keywords" in prompt


def test_parse_pasted_draft_preserves_plain_title_and_recommendation() -> None:
    parsed = draft_handoff.parse_pasted_draft(
        """Title Option 1:
**A plain wrapped title**

Caption Option 1:
First paragraph.

Second paragraph.

Recommended Pick: Title Option 1 + Caption Option 1
Why: Strongest option.
Selection Notes:
Option 1: Specific and clear.
"""
    )

    assert parsed.title_options == ["A plain wrapped title"]
    assert parsed.caption_options == ["First paragraph.\n\nSecond paragraph."]
    assert parsed.recommended_title_index == 1
    assert parsed.recommended_caption_index == 1
    assert parsed.option_notes == ["Specific and clear."]


def test_import_pasted_draft_saves_revision() -> None:
    item_id = _make_item()

    saved = draft_handoff.import_pasted_draft(
        item_id,
        "Title Option 1: A title\nCaption Option 1: A caption",
    )

    assert saved.source == "clipboard"
    assert draft_revisions.latest_revision(item_id).title_options == ["A title"]
