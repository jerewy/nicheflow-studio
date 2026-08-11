import re
import datetime as dt

import pytest

from nicheflow_studio.db.models import AccountPostMetric, Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import smart_drafts
from nicheflow_studio.services import draft_handoff, draft_revisions, library
from nicheflow_studio.services.draft_revisions import DraftRevisionError


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


def _make_account_items(count: int = 2) -> tuple[int, list[int]]:
    with get_session() as session:
        account = Account(
            name="History Batch",
            platform="instagram",
            niche_label="history",
            title_style_notes="Use specific subject-first hooks.",
        )
        session.add(account)
        session.flush()
        ids: list[int] = []
        for index in range(1, count + 1):
            item = DownloadItem(
                source_url=f"https://instagram.com/reel/batch{index}",
                title=f"Batch source {index}",
                source_description=f"Description {index}",
                file_path=f"C:/clips/batch{index}.mp4",
                account_id=account.id,
                status="downloaded",
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
        session.commit()
        return account.id, ids


def test_build_chat_prompt_omits_follow_outro_even_with_handle() -> None:
    # The follow outro is disabled across all accounts, so even an account that
    # has a handle gets no signature line in the caption contract.
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.instagram_handle = "pastmomentsdaily"
        account.niche = "history"
        session.commit()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "Caption follow outro" not in prompt
    assert "Lost moments from history, every day" not in prompt


def test_build_chat_prompt_omits_follow_outro_without_handle() -> None:
    item_id = _make_item()  # account has no instagram_handle

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "Caption follow outro" not in prompt


def test_build_chat_prompt_contains_selected_item_context() -> None:
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "C:\\clips\\source.mp4" in prompt
    assert "Niche: history" in prompt
    assert "Title Option 1:" in prompt
    assert "Cinema Bold Keywords" in prompt


def test_build_chat_prompt_carries_title_rules_and_hook_framing() -> None:
    # The chat path must share the live prompt's title rules (history account
    # with no explicit style auto-routes to the Cinematic Record hook) and the
    # green/yellow/red hook framing, so chat models write to the same contract.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "On-screen title rules" in prompt
    assert "SIX MOVES" in prompt
    assert "ANTI-AI-TELL GUARD" in prompt
    assert "STATIC WINNER EXAMPLES" in prompt
    assert "VOICE GUARD" in prompt
    assert "GREEN" in prompt and "YELLOW" in prompt and "RED" in prompt


def test_build_chat_prompt_carries_shared_caption_rules() -> None:
    # Regression: the chat path used to emit only a bare "Caption style:" token
    # with no length/structure, so chat models returned one-line captions. It
    # must now share the live prompt's caption rules (effective_caption_rules),
    # including the per-style paragraph template and word target.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id, {"caption_style": "historytrails_archive"})
    expected_rules = smart_drafts.effective_caption_rules("historytrails_archive")

    assert "Caption rules (follow these exactly):" in prompt
    assert all(rule in prompt for rule in expected_rules)
    assert "HISTORYTRAILS template" in prompt
    assert "Instagram description copy, about" in prompt


def test_build_account_batch_chat_prompt_carries_shared_caption_rules() -> None:
    account_id, item_ids = _make_account_items(2)

    prompt = draft_handoff.build_account_batch_chat_prompt(
        account_id,
        item_ids,
        {"caption_style": "historytrails_archive"},
    )
    expected_rules = smart_drafts.effective_caption_rules("historytrails_archive")

    assert prompt.count("Caption rules (follow these exactly):") == 1
    assert all(rule in prompt for rule in expected_rules)
    assert "HISTORYTRAILS template" in prompt


def test_chat_prompts_allow_verified_web_research() -> None:
    # The chat path routes through models that can identify famous clips and
    # verify facts with web search — the one capability the Groq path lacks.
    # The old "work ONLY from the signals... STOP and ask" rule forced them to
    # treat recognizable moments as unverifiable and hedge. Both chat prompts
    # must carry the verify-then-use research rules, keep invention banned, and
    # forbid recommending an option for being the most hedged.
    item_id = _make_item()
    account_id, item_ids = _make_account_items(2)

    single = draft_handoff.build_chat_prompt(item_id)
    batch = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    for prompt in (single, batch):
        assert "Research (chat assistants with web access):" in prompt
        assert "VERIFY it with a quick web search" in prompt
        assert "which facts came from your own" in prompt
        # QUOTE RULE: a researched specific must carry a verbatim source quote,
        # or it counts as unverified (caught after a model cited outlets for a
        # date those outlets never printed).
        assert "QUOTE RULE for researched specifics" in prompt
        assert "shortest verbatim quote" in prompt
        assert "Naming an outlet without a quote does NOT count" in prompt
        assert "Never state a guess you could not verify" in prompt
        assert "safest or most hedged" in prompt
    assert "work ONLY from the signals" not in single


def test_chat_prompts_disambiguate_title_vs_caption_and_fact_check() -> None:
    # Regression: the historytrails title rules call the on-screen title a
    # "documentary caption", so chat models duplicated the title into the
    # caption slot and dumped the real caption under an unparsed "(full):"
    # header. Both chat prompts must spell the two fields apart, forbid the
    # "(full)" field, and carry the fact-check pass.
    item_id = _make_item()
    account_id, item_ids = _make_account_items(2)

    single = draft_handoff.build_chat_prompt(item_id)
    batch = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    for prompt in (single, batch):
        assert "TWO DIFFERENT texts" in prompt
        assert "Shared Caption (full):" in prompt  # named so it's explicitly banned
        assert "Fact-check pass" in prompt


def test_field_disambiguation_echoes_selected_caption_style_word_target() -> None:
    # Regression: the disambiguation block hardcoded "80-120 words across two
    # paragraphs" (the historytrails_archive shape) for every style, and since
    # it sits near the end of the prompt it contradicted and outweighed the
    # 90-150 word / 3-paragraph history_lost_archive caption rules above it.
    # It must echo the selected style's own word target and defer the
    # paragraph shape to the caption rules.
    item_id = _make_item()
    account_id, item_ids = _make_account_items(2)

    single = draft_handoff.build_chat_prompt(
        item_id, {"caption_style": "history_lost_archive"}
    )
    batch = draft_handoff.build_account_batch_chat_prompt(
        account_id, item_ids, {"caption_style": "history_lost_archive"}
    )

    for prompt in (single, batch):
        assert "about 90-150 words, in the exact paragraph structure" in prompt
        assert "80-120 words across two paragraphs" not in prompt


def test_build_chat_prompt_uses_same_title_length_rule_as_api_prompt() -> None:
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id, {"title_length": "short"})
    expected_rules = smart_drafts._title_length_rules("short")

    assert all(rule in prompt for rule in expected_rules)
    assert "Title length: Short" in prompt


