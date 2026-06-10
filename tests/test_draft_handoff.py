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


def test_build_chat_prompt_carries_title_rules_and_hook_framing() -> None:
    # The chat path must share the live prompt's title rules (history account
    # with no explicit style auto-routes to the history hook rules) and the
    # green/yellow/red hook framing, so chat models write to the same contract.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "On-screen title rules" in prompt
    assert "NAMES the concrete visible subject" in prompt
    assert "COMMENT TEST" in prompt
    assert "GREEN" in prompt and "YELLOW" in prompt and "RED" in prompt


def test_build_chat_prompt_guards_blind_chat_models() -> None:
    # A chat model without file access must be told to use only the provided
    # signals and to ask the user rather than guess at an unidentified subject.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "CANNOT open the local video" in prompt
    assert "ask the user for a one-line description" in prompt
    assert "never bold or markdown-formatted" in prompt


def test_build_chat_prompt_bans_dashes_and_avoids_pipe_corruption() -> None:
    # Dash ban: long dashes read as AI-generated copy. Handoff instruction:
    # agents must save via --file, not the Get-Content pipe that corrupts
    # em dashes/emoji before Python sees them.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "Never write em dashes or double hyphens" in prompt
    assert f"save --item-id {item_id} --file" in prompt
    assert "Do NOT pipe the JSON through Get-Content" in prompt
    assert "Get-Content <draft-json-file> |" not in prompt


def test_build_chat_prompt_requires_same_index_recommendation() -> None:
    # The app applies title+caption as one unit; cross-paired recommendations
    # (Title 2 + Caption 3) render misleadingly, so the contract forbids them.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "MUST share the same option number" in prompt
    assert "never recommend Title 2 + Caption 3" in prompt
    assert "MUST be equal to each other" in prompt
    assert "never a file path" in prompt


def test_build_chat_prompt_includes_stored_vision_payload() -> None:
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.smart_vision_payload = '{"on_screen_hook": "man rides scooter with tent"}'
        session.commit()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "man rides scooter with tent" in prompt
    assert "Visual evidence JSON" in prompt


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
