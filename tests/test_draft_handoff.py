import datetime as dt

from nicheflow_studio.db.models import AccountPostMetric, Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import smart_drafts
from nicheflow_studio.services import draft_handoff, draft_revisions, library


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
        assert "Never state a guess you could not verify" in prompt
        assert "safest or most hedged" in prompt
    assert "work ONLY from the signals" not in single


def test_chat_prompts_disambiguate_title_vs_caption_and_fact_check() -> None:
    # Regression: the historytrails title rules call the on-screen title a
    # "documentary caption", so chat models duplicated the title into the
    # Caption Option slot and dumped the real caption under an unparsed
    # "Caption Option N (full):" header. Both chat prompts must now spell the
    # two fields apart, forbid the "(full)" field, and carry the fact-check pass.
    item_id = _make_item()
    account_id, item_ids = _make_account_items(2)

    single = draft_handoff.build_chat_prompt(item_id)
    batch = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)

    for prompt in (single, batch):
        assert "TWO DIFFERENT texts" in prompt
        assert "Caption Option N (full):" in prompt  # named so it's explicitly banned
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
    assert "Option 1 SHORT" in prompt


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