def test_build_chat_prompt_uses_accounts_measured_top_titles() -> None:
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.instagram_handle = "pastmomentsdaily"
        session.add_all(
            [
                AccountPostMetric(
                    account_key="pastmomentsdaily",
                    shortcode="winner-one",
                    caption="Measured winner one",
                    conversion_score=0.8,
                    pulled_at=dt.datetime.now(dt.timezone.utc),
                ),
                AccountPostMetric(
                    account_key="pastmomentsdaily",
                    shortcode="winner-two",
                    caption="Measured winner two",
                    conversion_score=0.5,
                    pulled_at=dt.datetime.now(dt.timezone.utc),
                ),
            ]
        )
        session.commit()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "Measured winner one" in prompt
    assert "Measured winner two" in prompt


def test_build_chat_prompt_guards_blind_chat_models() -> None:
    # A chat model without file access must be told to use only the provided
    # signals and to ask the user rather than guess at an unidentified subject.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "CANNOT open the local video" in prompt
    assert "ask the user for a one-line description" in prompt
    assert "never bold or markdown-formatted" in prompt


def test_build_chat_prompt_uses_source_caption_as_primary_story() -> None:
    # A clickbait-farm caption (junk hook + real backstory) must not be discarded
    # wholesale. The prompt elevates the caption to primary context, tells the model
    # to drop the teaser but keep the substantive story, and treats an off-camera
    # backstory as valid grounding. This reconciles "story beneath the clip" accounts
    # (Beneath History) with the HistoryTrails "anchor to what's visible" rules.
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.source_description = (
            "They never told you about this secret. Michael Jackson and Chris Tucker "
            "were close friends, and Tucker appeared in the You Rock My World short film."
        )
        session.commit()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "How to use the source caption" in prompt
    assert "PRIMARY CONTEXT" in prompt
    assert "SEPARATE THE HOOK FROM THE STORY" in prompt
    assert "THE CLIP IS THE VISUAL, THE CAPTION IS THE STORY" in prompt
    assert "never discard the whole caption" in prompt
    # The fact-check pass now keeps substantive backstory instead of all-or-nothing.
    assert "keep any substantive, checkable backstory" in prompt


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
    # The app applies title+caption as one unit. With a single shared caption
    # there is no cross-pairing to forbid in the prompt, but the agent handoff
    # (which still writes its own JSON) must keep the equal-index contract.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "MUST be equal to each other" in prompt
    assert "never a file path" in prompt


def test_shared_caption_recommendation_follows_the_title_pick() -> None:
    # The pick line names only a title now, but Apply needs both indexes, so
    # the caption index has to track the title's instead of staying unset.
    parsed = draft_handoff.parse_pasted_draft(
        "\n".join(
            [
                "Title Option 1:",
                "First hook",
                "Title Option 2:",
                "Second hook",
                "Title Option 3:",
                "Third hook",
                "Recommended Pick: Title Option 2",
                "Why:",
                "Second lands the specific hardest.",
                "Shared Caption:",
                "The story under the clip, told once for every title.",
            ]
        )
    )

    assert parsed.recommended_title_index == 2
    assert parsed.recommended_caption_index == 2
    assert parsed.title_options == ["First hook", "Second hook", "Third hook"]
    # Fanned out so title_options[i] still pairs with caption_options[i].
    assert parsed.caption_options == ["The story under the clip, told once for every title."] * 3


def test_shared_caption_does_not_override_per_option_captions() -> None:
    # Older replies that still emit one caption per option must parse exactly
    # as before; the shared caption only fills genuine gaps.
    parsed = draft_handoff.parse_pasted_draft(
        "\n".join(
            [
                "Title Option 1:",
                "First hook",
                "Caption Option 1:",
                "Caption written for the first hook.",
                "Title Option 2:",
                "Second hook",
                "Shared Caption:",
                "Fallback caption.",
            ]
        )
    )

    assert parsed.caption_options == ["Caption written for the first hook.", "Fallback caption."]


def test_build_chat_prompt_includes_stored_vision_payload() -> None:
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.smart_vision_payload = '{"on_screen_hook": "man rides scooter with tent"}'
        session.commit()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "man rides scooter with tent" in prompt
    assert "Visual evidence JSON" in prompt


def test_build_account_batch_chat_prompt_has_shared_rules_and_item_delimiters() -> None:
    account_id, item_ids = _make_account_items(2)
    with get_session() as session:
        seqs = library.account_sequence_map(session)

    prompt = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    for index, item_id in enumerate(item_ids, start=1):
        # Header echoes the reel number AND the account "#id" import routes by.
        assert f"===== REEL {index} (#{seqs[item_id]}) =====" in prompt
        assert f"reel_{index}_item{item_id}.jpg" in prompt
    assert prompt.count("On-screen title rules") == 1
    assert prompt.count("Hook framing") == 1
    assert "Automatic NicheFlow handoff" not in prompt


def test_build_account_batch_chat_prompt_uses_measured_winners_and_title_length() -> None:
    account_id, item_ids = _make_account_items(2)
    with get_session() as session:
        account = session.get(Account, account_id)
        account.instagram_handle = "pastmomentsdaily"
        session.add(
            AccountPostMetric(
                account_key="pastmomentsdaily",
                shortcode="batch-winner",
                caption="Measured batch winner",
                conversion_score=0.9,
                pulled_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        session.commit()

    prompt = draft_handoff.build_account_batch_chat_prompt(
        account_id,
        item_ids,
        {"title_length": "auto"},
    )

    assert "MEASURED ACCOUNT WINNER EXAMPLES" in prompt
    assert "Measured batch winner" in prompt
    assert "Title length: Auto" in prompt
    # The band list must reach the chat prompt; which option carries which band
    # is deliberately left to the register assignment (see
    # test_title_length_auto_does_not_pin_a_band_to_an_option_number).
    assert "each length band exactly once" in prompt
    assert "5-9 words" in prompt


def test_build_chat_prompt_requires_niche_fit_check() -> None:
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "Niche fit check" in prompt
    assert "account niche ('history')" in prompt
    assert "Niche check: OFF-NICHE, <one short reason>" in prompt


def test_build_account_batch_chat_prompt_requires_niche_fit_check() -> None:
    account_id, item_ids = _make_account_items(2)

    prompt = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    assert "Niche fit check (per reel, MANDATORY)" in prompt
    assert "account niche ('history')" in prompt
    assert "Niche check: OFF-NICHE, <one short reason>" in prompt


def test_import_account_batch_draft_ignores_niche_check_lines() -> None:
    # The prompt asks the model to prepend "Niche check: ..." to every block;
    # the importer must treat it as preamble, never as title/caption content.
    _account_id, item_ids = _make_account_items(2)
    first, second = item_ids

    result = draft_handoff.import_account_batch_draft(
        """===== REEL 1 =====
Niche check: fits
Title Option 1: First title
Caption Option 1: First caption

===== REEL 2 =====
Niche check: OFF-NICHE, soccer manufacturing is not history
Title Option 1: Second title
Caption Option 1: Second caption
""",
        item_ids,
    )

    assert result == {"imported": [first, second], "failed": [], "unmatched": []}
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    second_revision = draft_revisions.latest_revision(second)
    assert second_revision.title_options == ["Second title"]
    assert second_revision.caption_options == ["Second caption"]


def test_import_account_batch_draft_routes_by_reel_number_when_shuffled() -> None:
    _account_id, item_ids = _make_account_items(2)
    first, second = item_ids

    # Blocks pasted out of order; routing is by reel number, not text order.
    result = draft_handoff.import_account_batch_draft(
        """===== REEL 2 =====
Title Option 1: Second title
Caption Option 1: Second caption

===== REEL 1 =====
Title Option 1: First title
Caption Option 1: First caption
""",
        item_ids,
    )

    assert result == {"imported": [first, second], "failed": [], "unmatched": []}
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]


def test_import_account_batch_draft_tolerates_mangled_reel_headers() -> None:
    # The real failure: ChatGPT drops the "=====", bolds or colon-suffixes the
    # header, and omits the opaque item id. Routing by reel number must still work.
    _account_id, item_ids = _make_account_items(2)
    first, second = item_ids

    result = draft_handoff.import_account_batch_draft(
        """**Reel 1**
Title Option 1: First title
Caption Option 1: First caption

Reel 2:
Title Option 1: Second title
Caption Option 1: Second caption
""",
        item_ids,
    )

    assert result["imported"] == [first, second]
    assert result["unmatched"] == []
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]


def test_import_account_batch_draft_reports_failed_and_unmatched() -> None:
    _account_id, item_ids = _make_account_items(3)
    first, second, third = item_ids

    result = draft_handoff.import_account_batch_draft(
        f"""===== REEL 1 | item {first} | First =====
Title Option 1: First title
Caption Option 1: First caption

===== REEL 2 | item {second} | Second =====
This block has no importable sections.

===== REEL 99 | item 999999 | Unknown =====
Title Option 1: Unknown title
Caption Option 1: Unknown caption
""",
        item_ids,
    )

    assert result["imported"] == [first]
    assert result["failed"][0]["item_id"] == second
    assert "No 'Title Option'" in result["failed"][0]["error"]
    assert result["unmatched"] == [third]
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second) is None


def test_import_account_batch_draft_finds_header_glued_to_preamble() -> None:
    # Real reply shape: the model glued its intro sentence to the first
    # delimiter with no newline ("...verifiable claims.===== REEL 1 (#145)
    # ====="). A line-start-only header match misses it and the first reel
    # silently comes back as "unmatched" despite a correctly formatted block.
    _account_id, item_ids = _make_account_items(2)
    first, second = item_ids

    result = draft_handoff.import_account_batch_draft(
        """I'll fact-check each reel before drafting.===== REEL 1 =====
Title Option 1: First title
Caption Option 1: First caption

===== REEL 2 =====
Title Option 1: Second title
Caption Option 1: Second caption
""",
        item_ids,
    )

    assert result == {"imported": [first, second], "failed": [], "unmatched": []}
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]


def test_import_account_batch_draft_ignores_preamble_notes_addressed_to_reels() -> None:
    # Real 2026-07-31 failure (items 1100/1101): the model wrote fact-check notes
    # ABOVE the first delimiter and addressed each reel by name ("Reel 1 (#88):
    # verified..."), which _REEL_HEADER accepts as a delimiter. Those note blocks
    # sort first, and under first-block-wins they shadowed the real drafts, so
    # both items failed with "No 'Title Option'..." and nothing came back
    # unmatched. A block with no draft sections must never win.
    _account_id, item_ids = _make_account_items(2)
    first, second = item_ids

    result = draft_handoff.import_account_batch_draft(
        """A couple of fact-check notes before the drafts:

Reel 1 (#88): verified the Heath Ledger story checks out as written.

Reel 2 (#87): the source's claim isn't what the actual clip shows.

===== REEL 1 =====
Title Option 1: First title
Caption Option 1: First caption

===== REEL 2 =====
Title Option 1: Second title
Caption Option 1: Second caption
""",
        item_ids,
    )

    assert result == {"imported": [first, second], "failed": [], "unmatched": []}
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]


def test_import_account_batch_draft_rejects_the_prepared_prompt() -> None:
    # The real failure: Import reads the clipboard, and the clipboard still held
    # the PROMPT that "Prepare batch" copied (the user never copied the reply).
    # The prompt's reel headers route every item to a block with no
    # Title/Caption sections, so without the guard each item fails with the
    # generic "No 'Title Option'..." error and no hint at the actual mistake.
    account_id, item_ids = _make_account_items(2)

    prompt = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids, {})

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.import_account_batch_draft(prompt, item_ids)

    message = str(excinfo.value)
    assert "PROMPT" in message
    assert "reply" in message


def test_import_pasted_draft_rejects_the_prepared_prompt() -> None:
    # Same clipboard mistake on the single-item "Paste Draft" path.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.import_pasted_draft(item_id, prompt)

    assert "PROMPT" in str(excinfo.value)


def _set_vision_payload(item_id: int, payload: str) -> None:
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.smart_vision_payload = payload
        session.commit()


def test_batch_prompt_drops_the_attachment_for_a_vision_backed_reel() -> None:
    # Visual evidence JSON replaces the per-reel still: it describes the whole
    # clip for a fraction of an attached image's tokens.
    account_id, item_ids = _make_account_items(1)
    (only,) = item_ids
    _set_vision_payload(only, '{"main_subject": "a stonemason", "ocr_text": ["1911"]}')

    prompt = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    assert "Attach and inspect image file:" not in prompt
    assert "No images are attached" in prompt
    assert '"main_subject": "a stonemason"' in prompt


def test_batch_prompt_still_asks_for_a_frame_when_vision_is_missing() -> None:
    # Degrades to the old behavior when the vision pass could not run (no Groq
    # key, no video on disk), so the model is never left without visuals.
    account_id, item_ids = _make_account_items(2)
    first, second = item_ids
    _set_vision_payload(first, '{"main_subject": "a stonemason"}')

    prompt = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    assert f"Attach and inspect image file: reel_2_item{second}.jpg" in prompt
    assert f"reel_1_item{first}.jpg" not in prompt
    assert "Some reels below ask you to attach" in prompt


def test_batch_prompt_explains_the_visual_evidence_fields() -> None:
    # The JSON is now the primary visual grounding, so the prompt has to teach
    # the model what its fields mean or it falls back to the source caption.
    account_id, item_ids = _make_account_items(1)

    prompt = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    assert "How to use the Visual evidence JSON" in prompt
    assert "on_screen_hook" in prompt


def test_batch_frames_skips_stills_for_vision_backed_reels(monkeypatch) -> None:
    account_id, item_ids = _make_account_items(2)
    first, second = item_ids
    _set_vision_payload(first, '{"main_subject": "a stonemason"}')
    _set_vision_payload(second, "")  # no evidence -> still needs a frame

    captured: list[int] = []

    def fake_crop_preview_frame(item_id: int, at_seconds: float | None = None):
        captured.append(item_id)
        source = tmp_frame_path()
        return source

    def tmp_frame_path():
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "frame.jpg"
        path.write_bytes(b"jpeg")
        return path

    monkeypatch.setattr(
        draft_handoff.export_svc, "crop_preview_frame", fake_crop_preview_frame
    )

    result = draft_handoff.batch_frames(item_ids)

    assert captured == [second]  # the vision-backed reel never cut a frame
    assert [frame["item_id"] for frame in result["frames"]] == [second]
    assert result["described"] == [first]


def test_batch_frames_names_the_reel_when_a_video_file_is_missing() -> None:
    _account_id, item_ids = _make_account_items(1)
    (only,) = item_ids
    with get_session() as session:
        item = session.get(DownloadItem, only)
        item.file_path = None
        session.commit()

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.batch_frames(item_ids)

    assert f"Reel 1 (item {only})" in str(excinfo.value)
    assert "no downloaded video file" in str(excinfo.value)


def test_import_account_batch_draft_routes_by_echoed_id_over_reel_number() -> None:
    # The echoed "(#id)" is more specific than the reel number: when they
    # disagree (reel numbers crossed), the id wins so each block lands on the
    # video it was generated for.
    _account_id, item_ids = _make_account_items(2)
    first, second = item_ids
    with get_session() as session:
        seqs = library.account_sequence_map(session)

    result = draft_handoff.import_account_batch_draft(
        f"""===== REEL 2 (#{seqs[first]}) =====
Title Option 1: First title
Caption Option 1: First caption

===== REEL 1 (#{seqs[second]}) =====
Title Option 1: Second title
Caption Option 1: Second caption
""",
        item_ids,
    )

    assert set(result["imported"]) == {first, second}
    assert result["unmatched"] == []
    # Routed by #id despite the crossed reel numbers.
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]


def test_import_account_batch_draft_routes_by_id_when_reel_numbers_collide() -> None:
    # Models sometimes renumber every block ("REEL 1" three times) while still
    # echoing distinct, correct ids. Keying the splitter by reel number merged
    # all three into ONE block, whose later "Title Option 1:" headers redefined
    # the first block's options — the first item silently imported the LAST
    # block's draft and the other two came back unmatched. The echoed id must
    # route each block regardless of the reel numbering.
    _account_id, item_ids = _make_account_items(3)
    first, second, third = item_ids
    with get_session() as session:
        seqs = library.account_sequence_map(session)

    result = draft_handoff.import_account_batch_draft(
        f"""===== REEL 1 (#{seqs[first]}) =====
Title Option 1: First title
Caption Option 1: First caption

===== REEL 1 (#{seqs[second]}) =====
Title Option 1: Second title
Caption Option 1: Second caption

===== REEL 1 (#{seqs[third]}) =====
Title Option 1: Third title
Caption Option 1: Third caption
""",
        item_ids,
    )

    assert set(result["imported"]) == {first, second, third}
    assert result["unmatched"] == []
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]
    assert draft_revisions.latest_revision(third).title_options == ["Third title"]


def test_import_account_batch_draft_ignores_selection_order_when_ids_echoed() -> None:
    # Selection order must not matter: the same reply imports identically no
    # matter what order the UI hands the item ids over, because the echoed id
    # pins each block to its video.
    _account_id, item_ids = _make_account_items(3)
    first, second, third = item_ids
    with get_session() as session:
        seqs = library.account_sequence_map(session)

    reply = f"""===== REEL 1 (#{seqs[first]}) =====
Title Option 1: First title
Caption Option 1: First caption

===== REEL 2 (#{seqs[second]}) =====
Title Option 1: Second title
Caption Option 1: Second caption

===== REEL 3 (#{seqs[third]}) =====
Title Option 1: Third title
Caption Option 1: Third caption
"""

    # Hand the ids over reversed relative to the reel numbering.
    result = draft_handoff.import_account_batch_draft(reply, [third, second, first])

    assert set(result["imported"]) == {first, second, third}
    assert result["unmatched"] == []
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]
    assert draft_revisions.latest_revision(third).title_options == ["Third title"]


def test_import_account_batch_draft_ignores_unknown_id_and_falls_back() -> None:
    # An echoed id that is not part of this batch (e.g. the model parroted the
    # frame filename's item id) must be ignored, falling back to the reel number.
    _account_id, item_ids = _make_account_items(2)
    first, second = item_ids

    result = draft_handoff.import_account_batch_draft(
        """===== REEL 1 (#99999) =====
Title Option 1: First title
Caption Option 1: First caption

===== REEL 2 (#88888) =====
Title Option 1: Second title
Caption Option 1: Second caption
""",
        item_ids,
    )

    assert result["imported"] == [first, second]
    assert result["unmatched"] == []
    assert draft_revisions.latest_revision(first).title_options == ["First title"]
    assert draft_revisions.latest_revision(second).title_options == ["Second title"]


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


def test_parse_pasted_draft_tolerates_bold_markdown_headers() -> None:
    # Chat models keep bolding section headers ('**Title Option 1:**') despite
    # the plain-text instruction. The '^'-anchored header regexes used to reject
    # the leading '**', so the whole reply imported as zero sections. The parser
    # must now unwrap the bold header while leaving '**bold**' inside the title
    # value intact.
    parsed = draft_handoff.parse_pasted_draft(
        """**Title Option 1:** The **GOAT** play
**Caption Option 1:** First paragraph.

Second paragraph.

**Recommended Pick:** Title Option 1 + Caption Option 1
**Why:** Strongest option.
**Selection Notes:**
**Option 1:** Specific and clear.
"""
    )

    assert parsed.title_options == ["The **GOAT** play"]
    assert parsed.caption_options == ["First paragraph.\n\nSecond paragraph."]
    assert parsed.recommended_title_index == 1
    assert parsed.recommended_caption_index == 1
    assert parsed.option_notes == ["Specific and clear."]


def test_parse_pasted_draft_drops_trailing_citation_from_notes() -> None:
    # Chat models append a sources block ("[1]: https://...") after the last
    # block; it must not bleed into that reel's final selection note.
    parsed = draft_handoff.parse_pasted_draft(
        """Title Option 1:
A title

Caption Option 1:
A caption.

Recommended Pick: Title Option 1 + Caption Option 1
Why: Strongest.
Selection Notes:
Option 1: Best fit because it is specific.

[1]: https://en.wikipedia.org/wiki/Music_for_Montserrat?utm_source=chatgpt.com "Music for Montserrat"
https://example.com/bare-url
"""
    )

    assert parsed.option_notes == ["Best fit because it is specific."]


def test_parse_pasted_draft_dedupes_repeated_option_headers() -> None:
    # Models (and hand-edits) sometimes restate options: a leading summary list
    # of all three titles, then the same titles again interleaved with captions.
    # flush()'s extend() used to concatenate the repeated title into itself
    # ("Title... Title..."). A repeated header must redefine the slot, not append.
    parsed = draft_handoff.parse_pasted_draft(
        """Title Option 1:
Janet Jackson's 2009 VMA tribute to Michael
Title Option 2:
The night Janet Jackson paid tribute to Michael at the 2009 VMAs
Title Option 3:
Janet Jackson took the stage at the 2009 VMAs

Caption Option 1:
First caption.

Title Option 2:
The night Janet Jackson paid tribute to Michael at the 2009 VMAs
Caption Option 2:
Second caption.

Title Option 3:
Janet Jackson took the stage at the 2009 VMAs
Caption Option 3:
Third caption.

Recommended Pick: Title Option 3 + Caption Option 3
Why: Strongest anchor.
"""
    )

    assert parsed.title_options == [
        "Janet Jackson's 2009 VMA tribute to Michael",
        "The night Janet Jackson paid tribute to Michael at the 2009 VMAs",
        "Janet Jackson took the stage at the 2009 VMAs",
    ]
    assert parsed.caption_options == [
        "First caption.",
        "Second caption.",
        "Third caption.",
    ]


def test_parse_pasted_draft_keeps_titles_restated_as_empty_caption_lead_ins() -> None:
    # The real 2026-07-15 batch shape: the model lists all three titles up
    # front, then restates a BARE "Title Option N:" purely as a lead-in to
    # "Caption Option N:". The repeated-header dedup treated that empty
    # restatement as a redefinition and wiped titles 2-3, so every reel
    # imported with one option and the screen showed a single card.
    parsed = draft_handoff.parse_pasted_draft(
        """Title Option 1:
First title

Title Option 2:
Second title

Title Option 3:
Third title

Caption Option 1:
First caption.

Second paragraph.

Title Option 2:
Caption Option 2:
Second caption.

Title Option 3:
Caption Option 3:
Third caption.

Recommended Pick: Title Option 2 + Caption Option 2
"""
    )

    assert parsed.title_options == ["First title", "Second title", "Third title"]
    assert parsed.caption_options == [
        "First caption.\n\nSecond paragraph.",
        "Second caption.",
        "Third caption.",
    ]
    assert parsed.recommended_title_index == 2


def test_parse_pasted_draft_expands_same_caption_placeholder() -> None:
    # Chat replies collapse an identical caption to "[Same caption as Option 1]"
    # instead of repeating the full text. Left as-is that placeholder becomes the
    # stored caption for Options 2/3 and shows verbatim in the UI and exported
    # post. Each option must carry the real Option 1 caption instead.
    parsed = draft_handoff.parse_pasted_draft(
        """Title Option 1:
The King took private Arabic lessons?

Caption Option 1:
King Charles once admitted his Arabic lessons went in one ear and out the other.

As Prince of Wales, he took private Arabic tuition for several months.

Title Option 2:
King Charles once said his Arabic lessons went in one ear.
Caption Option 2:
[Same caption as Option 1]

Title Option 3:
When King Charles was still Prince of Wales, he admitted his struggle.
Caption Option 3:
[Same caption as Option 1]

Recommended Pick: Title Option 3 + Caption Option 3
Why: Strongest hook.
"""
    )

    expected_caption = (
        "King Charles once admitted his Arabic lessons went in one ear and out the other."
        "\n\nAs Prince of Wales, he took private Arabic tuition for several months."
    )
    assert parsed.caption_options == [expected_caption, expected_caption, expected_caption]


def test_parse_pasted_draft_leaves_broken_caption_reference_untouched() -> None:
    # An out-of-range placeholder has nothing real to expand to; keep the honest
    # original rather than silently emptying the caption (which would trip the
    # half-parsed guard downstream).
    parsed = draft_handoff.parse_pasted_draft(
        """Title Option 1:
First title
Caption Option 1:
[Same caption as Option 9]

Title Option 2:
Second title
Caption Option 2:
Second caption.
"""
    )

    assert parsed.caption_options == ["[Same caption as Option 9]", "Second caption."]


def test_import_pasted_draft_rejects_half_parsed_options() -> None:
    # When a caption parses but its paired title header does not (here: no
    # colon at all on the Title Option 2/3 lines), the import must fail loudly
    # naming the broken headers — silently saving a 1-title/3-caption revision
    # hides options in the UI and breaks Apply's shared index pairing.
    item_id = _make_item()

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.import_pasted_draft(
            item_id,
            """Title Option 1:
First title
Caption Option 1:
First caption.

Title Option 2
Second title
Caption Option 2:
Second caption.

Title Option 3
Third title
Caption Option 3:
Third caption.
""",
        )

    message = str(excinfo.value)
    assert "Title Option 2/3" in message
    assert "did not parse" in message
    assert draft_revisions.latest_revision(item_id) is None


def test_import_pasted_draft_saves_revision() -> None:
    item_id = _make_item()

    saved = draft_handoff.import_pasted_draft(
        item_id,
        "Title Option 1: A title\nCaption Option 1: A caption",
    )

    assert saved.source == "clipboard"
    assert draft_revisions.latest_revision(item_id).title_options == ["A title"]


def test_build_chat_prompt_asks_cli_agents_for_option_tiers() -> None:
    # Codex/Claude Code can inspect the actual video, so their self-rated
    # tiers are genuinely informed — the CLI contract must request them.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id)

    assert "option_tiers" in prompt
    assert "'green'/'yellow'/'red'" in prompt


def test_import_pasted_draft_grades_titles_and_moves_flagged_pick() -> None:
    # ChatGPT pastes arrive untiered; the deterministic guard grades them from
    # the stored clip signals and never lets an unsupported claim ("heavy"
    # with no weight evidence anywhere) stay the recommended pick.
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.source_description = "Vintage prams on the streets of London, 1950s."
        session.commit()

    saved = draft_handoff.import_pasted_draft(
        item_id,
        """Title Option 1:
Would you push one of these today?
Caption Option 1:
Cap one.

Title Option 2:
These prams were insanely heavy
Caption Option 2:
Cap two.

Recommended Pick: Title Option 2 + Caption Option 2
Why: Boldest hook.
Selection Notes:
Option 1: Safe curiosity angle.
Option 2: Boldest claim.
""",
    )

    assert saved.option_tiers == ["green", "red"]
    assert saved.recommended_title_index == 1
    assert saved.recommended_caption_index == 1
    assert "Auto-moved from Option 2" in saved.recommendation_reason
    assert "'heavy'" in saved.option_notes[1]


def test_import_pasted_draft_keeps_supported_claims_yellow() -> None:
    # A claim the source caption actually backs ("1950s") grades yellow and
    # the recommendation stays where the chat model put it.
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.source_description = "Vintage prams on the streets of London, 1950s."
        session.commit()

    saved = draft_handoff.import_pasted_draft(
        item_id,
        """Title Option 1:
Pram fashion in the 1950s
Caption Option 1:
Cap one.

Recommended Pick: Title Option 1 + Caption Option 1
Why: Dated and grounded.
""",
    )

    assert saved.option_tiers == ["yellow"]
    assert saved.recommended_title_index == 1
    assert saved.recommendation_reason == "Dated and grounded."


def test_caption_mode_defaults_to_shared_in_both_prompts() -> None:
    # No caption_mode in settings (the shape every existing caller sends) must
    # keep the cheaper shared-caption contract rather than silently reverting.
    item_id = _make_item()
    account_id, item_ids = _make_account_items(2)

    single = draft_handoff.build_chat_prompt(item_id, {})
    batch = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids, {})

    for prompt in (single, batch):
        assert "Shared Caption:" in prompt
        assert "Caption Option 1:" not in prompt
        assert "Recommended Pick: Title Option N" in prompt


def test_caption_mode_per_option_restores_three_caption_contract() -> None:
    item_id = _make_item()
    account_id, item_ids = _make_account_items(2)
    settings = {"caption_mode": "per_option"}

    single = draft_handoff.build_chat_prompt(item_id, settings)
    batch = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids, settings)

    for prompt in (single, batch):
        assert "Shared Caption:" not in prompt
        for number in (1, 2, 3):
            assert f"Caption Option {number}:" in prompt
        assert "Recommended Pick: Title Option N + Caption Option N" in prompt
        # The pairing rule only matters when captions differ per option.
        assert "MUST share the same option number" in prompt


def test_unknown_caption_mode_falls_back_to_shared() -> None:
    # A stale or hand-edited preference must not produce a prompt with neither
    # caption contract in it.
    item_id = _make_item()

    prompt = draft_handoff.build_chat_prompt(item_id, {"caption_mode": "nonsense"})

    assert "Shared Caption:" in prompt
    assert "Caption Option 1:" not in prompt


def test_parser_accepts_both_shapes_regardless_of_caption_mode() -> None:
    # caption_mode steers the PROMPT only. A reply in either shape must import,
    # so switching the setting never strands a reply already in flight.
    shared = "\n".join(
        [
            "Title Option 1:",
            "First title",
            "Title Option 2:",
            "Second title",
            "Recommended Pick: Title Option 2",
            "Shared Caption:",
            "One caption for both titles.",
        ]
    )
    per_option = "\n".join(
        [
            "Title Option 1:",
            "First title",
            "Caption Option 1:",
            "First caption.",
            "Title Option 2:",
            "Second title",
            "Caption Option 2:",
            "Second caption.",
            "Recommended Pick: Title Option 2 + Caption Option 2",
        ]
    )

    shared_draft = draft_handoff.parse_pasted_draft(shared)
    assert shared_draft.caption_options == [
        "One caption for both titles.",
        "One caption for both titles.",
    ]
    assert shared_draft.recommended_caption_index == 2

    per_option_draft = draft_handoff.parse_pasted_draft(per_option)
    assert per_option_draft.caption_options == ["First caption.", "Second caption."]
    assert per_option_draft.recommended_caption_index == 2


def _make_second_account_items(name: str, count: int = 2) -> tuple[int, list[int]]:
    with get_session() as session:
        account = Account(
            name=name,
            platform="instagram",
            niche="history",
            niche_label="history",
            title_style_notes="Use specific subject-first hooks.",
        )
        session.add(account)
        session.flush()
        ids: list[int] = []
        for index in range(1, count + 1):
            item = DownloadItem(
                source_url=f"https://instagram.com/reel/{name}{index}",
                title=f"{name} source {index}",
                source_description=f"{name} description {index}",
                file_path=f"C:/clips/{name}{index}.mp4",
                account_id=account.id,
                status="downloaded",
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
        session.commit()
        return account.id, ids


def test_multi_account_prompt_sends_shared_rules_once() -> None:
    # The whole point of batching accounts together: the ~14k-character style
    # rule block is emitted once instead of once per account.
    _a1, first = _make_second_account_items("AlphaHistory")
    _a2, second = _make_second_account_items("BetaHistory")

    prompt = draft_handoff.build_multi_account_batch_chat_prompt([*first, *second])

    assert prompt.count("Shared style rules") == 1
    assert prompt.count("On-screen title rules") == 1
    assert prompt.count("########## ACCOUNT:") == 2


def test_multi_account_prompt_groups_interleaved_items_by_account() -> None:
    # Reel numbers and frame filenames are positional, so the prompt must walk
    # the batch in the same grouped order the frame extractor uses.
    _a1, first = _make_second_account_items("GammaHistory")
    _a2, second = _make_second_account_items("DeltaHistory")
    interleaved = [first[0], second[0], first[1], second[1]]

    assert draft_handoff.multi_account_batch_order(interleaved) == [*first, *second]

    prompt = draft_handoff.build_multi_account_batch_chat_prompt(interleaved)
    headers = re.findall(r"===== REEL (\d+) \(#(\d+)\) =====", prompt)

    assert [int(item_id) for _reel, item_id in headers] == [*first, *second]
    assert [int(reel) for reel, _item_id in headers] == [1, 2, 3, 4]


def test_multi_account_prompt_echoes_global_item_ids() -> None:
    # Account-relative "#id"s repeat across accounts, so cross-account headers
    # must carry the global download-item id or blocks misroute.
    _a1, first = _make_second_account_items("EpsilonHistory", 1)
    _a2, second = _make_second_account_items("ZetaHistory", 1)

    prompt = draft_handoff.build_multi_account_batch_chat_prompt([*first, *second])

    assert f"(#{first[0]})" in prompt
    assert f"(#{second[0]})" in prompt


def test_import_multi_account_batch_routes_across_accounts() -> None:
    _a1, first = _make_second_account_items("EtaHistory", 1)
    _a2, second = _make_second_account_items("ThetaHistory", 1)

    reply = "\n".join(
        [
            f"===== REEL 1 (#{first[0]}) =====",
            "Title Option 1:",
            "Alpha hook",
            "Recommended Pick: Title Option 1",
            "Shared Caption:",
            "Alpha caption body.",
            f"===== REEL 2 (#{second[0]}) =====",
            "Title Option 1:",
            "Beta hook",
            "Recommended Pick: Title Option 1",
            "Shared Caption:",
            "Beta caption body.",
        ]
    )

    result = draft_handoff.import_multi_account_batch_draft(reply, [*first, *second])

    assert result["imported"] == [*first, *second]
    assert result["failed"] == []
    assert result["unmatched"] == []
    assert draft_revisions.latest_revision(first[0]).title_options == ["Alpha hook"]
    assert draft_revisions.latest_revision(second[0]).title_options == ["Beta hook"]


def test_batch_candidates_skips_drafted_and_resting_accounts() -> None:
    account_id, item_ids = _make_second_account_items("IotaHistory", 3)
    draft_handoff.import_pasted_draft(
        item_ids[0],
        "Title Option 1:\nAlready drafted\nShared Caption:\nA caption.",
    )

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)
    assert [item["id"] for item in mine["items"]] == item_ids[1:]

    with get_session() as session:
        session.get(Account, account_id).operational_status = "resting"
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    assert all(g["account_id"] != account_id for g in groups)


def _add_upload_job(account_id: int, item_id: int, status: str, posted: bool) -> int:
    with get_session() as session:
        job = UploadJob(
            account_id=account_id,
            download_item_id=item_id,
            processed_path=f"C:/clips/out{item_id}.mp4",
            status=status,
            posted_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc) if posted else None,
        )
        session.add(job)
        session.commit()
        return job.id


def test_batch_candidates_skips_reels_that_already_posted() -> None:
    # Publishing records the outcome on UploadJob and leaves DownloadItem.status
    # alone on the legacy direct-download path, so filtering on the item's own
    # status let posted reels back in as "draftless" — the batch screen offered
    # clips the Processing table was already showing as Posted.
    account_id, item_ids = _make_second_account_items("RhoHistory", 3)
    _add_upload_job(account_id, item_ids[0], "posted", posted=True)
    # A job flagged posted without a posted_at counts too: that is the shape the
    # older local publish path left behind.
    _add_upload_job(account_id, item_ids[1], "posted", posted=False)

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert [item["id"] for item in mine["items"]] == [item_ids[2]]
    assert mine["available"] == 1


def test_batch_candidates_keep_a_posted_reel_that_was_reopened() -> None:
    # Reopening a posted reel creates a NEWER unposted job, and Processing shows
    # it as drafting again. The batch screen must agree, or a deliberate repost
    # becomes impossible to draft for.
    account_id, item_ids = _make_second_account_items("SigmaHistory", 1)
    _add_upload_job(account_id, item_ids[0], "posted", posted=True)
    _add_upload_job(account_id, item_ids[0], "draft", posted=False)

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert [item["id"] for item in mine["items"]] == item_ids


def test_batch_candidates_skips_a_reel_another_account_already_posted() -> None:
    # The same source reel can reach two accounts: clips from the legacy
    # direct-download path were never pool items, so the pool's dedup never knew
    # another account held them. Publishing it twice from one network is a
    # footprint risk, so match on the source video, not just this account's copy.
    poster_id, posted_ids = _make_second_account_items("TauHistory", 1)
    _add_upload_job(poster_id, posted_ids[0], "posted", posted=True)
    other_id, other_ids = _make_second_account_items("UpsilonHistory", 2)
    with get_session() as session:
        # Same footage, different DownloadItem row on the other account.
        shared = session.get(DownloadItem, posted_ids[0]).video_id or "shared-reel"
        session.get(DownloadItem, posted_ids[0]).video_id = shared
        session.get(DownloadItem, other_ids[0]).video_id = shared
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    other = next(g for g in groups if g["account_id"] == other_id)

    assert [item["id"] for item in other["items"]] == [other_ids[1]]
    assert other["available"] == 1


def test_batch_candidates_keep_reels_with_no_video_id() -> None:
    # A null video_id must not collide with every other null and wipe the list.
    account_id, item_ids = _make_second_account_items("PhiHistory", 2)
    poster_id, posted_ids = _make_second_account_items("ChiHistory", 1)
    _add_upload_job(poster_id, posted_ids[0], "posted", posted=True)
    with get_session() as session:
        session.get(DownloadItem, posted_ids[0]).video_id = None
        for item_id in item_ids:
            session.get(DownloadItem, item_id).video_id = None
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert [item["id"] for item in mine["items"]] == item_ids


def test_batch_candidates_skips_globally_blocked_items() -> None:
    # A globally-rejected reel is hidden from the Processing table
    # (library.list_items filters review_state == "blocked"). Offering it here
    # would resurrect a clip the user killed everywhere else, and made accounts
    # whose only "candidates" were blocked look like they had a backlog.
    account_id, item_ids = _make_second_account_items("OmicronHistory", 3)
    with get_session() as session:
        session.get(DownloadItem, item_ids[0]).review_state = "blocked"
        session.get(DownloadItem, item_ids[1]).review_state = "rejected"
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert [item["id"] for item in mine["items"]] == [item_ids[2]]
    # "available" drives the "N of M draftless" line, so it must drop too.
    assert mine["available"] == 1


def test_batch_candidates_number_items_the_way_processing_does() -> None:
    # The batch list, the Processing table, the generated prompt's
    # "REEL n (#N)" headers, and the paste router must all name a reel by the
    # same number. Raw DownloadItem.id here meant the batch screen alone showed
    # a different one.
    account_id, item_ids = _make_second_account_items("PiHistory", 2)

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    with get_session() as session:
        expected = library.account_sequence_map(session)
    assert [item["account_seq"] for item in mine["items"]] == [
        expected[item_id] for item_id in item_ids
    ]

    # The number shown routes the pasted reply back to the same item.
    prompt = draft_handoff.build_multi_account_batch_chat_prompt(item_ids)
    for item in mine["items"]:
        assert f"(#{item['account_seq']})" in prompt


def test_batch_candidates_reports_the_full_backlog_beyond_the_limit() -> None:
    # A short list must read as a small backlog, not a bug: the UI shows
    # "2 of 5 draftless" so an account low on undrafted clips is obvious.
    account_id, item_ids = _make_second_account_items("KappaHistory", 5)

    groups = draft_handoff.batch_candidates(niche="history", per_account=2)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert len(mine["items"]) == 2
    assert mine["available"] == len(item_ids)


def test_batch_candidates_report_reels_whose_footage_is_still_downloading() -> None:
    # Distribute creates the pending-review row immediately but its file_path
    # only lands when the shared asset finishes downloading. Dropping those rows
    # silently made a 6-clip distribute read "5 of 5 draftless" until the
    # background download caught up, which looked like distribution shorting the
    # account. They stay out of the offer list but must be counted and shown.
    account_id, item_ids = _make_second_account_items("PsiHistory", 3)
    with get_session() as session:
        session.get(DownloadItem, item_ids[0]).file_path = None
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert [item["id"] for item in mine["items"]] == item_ids[1:]
    assert mine["available"] == 2
    assert mine["pending_media"] == 1


def test_batch_candidates_never_count_retired_reels_as_still_downloading() -> None:
    # Retiring a clip (per-account reject or the global block) leaves the row
    # with no media link, which is the same shape as "still downloading". The
    # new counter must not resurrect those: a killed reel has to stay gone from
    # every surface, including the "still downloading" hint.
    account_id, item_ids = _make_second_account_items("OmegaHistory", 3)
    with get_session() as session:
        for item_id, state in zip(item_ids[:2], ["rejected", "blocked"]):
            item = session.get(DownloadItem, item_id)
            item.review_state = state
            item.file_path = None
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert [item["id"] for item in mine["items"]] == [item_ids[2]]
    assert mine["available"] == 1
    assert mine["pending_media"] == 0


def test_batch_candidates_expose_a_preview_path_and_source_url() -> None:
    # The review player needs both: the bridge maps file_path to a media URL.
    account_id, item_ids = _make_second_account_items("LambdaHistory", 1)

    groups = draft_handoff.batch_candidates(niche="history", per_account=5)
    mine = next(g for g in groups if g["account_id"] == account_id)
    (item,) = mine["items"]

    assert item["file_path"] == "C:/clips/LambdaHistory1.mp4"
    assert item["source_url"] == "https://instagram.com/reel/LambdaHistory1"
    assert item["id"] == item_ids[0]


def test_batch_headers_defer_the_recommendation_to_the_style_rules() -> None:
    # Regression: the batch header carried its own global "recommend the one
    # whose specific the clip delivers most strongly" line that competed with the
    # style-specific RECOMMENDED PICK rule. After the HistoryTrails rules were
    # retuned so the reactive register wins by default, a real batch still picked
    # the documentary option in 6 of 6 reels and justified each as "the strongest
    # concrete signal", which is the header's wording and not the style rule's.
    account_id, item_ids = _make_account_items(2)

    prompt = draft_handoff.build_account_batch_chat_prompt(
        account_id,
        item_ids,
        {"title_style": "historytrails_record", "caption_style": "history_context"},
    )

    assert "whose specific the clip delivers most strongly" not in prompt
    assert "RECOMMENDED PICK rule in the on-screen title rules above" in prompt
    # The style rule must still be the one voice that IS present.
    assert "a REACTIVE option is the default pick" in prompt


def test_batch_headers_ban_repeating_a_title_stem_across_reels() -> None:
    # Regression: one prompt draws one example set and every reel is written
    # against it, so a real six-reel batch came back with four titles opening
    # "Would you ..." / "Would this ...". Within-reel variety rules cannot see
    # across reels; the batch header has to say it.
    account_id, item_ids = _make_account_items(3)

    prompt = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids, {})

    assert "Cross-reel variety (HARD RULE)" in prompt
    assert "Never open two reels' titles with the same first three words" in prompt
    assert "Would you ..." in prompt


def test_multi_account_batch_header_carries_the_same_variety_rules() -> None:
    _account_id, item_ids = _make_account_items(2)

    prompt = draft_handoff.build_multi_account_batch_chat_prompt(item_ids, {})

    assert "Cross-reel variety (HARD RULE)" in prompt
    assert "whose specific the clip delivers most strongly" not in prompt


def test_batch_header_requires_the_recommended_register_to_vary() -> None:
    # Regression: a six-reel batch recommended Option 2, the question, in all six
    # reels. Within-reel rules cannot see that pattern; only the batch header can.
    _account_id, item_ids = _make_account_items(2)

    prompt = draft_handoff.build_account_batch_chat_prompt(_account_id, item_ids, {})

    assert "Vary the RECOMMENDED register across reels" in prompt
    assert "no single register may take more than about half" in prompt
    assert "the option number you recommend must change between" in prompt
