import json
import re
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from nicheflow_studio.db.models import (
    AccountPostMetric,
    Account,
    DownloadItem,
    MediaAsset,
    UploadJob,
    ScrapeCandidate,
)
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import smart_drafts
from nicheflow_studio.processing import draft_guard
from nicheflow_studio.services import draft_handoff, draft_revisions, library

NL = chr(10)
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


def _strip_signals(item_id: int) -> None:
    """Leave an item with nothing a drafter could write from."""
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.title = None
        item.source_description = None
        item.transcript_text = None
        item.smart_vision_payload = None
        session.commit()


def test_build_chat_prompt_refuses_a_reel_with_no_signals(hook_fixture) -> None:
    """A prompt whose every context field reads "(none)" is not worth copying.

    Item 2371 went out that way: Apify returned a metadata-less row, nothing
    downstream filled it, and a chat assistant cannot open the local path, so
    the only honest reply it could give was to ask what was in the clip. The
    round trip through ChatGPT is a slow way to learn that, so fail at the
    button instead.
    """
    _account_id, item_ids = hook_fixture.account("BlankHistory", 1)
    _strip_signals(item_ids[0])

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.build_chat_prompt(item_ids[0])

    assert "Prepare batch" in str(excinfo.value)


def test_a_clip_premise_alone_unblocks_a_signal_less_reel(hook_fixture) -> None:
    # The premise IS the one-line description the guard asks for, so typing one
    # has to unblock the same reel rather than dead-ending it.
    _account_id, item_ids = hook_fixture.account("PremiseHistory", 1)
    _strip_signals(item_ids[0])

    prompt = draft_handoff.build_chat_prompt(
        item_ids[0], {"clip_premise": "Pastor loses his place mid-sermon."}
    )

    assert "Pastor loses his place mid-sermon." in prompt


def test_build_chat_prompt_reads_a_missing_on_screen_hook_first(monkeypatch, hook_fixture) -> None:
    """The single-reel path has to fill the hook itself.

    ``batch_frames`` does it for the batch path, which left the one-click path
    as the only prompt that could go out with no visual grounding at all. The
    read is local OCR, so it costs nothing but a second.
    """
    account_id, item_ids = hook_fixture.account("OcrHistory", 1)
    hook_fixture.link(item_ids[0], "vid-ocr-1", account_id)
    _strip_signals(item_ids[0])
    monkeypatch.setattr(
        draft_handoff.on_screen_hook,
        "read_hook",
        lambda path: ("At least he is honest", 0.96),
    )
    monkeypatch.setattr(Path, "exists", lambda self: True)

    prompt = draft_handoff.build_chat_prompt(item_ids[0])

    assert "At least he is honest" in prompt


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
        # Same intent, v2's wording: research must not push the model toward
        # recommending whichever option commits to the least.
        assert "hedging is not a virtue" in prompt
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
                    reach=50_000,
                    caption="Measured winner one",
                    conversion_score=0.8,
                    pulled_at=dt.datetime.now(dt.timezone.utc),
                ),
                AccountPostMetric(
                    account_key="pastmomentsdaily",
                    shortcode="winner-two",
                    reach=50_000,
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
                reach=50_000,
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

    assert result == {
        "imported": [first, second],
        "failed": [],
        "skipped": [],
        "unmatched": [],
    }
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

    assert result == {
        "imported": [first, second],
        "failed": [],
        "skipped": [],
        "unmatched": [],
    }
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

    assert result == {
        "imported": [first, second],
        "failed": [],
        "skipped": [],
        "unmatched": [],
    }
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

    assert result == {
        "imported": [first, second],
        "failed": [],
        "skipped": [],
        "unmatched": [],
    }
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


def test_every_prompt_path_uses_the_accounts_saved_title_style(hook_fixture) -> None:
    """One account must not get two different rulebooks.

    Only the multi-account builder resolved the account's saved
    processing_preferences. The other two read `settings` alone, so an empty
    dict gave title_style=None, which falls past the beats_v2 branch to the v1
    rules - the Processing screen's single-clip prompt and the single-account
    batch were handing out Curiosity / Open Loop for an account saved as v2,
    while the batch screen handed out v2. Same account, same clip, two
    different sets of instructions.
    """
    account_id, item_ids = hook_fixture.account("RhoHistory", 2)
    with get_session() as session:
        session.get(Account, account_id).processing_preferences = json.dumps(
            {"prompt_title_style": "beats_v2"}
        )
        session.commit()

    prompts = {
        "multi": draft_handoff.build_multi_account_batch_chat_prompt(item_ids, {}),
        "account_batch": draft_handoff.build_account_batch_chat_prompt(account_id, item_ids),
        "single_clip": draft_handoff.build_chat_prompt(item_ids[0]),
    }
    for name, prompt in prompts.items():
        # v2 markers
        assert "DO NOT CLOSE THE LOOP" in prompt, name
        assert "130 CHARACTERS" in prompt, name
        # and NOT the v1 rulebook it used to fall through to
        assert "SIX MOVES" not in prompt, name


@pytest.fixture
def hook_fixture():
    """Make a history account with items, optionally linked to a ScrapeCandidate.

    Cleans up everything it creates, which existing callers of
    ``_make_second_account_items`` do not. Two couplings make that necessary:
    the factory hardcodes ``niche="history"`` and test_assignments asserts an
    exact distribution across EVERY history account that exists, and
    distribution also queries ScrapeCandidate globally. So a leftover account or
    candidate row from a test in this file fails a distribution test several
    files later, which is exactly what adding these did.
    """
    accounts: list[int] = []

    class _Fixture:
        def account(self, name: str, count: int = 1):
            account_id, item_ids = _make_second_account_items(name, count)
            accounts.append(account_id)
            return account_id, item_ids

        def link(self, item_id: int, video_id: str, account_id: int) -> None:
            """on_screen_title lives per SOURCE VIDEO, not per item, so one
            reading is shared by every account the reel reached."""
            with get_session() as session:
                session.get(DownloadItem, item_id).video_id = video_id
                session.add(
                    ScrapeCandidate(
                        video_id=video_id,
                        source_url=f"https://x/{video_id}",
                        scrape_source_url=f"https://x/{video_id}",
                        account_id=account_id,
                    )
                )
                session.commit()

    yield _Fixture()

    with get_session() as session:
        for account_id in accounts:
            for item in session.query(DownloadItem).filter_by(account_id=account_id).all():
                if item.video_id:
                    session.query(ScrapeCandidate).filter_by(video_id=item.video_id).delete()
                session.delete(item)
            session.query(ScrapeCandidate).filter_by(account_id=account_id).delete()
            account = session.get(Account, account_id)
            if account is not None:
                session.delete(account)
        session.commit()


def test_frame_prep_fills_a_missing_on_screen_hook(monkeypatch, hook_fixture) -> None:
    """The hook has to exist before the prompt builder reads it.

    Two shipping rules need one to point at - the evidence block's "match the
    angle" and title_lab's ONE EXCEPTION - and the field was empty on 161 of 185
    reels. Frame prep is where it belongs: the UI calls it before building the
    prompt, and the footage is guaranteed on disk, which it is not at distribute
    time.
    """
    account_id, item_ids = hook_fixture.account("NuHistory", 1)
    hook_fixture.link(item_ids[0], "vid-nu-1", account_id)

    monkeypatch.setattr(
        draft_handoff.on_screen_hook, "read_hook", lambda path: ("A read hook", 0.97)
    )
    monkeypatch.setattr(Path, "exists", lambda self: True)

    assert draft_handoff._ensure_on_screen_hooks(item_ids) == 1

    with get_session() as session:
        stored = (
            session.query(ScrapeCandidate.on_screen_title)
            .filter(ScrapeCandidate.video_id == "vid-nu-1")
            .scalar()
        )
    assert stored == "A read hook"


def test_an_existing_hook_is_never_re_read(monkeypatch, hook_fixture) -> None:
    # Some of the 719 rows already there were hand-checked; OCR must not stomp them.
    account_id, item_ids = hook_fixture.account("PiHistory", 1)
    hook_fixture.link(item_ids[0], "vid-pi-1", account_id)
    with get_session() as session:
        row = session.query(ScrapeCandidate).filter_by(video_id="vid-pi-1").one()
        row.on_screen_title = "hand checked"
        session.commit()

    def boom(path):
        raise AssertionError("should not have re-read an existing hook")

    monkeypatch.setattr(draft_handoff.on_screen_hook, "read_hook", boom)
    monkeypatch.setattr(Path, "exists", lambda self: True)

    assert draft_handoff._ensure_on_screen_hooks(item_ids) == 0


def test_a_low_confidence_hook_is_dropped_not_stored(monkeypatch, hook_fixture) -> None:
    """A wrong hook is worse than none.

    The prompt tells the model to MATCH THE ANGLE of whatever hook it is handed,
    so a garbled read would steer every option on that reel. RapidOCR separates
    the cases cleanly: real banners read above 0.95, music-only frames near 0.25.
    """
    account_id, item_ids = hook_fixture.account("XiHistory", 1)
    hook_fixture.link(item_ids[0], "vid-xi-1", account_id)

    monkeypatch.setattr(
        draft_handoff.on_screen_hook, "read_hook", lambda path: ("garbled noise", 0.20)
    )
    monkeypatch.setattr(Path, "exists", lambda self: True)

    assert draft_handoff._ensure_on_screen_hooks(item_ids) == 0
    with get_session() as session:
        stored = (
            session.query(ScrapeCandidate.on_screen_title)
            .filter(ScrapeCandidate.video_id == "vid-xi-1")
            .scalar()
        )
    assert not stored


def test_a_failing_hook_read_never_blocks_the_batch(monkeypatch, hook_fixture) -> None:
    # A reel whose banner will not read still drafts from its caption and sheet.
    account_id, item_ids = hook_fixture.account("OmicronHistory", 1)
    hook_fixture.link(item_ids[0], "vid-om-1", account_id)

    def boom(path):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(draft_handoff.on_screen_hook, "read_hook", boom)
    monkeypatch.setattr(Path, "exists", lambda self: True)

    assert draft_handoff._ensure_on_screen_hooks(item_ids) == 0  # no exception


def test_a_missing_vision_pass_does_not_ask_for_timid_writing() -> None:
    """No vision payload is the normal case, not a degraded one.

    The vision pass is Groq-backed and no key is configured, so 0 of 185 items
    on the live account carry a payload. The guidance used to end "stay more
    conservative", which therefore fired on every single reel while the contact
    sheet and the source's own OCR'd hook sat in the same block as real
    evidence. Hedged titles were being asked for.
    """
    guidance = NL.join(draft_handoff._VISUAL_EVIDENCE_GUIDANCE)

    assert "stay more conservative" not in guidance
    assert "NOT a reason to write timidly" in guidance
    # the fact-safety half must survive the reword
    assert "conservative about SPECIFICS the evidence cannot carry" in guidance


def test_both_batch_prompts_offer_a_concern_line_instead_of_a_dropped_block() -> None:
    """An objection must cost a line, never the whole block.

    The contract says "one block per reel, one for every reel" and forbids
    skipping, but named no way to register a problem - so a model with a real
    one answered in prose outside the format, the block vanished, the import
    reported "1 unmatched", and the reel reached Finish Batch with no draft,
    where it exports under its raw scraped caption.
    """
    account_id, item_ids = _make_second_account_items("MuHistory", 2)

    single = draft_handoff.build_account_batch_chat_prompt(account_id, item_ids)
    multi = draft_handoff.build_multi_account_batch_chat_prompt(item_ids, {})

    for prompt in (single, multi):
        assert "Concern:" in prompt
        assert "still return its block" in prompt
        # the hatch must not weaken the no-skip rule it sits beside
        assert "skip any reel" in prompt


def test_batch_candidates_lists_drafted_reels_last_and_flags_them() -> None:
    """An imported reel must stay on this screen.

    Dropping it meant the row vanished on the next poll: redoing a title had to
    go through Processing, and an import that silently matched nothing looked
    exactly like one that worked. `available` still counts only the draftless
    reels, so the "N of M draftless" label keeps its meaning.
    """
    account_id, item_ids = _make_second_account_items("IotaHistory", 3)
    draft_handoff.import_pasted_draft(
        item_ids[0],
        "Title Option 1:\nAlready drafted\nShared Caption:\nA caption.",
    )

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    # draftless first, the drafted reel appended after them
    assert [item["id"] for item in mine["items"]] == [*item_ids[1:], item_ids[0]]
    assert [item["has_draft"] for item in mine["items"]] == [False, False, True]
    assert mine["available"] == 2


def test_batch_candidates_flags_an_imported_reel_that_was_never_finished() -> None:
    """An import that never reached Finish is stranded work, not a finished draft.

    Importing writes a DraftRevision; only finish_batch applies an option onto
    the item's title_draft/caption_draft. A reel left between the two used to
    read here as plain "drafted" and drop out of the queue for good, while the
    Processing table still showed it as pending review - 23 reels had piled up
    that way with no surface left that could act on them.
    """
    account_id, item_ids = _make_second_account_items("NuHistory", 3)
    draft_handoff.import_pasted_draft(
        item_ids[0],
        NL.join(["Title Option 1:", "Imported, never finished", "Shared Caption:", "A caption."]),
    )

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)
    stranded = next(item for item in mine["items"] if item["id"] == item_ids[0])

    assert stranded["needs_finish"] is True
    assert stranded["has_draft"] is True
    assert mine["pending_finish"] == 1
    # It needs finishing, not re-drafting, so it stays out of the draftless count.
    assert mine["available"] == 2


def test_batch_candidates_clears_the_finish_flag_once_the_draft_is_applied() -> None:
    """Applying an option is what turns an import into a real draft."""
    account_id, item_ids = _make_second_account_items("XiHistory", 2)
    draft_handoff.import_pasted_draft(
        item_ids[0],
        NL.join(["Title Option 1:", "Applied for real", "Shared Caption:", "A caption."]),
    )
    draft_revisions.apply_revision(item_ids[0], 1)

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)
    applied = next(item for item in mine["items"] if item["id"] == item_ids[0])

    assert applied["needs_finish"] is False
    assert applied["has_draft"] is True
    assert mine["pending_finish"] == 0


def test_batch_candidates_lists_unfinished_reels_ahead_of_finished_ones() -> None:
    """The per-account cap must not be what hides stranded work.

    Both groups are capped at ``per_account``, so with finished drafts listed
    first a backlog of them would push every unfinished reel past the cut - the
    exact way the 23 stayed invisible behind a "6 already drafted" line.
    """
    account_id, item_ids = _make_second_account_items("OmicronHistory", 3)
    for index, item_id in enumerate(item_ids, start=1):
        draft_handoff.import_pasted_draft(
            item_id,
            NL.join(["Title Option 1:", f"Draft {index}", "Shared Caption:", "A caption."]),
        )
    # The first two were finished; the third was left between import and Finish.
    draft_revisions.apply_revision(item_ids[0], 1)
    draft_revisions.apply_revision(item_ids[1], 1)

    groups = draft_handoff.batch_candidates(niche="history", per_account=1)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert [item["id"] for item in mine["items"]][0] == item_ids[2]
    assert mine["pending_finish"] == 1


def test_batch_candidates_drops_a_drafted_reel_once_its_export_is_committed() -> None:
    """A booked export must leave this screen even though it has a draft.

    Drafted reels are now listed, and the screen's post-finish reconcile reads
    "reels that did export drop out of the candidate list" to tell a successful
    18-reel finish from a no-op. Having a draft used to be what removed them, so
    the commit check has to do it now or a good finish reads as "did nothing".
    """
    account_id, item_ids = _make_second_account_items("LambdaHistory", 2)
    draft_handoff.import_pasted_draft(
        item_ids[0],
        "Title Option 1:\nDrafted then exported\nShared Caption:\nA caption.",
    )

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)
    assert item_ids[0] in [item["id"] for item in mine["items"]]

    _add_upload_job(account_id, item_ids[0], "scheduled", posted=False)

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)
    assert item_ids[0] not in [item["id"] for item in mine["items"]]


def test_batch_candidates_skips_resting_accounts() -> None:
    account_id, _ = _make_second_account_items("KappaHistory", 2)

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


def test_batch_candidates_offers_a_reel_another_account_already_posted() -> None:
    """The source-video check is PER ACCOUNT now that the pool is shared.

    It used to skip any reel whose footage had gone out anywhere in the network.
    That made a shared pool behave like private inventory - the same reasoning
    as ``assignments.MIN_REUSE_GAP_DAYS``: a sibling account posting it is
    spaced by the reuse gap at distribution time, not blocked here. What this
    check still catches is ONE account repeating its own footage, including
    through the legacy direct-download path, whose clips were never pool items
    and so carry no assignment row to catch it.
    """
    poster_id, posted_ids = _make_second_account_items("TauHistory", 2)
    _add_upload_job(poster_id, posted_ids[0], "posted", posted=True)
    other_id, other_ids = _make_second_account_items("UpsilonHistory", 2)
    with get_session() as session:
        shared = session.get(DownloadItem, posted_ids[0]).video_id or "shared-reel"
        session.get(DownloadItem, posted_ids[0]).video_id = shared
        # The publisher's OWN second copy of that footage...
        session.get(DownloadItem, posted_ids[1]).video_id = shared
        # ...and a different account's copy of the same footage.
        session.get(DownloadItem, other_ids[0]).video_id = shared
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    other = next(g for g in groups if g["account_id"] == other_id)
    poster = next(g for g in groups if g["account_id"] == poster_id)

    # The sibling may draft it: both of its reels are offered.
    assert [item["id"] for item in other["items"]] == other_ids
    assert other["available"] == 2
    # The account that published it may not run it again.
    assert [item["id"] for item in poster["items"]] == []
    assert poster["available"] == 0


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


def test_batch_candidates_relink_footage_that_is_already_on_disk(tmp_path) -> None:
    # The regression this screen kept hitting: distribution writes the row before
    # the shared asset downloads, and the only backfill is a one-shot daemon
    # thread. When that thread dies (app closed, transient failure) the row keeps
    # a NULL file_path forever, so the screen reported "waiting on footage" on
    # every refresh even though a sibling account had already pulled the footage.
    # Loading the screen must re-link it instead of stalling until the user opens
    # each reel in Processing by hand.
    media_file = tmp_path / "sibling.mp4"
    media_file.write_bytes(b"video")
    account_id, item_ids = _make_second_account_items("SigmaHistory", 2)
    with get_session() as session:
        stalled = session.get(DownloadItem, item_ids[0])
        stalled.file_path = None
        stalled.status = "pending_review"
        stalled.review_state = "pending_review"
        stalled.video_id = "sigma1"
        session.add(
            MediaAsset(
                canonical_source_url=stalled.source_url,
                source_shortcode="sigma1",
                download_status="downloaded",
                original_download_path=str(media_file),
            )
        )
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert mine["pending_media"] == 0
    assert [item["id"] for item in mine["items"]] == item_ids
    assert mine["available"] == 2
    with get_session() as session:
        assert session.get(DownloadItem, item_ids[0]).file_path == str(media_file)


def test_batch_candidates_still_report_footage_that_is_genuinely_absent() -> None:
    # The counterpart: re-linking must not paper over a clip whose footage was
    # never downloaded at all. Those stay counted so the retry button appears.
    account_id, item_ids = _make_second_account_items("TauHistory", 2)
    with get_session() as session:
        stalled = session.get(DownloadItem, item_ids[0])
        stalled.file_path = None
        stalled.status = "pending_review"
        stalled.review_state = "pending_review"
        stalled.video_id = "tau1"
        session.add(
            MediaAsset(
                canonical_source_url=stalled.source_url,
                source_shortcode="tau1",
                download_status="pending",
            )
        )
        session.commit()

    groups = draft_handoff.batch_candidates(niche="history", per_account=10)
    mine = next(g for g in groups if g["account_id"] == account_id)

    assert mine["pending_media"] == 1
    assert [item["id"] for item in mine["items"]] == item_ids[1:]


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
    # The style rule must still be the one voice that IS present. The default
    # moved from "a reactive option" to "an option carrying no hard specific"
    # once the specificity gap (2.1x) was measured against the register gap.
    assert "carrying NO" in prompt and "is the default pick" in prompt
    assert "a REACTIVE option is the default pick" not in prompt


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


def test_import_batch_skips_a_reel_rejected_after_prepare() -> None:
    # The batch's item list is pinned when the prompt is copied, so a reject made
    # afterwards still has a block in the pasted reply. That block must not
    # become a draft revision, or the reel reads as "ready" in the finish plan
    # and gets exported and re-scheduled — undoing the reject.
    _account_id, ids = _make_second_account_items("RejectAfterPrepare", 3)
    with get_session() as session:
        session.get(DownloadItem, ids[1]).review_state = "rejected"
        session.commit()

    reply = "\n".join(
        line
        for index, item_id in enumerate(ids, start=1)
        for line in (
            f"===== REEL {index} (#{item_id}) =====",
            "Title Option 1:",
            f"Hook {index}",
            "Recommended Pick: Title Option 1",
            "Shared Caption:",
            f"Caption body {index}.",
        )
    )

    result = draft_handoff.import_multi_account_batch_draft(reply, ids)

    assert result["imported"] == [ids[0], ids[2]]
    assert result["skipped"] == [
        {"item_id": ids[1], "reason": "rejected since this batch was prepared"}
    ]
    # A rejected reel is a deliberate exclusion, not a lost block.
    assert result["unmatched"] == []
    assert draft_revisions.latest_revision(ids[1]) is None
    # The reels either side still get THEIR own draft: the rejected reel keeps
    # its slot in the pinned list, so positional routing never shifts.
    assert draft_revisions.latest_revision(ids[0]).title_options == ["Hook 1"]
    assert draft_revisions.latest_revision(ids[2]).title_options == ["Hook 3"]


def test_import_batch_keeps_positional_routing_when_ids_are_dropped() -> None:
    # Same as above but the model dropped every "(#id)", so routing falls back to
    # reel NUMBER position. This is the case that breaks if a rejected reel is
    # removed from the pinned list instead of skipped at the save step.
    _account_id, ids = _make_second_account_items("RejectNoEchoedId", 3)
    with get_session() as session:
        session.get(DownloadItem, ids[0]).review_state = "rejected"
        session.commit()

    reply = "\n".join(
        line
        for index in range(1, 4)
        for line in (
            f"===== REEL {index} =====",
            "Title Option 1:",
            f"Hook {index}",
            "Recommended Pick: Title Option 1",
            "Shared Caption:",
            f"Caption body {index}.",
        )
    )

    result = draft_handoff.import_multi_account_batch_draft(reply, ids)

    assert result["imported"] == [ids[1], ids[2]]
    assert draft_revisions.latest_revision(ids[1]).title_options == ["Hook 2"]
    assert draft_revisions.latest_revision(ids[2]).title_options == ["Hook 3"]


# --- Clip Studio title handoff ---------------------------------------------- #


def _clip_account() -> int:
    with get_session() as session:
        account = Account(
            name="Resurfaced History",
            platform="instagram",
            niche="history",
            niche_label="History moments and forgotten stories",
            hook_style="specific subject-first history hooks",
        )
        session.add(account)
        session.commit()
        return account.id


def test_clip_title_prompt_separates_the_clip_from_its_context() -> None:
    """The model needs the surrounding minute to know the subject, but the title
    still has to be delivered by the clip — so the two are labelled apart."""
    prompt = draft_handoff.build_clip_title_prompt(
        account_id=_clip_account(),
        clip_words="This card is $400,000.",
        surrounding_context="They discuss a rare Pikachu card collection.",
        source_title="The Pokemon Card Scandal",
        range_label="49:40-49:55",
    )

    assert "WHAT THE CLIP ITSELF SAYS" in prompt
    assert "SURROUNDING CONTEXT" in prompt
    assert "This card is $400,000." in prompt
    assert "rare Pikachu card collection" in prompt
    assert "The Pokemon Card Scandal" in prompt
    # The paste parser matches these at line start; a prefix breaks the import.
    for header in ("Title Option 1:", "Title Option 2:", "Title Option 3:"):
        assert f"\n{header}" in prompt
    # Captions are built from the campaign template, never written by the model.
    assert "Caption Option" not in prompt


def test_clip_title_prompt_carries_the_accounts_own_voice() -> None:
    prompt = draft_handoff.build_clip_title_prompt(
        account_id=_clip_account(), clip_words="This card is $400,000."
    )

    assert "Resurfaced History" in prompt
    assert "specific subject-first history hooks" in prompt
    # Same rule source as the live API prompt, keyed off the account's style.
    assert "historytrails" in prompt.lower() or "documentary" in prompt.lower()


def test_clip_batch_prompt_states_the_rules_once_for_every_clip() -> None:
    """An earlier version derived the batch prompt by slicing the single-clip
    one, which silently dropped every title rule: they sit after the per-clip
    material, so the slice kept only the account header."""
    account_id = _clip_account()
    single = draft_handoff.build_clip_title_prompt(
        account_id=account_id, clip_words="This card is $400,000."
    )
    batch = draft_handoff.build_clip_batch_title_prompt(
        account_id=account_id,
        clips=[
            {"number": 1, "words": "This card is $400,000.", "range_label": "49:40-49:55"},
            {"number": 2, "words": "Around $60 million of fraud.", "range_label": "27:12"},
        ],
    )

    assert "On-screen title rules" in batch
    assert "Hook framing" in batch
    # The rule block is the bulk of the prompt; a batch of two must not be
    # dramatically smaller than one clip's prompt.
    assert len(batch) > len(single) * 0.8
    assert "There are 2 clips below" in batch
    assert "\nClip 1: 49:40-49:55" in batch
    assert "\nClip 2: 27:12" in batch


def test_clip_batch_reply_routes_by_number_not_position() -> None:
    """A reply that reorders or omits clips must still land each block on the
    right candidate rather than shifting every one of them along."""
    reply = "\n".join(
        [
            "Clip 3:",
            "Title Option 1:",
            "Third clip title",
            "Recommended Pick: Title Option 1",
            "",
            "Clip 1:",
            "Title Option 1:",
            "First clip title",
            "Recommended Pick: Title Option 1",
        ]
    )

    parsed = draft_handoff.parse_clip_batch_titles(reply)

    assert sorted(parsed) == [1, 3]
    assert parsed[1].title_options == ["First clip title"]
    assert parsed[3].title_options == ["Third clip title"]


def test_clip_batch_reply_ignores_blocks_with_no_titles() -> None:
    reply = "Clip 1:\nSorry, I could not write titles for this one.\n"
    assert draft_handoff.parse_clip_batch_titles(reply) == {}


def test_multi_account_prompt_uses_the_account_saved_title_style() -> None:
    """The batch screen sends {}, so styles must resolve from the account.

    Regression: with an empty settings dict every account resolved
    title_style=None and fell through to the caption-derived rules, so the
    historytrails_record rule set the accounts are configured for - the register
    mix, the specificity rules, the scene-description format - never reached the
    prompt the operator actually pasted into a chat model.
    """
    account = Account(
        id=1,
        name="Vaulted History",
        platform="instagram",
        instagram_handle="vaultedhistory",
        niche_label="history",
        processing_preferences=json.dumps(
            {
                "prompt_title_style": "historytrails_record",
                "caption_style": "history_lost_archive",
                "title_length": "auto",
            }
        ),
    )

    rules = "\n".join(draft_handoff._account_style_rules(account, {}))

    assert "BE CLEAR, NOT SPECIFIC" in rules
    assert "THE THREE OPTIONS SPAN SPECIFICITY" in rules
    assert "REGISTER MIX (HARD RULE)" in rules


def test_beats_v2_is_selected_per_account_through_the_title_style() -> None:
    """v2 is a title STYLE, not a global switch.

    Selecting it per account is what makes an A/B possible: one account can run
    the new rules while the rest keep theirs, and reverting is a dropdown change
    rather than a restart. Every other style must still resolve to its own block.
    """
    account = Account(
        id=1,
        name="Vaulted History",
        platform="instagram",
        instagram_handle="vaultedhistory",
        niche_label="history",
        processing_preferences=json.dumps({"prompt_title_style": "historytrails_record"}),
    )

    # The account is saved as historytrails_record, so it keeps the old block.
    rules = "\n".join(draft_handoff._account_style_rules(account, {}))
    assert "THE THREE OPTIONS SPAN SPECIFICITY" in rules
    # "END ON A VERDICT" is the v2 marker; it was "TELL IT AS A SEQUENCE"
    # until that rule was rewritten to lead on the ending, not the beat count.
    assert "END ON A VERDICT" not in rules

    # Picking the new style in the dropdown swaps the rulebook for this account.
    picked = "\n".join(
        draft_handoff._account_style_rules(
            account, {"title_style": draft_handoff.TITLE_STYLE_BEATS_V2}
        )
    )
    assert "END ON A VERDICT" in picked
    assert "THE THREE OPTIONS SPAN SPECIFICITY" not in picked
    # And the rule that keeps a hook's unsupported claim out of the title rides with it.
    assert "THE SOURCE'S OWN ON-SCREEN HOOK IS NOT EVIDENCE" in picked


def test_explicit_settings_still_win_over_the_account_preferences() -> None:
    account = Account(
        id=1,
        name="Vaulted History",
        platform="instagram",
        instagram_handle="vaultedhistory",
        niche_label="history",
        processing_preferences=json.dumps({"prompt_title_style": "historytrails_record"}),
    )

    rules = "\n".join(
        draft_handoff._account_style_rules(account, {"title_style": "cinema_hook"})
    )

    assert "THE THREE OPTIONS SPAN SPECIFICITY" not in rules


def test_prompt_carries_the_source_on_screen_hook_as_an_angle_not_a_template() -> None:
    """The line the drafter was missing entirely.

    Without it the only evidence is the scraped caption, which on these accounts
    is descriptive prose - so a real batch wrote "A woman in bright pink loses
    her footing on the stairs as the dog behind her closes the gap" while the
    source's own on-screen line read "The Breed (2006) features a scene so
    accidentally hilarious it completely steals the movie".
    """
    with get_session() as session:
        candidate = ScrapeCandidate(
            scrape_source_url="https://www.instagram.com/historytrails/",
            source_url="https://www.instagram.com/p/ABC123/",
            video_id="ABC123",
            state="pooled",
            account_id=1,
            on_screen_title="The Breed (2006) features a scene so hilarious it steals the movie",
        )
        item = DownloadItem(
            source_url="https://www.instagram.com/p/ABC123/",
            video_id="ABC123",
            status="completed",
            title="Fierce pursuit unleashes absolute mayhem as a woman clad in bright pink",
        )
        session.add_all([candidate, item])
        session.flush()

        lines = draft_handoff._on_screen_hook_lines(item)

    assert len(lines) == 1
    body = lines[0]
    assert "The Breed (2006)" in body
    # Framed as an angle to match, never as text to reuse.
    assert "MATCH THE ANGLE AND THE REGISTER" in body
    assert "Do NOT reuse its wording" in body
    assert "do NOT inherit its factual claims" in body


def test_on_screen_hook_line_is_omitted_when_the_reel_has_none() -> None:
    """Coverage is 14% of scraped candidates, so absence is the common case."""
    with get_session() as session:
        item = DownloadItem(
            source_url="https://www.instagram.com/p/NOHOOK/",
            video_id="NOHOOK",
            status="completed",
        )
        session.add(item)
        session.flush()

        assert draft_handoff._on_screen_hook_lines(item) == []


def test_import_pasted_draft_rejects_an_all_metadata_reply() -> None:
    # Item 1959: the scraped caption said Qajar dynasty (ended 1925), the
    # footage showed 1970s Iran, and the drafting model wrote that finding into
    # all three title slots instead of drafting or declining. Every option was
    # green (nothing checkable was claimed), so Apply burned one onto the video.
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.source_description = "Iran's royal family lived a secluded life in the Qajar dynasty."
        session.commit()

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.import_pasted_draft(
            item_id,
            """Title Option 1:
The caption attached to this clip is about a dynasty gone a century ago.
Title Option 2:
This footage is mislabeled and its real source cannot be confirmed.
Title Option 3:
What's on screen is not what the source caption describes.
Recommended Pick: Title Option 1
Why: Stays true to the mismatch.
Shared Caption:
Young people move through sunlit streets in this clip.
""",
        )

    assert "metadata" in str(excinfo.value)
    assert "Nothing was saved" in str(excinfo.value)
    assert draft_revisions.list_revisions(item_id) == []


def test_import_pasted_draft_keeps_a_reply_with_one_usable_title() -> None:
    # One bad option is not a refusal: it reddens and the pick moves off it,
    # exactly like an unsupported claim, and the revision still saves.
    item_id = _make_item()

    saved = draft_handoff.import_pasted_draft(
        item_id,
        """Title Option 1:
The caption attached to this clip does not match the footage.
Title Option 2:
Three water skiers crossed the bay carrying a flag that no longer exists.
Recommended Pick: Title Option 1
Why: Most honest about the mismatch.
Shared Caption:
Water skiers cut across a bay in formation.
""",
    )

    assert saved.option_tiers[0] == "red"
    assert saved.option_tiers[1] != "red"
    assert saved.recommended_title_index == 2
    assert "Not a viewer-facing title" in saved.option_notes[0]



def _item_with_media(tmp_path) -> int:
    """An item whose file_path actually exists, so _ensure_transcripts reaches
    the transcriber instead of skipping on its missing-file guard."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not really a video")
    item_id = _make_item()
    with get_session() as session:
        session.get(DownloadItem, item_id).file_path = str(media)
        session.commit()
    return item_id


def test_ensure_transcripts_stores_real_speech(monkeypatch, tmp_path) -> None:
    # The gap behind both live failures: every item was drafted with
    # transcript_text empty, so the reposter's caption was the only claim about
    # who is in the clip. Item 1948's proof that Snoop Dogg is really there was
    # in an audio track nothing had ever read.
    item_id = _item_with_media(tmp_path)
    monkeypatch.setattr(draft_handoff.video, "has_audio_stream", lambda path: True)
    monkeypatch.setattr(
        draft_handoff.transcription,
        "generate_transcript_draft_in_subprocess",
        lambda path: SimpleNamespace(
            transcript_text=(
                "Little cute little lizard. What the hell is going on? "
                "Snakes are straight assholes, you could tell what they looking at."
            ),
            title_draft="ignored",
            caption_draft="ignored",
        ),
    )

    assert draft_handoff._ensure_transcripts([item_id]) == 1
    with get_session() as session:
        assert "lizard" in (session.get(DownloadItem, item_id).transcript_text or "")


def test_ensure_transcripts_drops_a_short_hallucination(monkeypatch, tmp_path) -> None:
    # Item 1959: music-only footage where whisper emitted "I passed along the
    # state". Stored, it would sit in the prompt as evidence and steer all three
    # titles — worse than the empty field it replaced.
    item_id = _item_with_media(tmp_path)
    monkeypatch.setattr(draft_handoff.video, "has_audio_stream", lambda path: True)
    monkeypatch.setattr(
        draft_handoff.transcription,
        "generate_transcript_draft_in_subprocess",
        lambda path: SimpleNamespace(
            transcript_text="I passed along the state",
            title_draft="",
            caption_draft="",
        ),
    )

    assert draft_handoff._ensure_transcripts([item_id]) == 0
    with get_session() as session:
        assert not (session.get(DownloadItem, item_id).transcript_text or "")


def test_ensure_transcripts_never_blocks_a_batch(monkeypatch, tmp_path) -> None:
    # Best-effort on the same terms as the on-screen hook read: a whisper
    # failure must not stop the batch from being prepared.
    item_id = _item_with_media(tmp_path)

    def boom(path):
        raise RuntimeError("faster-whisper exploded")

    monkeypatch.setattr(draft_handoff.video, "has_audio_stream", lambda path: True)
    monkeypatch.setattr(
        draft_handoff.transcription, "generate_transcript_draft_in_subprocess", boom
    )

    assert draft_handoff._ensure_transcripts([item_id]) == 0


def test_ensure_transcripts_skips_items_that_already_have_one(monkeypatch) -> None:
    # A transcript the user typed by hand must never be overwritten.
    item_id = _make_item()
    with get_session() as session:
        session.get(DownloadItem, item_id).transcript_text = "hand written context"
        session.commit()

    def fail(path):
        raise AssertionError("should not re-transcribe")

    monkeypatch.setattr(draft_handoff.video, "has_audio_stream", lambda path: True)
    monkeypatch.setattr(
        draft_handoff.transcription, "generate_transcript_draft_in_subprocess", fail
    )

    assert draft_handoff._ensure_transcripts([item_id]) == 0
    with get_session() as session:
        assert session.get(DownloadItem, item_id).transcript_text == "hand written context"


def _batch_reply(*blocks: tuple[int, int | None, str, str | None]) -> str:
    lines: list[str] = []
    for reel, echoed, hook, label in blocks:
        header = f"===== REEL {reel} =====" if echoed is None else f"===== REEL {reel} (#{echoed}) ====="
        lines.extend(
            [
                f"{header} {label}" if label else header,
                "Title Option 1:",
                hook,
                "Recommended Pick: Title Option 1",
                "Shared Caption:",
                f"{hook} caption body.",
            ]
        )
    return "\n".join(lines)


def test_import_multi_account_batch_rejects_a_reply_prepared_for_another_row() -> None:
    # The silent misroute this guard exists for: several rows are prepared at
    # once, and the operator imports account A's chat reply onto account B's row.
    # Every echoed id misses B's batch, so routing used to fall back to reel
    # NUMBER and file all of A's drafts onto B's clips - reported as a clean
    # "imported 2" and carried through to export and scheduling.
    _a1, alpha = _make_second_account_items("Resurfaced History", 2)
    _a2, beta = _make_second_account_items("Vaulted History", 2)
    reply = _batch_reply(
        (1, alpha[0], "Alpha hook one", "Resurfaced History"),
        (2, alpha[1], "Alpha hook two", "Resurfaced History"),
    )

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.import_multi_account_batch_draft(reply, beta)

    assert "Resurfaced History" in str(excinfo.value)
    assert "Vaulted History" in str(excinfo.value)
    assert [draft_revisions.latest_revision(item_id) for item_id in beta] == [None, None]


def test_import_multi_account_batch_keeps_importing_when_one_id_is_foreign() -> None:
    # The guard must not undo the tolerance it sits on top of: a single stray id
    # (a model parroting a frame filename's item id) still routes by reel number
    # as long as the rest of the reply points at this batch.
    _a1, own = _make_second_account_items("Lambda History", 2)
    _a2, other = _make_second_account_items("Mu History", 1)
    reply = _batch_reply(
        (1, own[0], "Own hook one", None),
        (2, other[0], "Own hook two", None),
    )

    result = draft_handoff.import_multi_account_batch_draft(reply, own)

    assert result["imported"] == own
    assert result["unmatched"] == []
    assert draft_revisions.latest_revision(own[1]).title_options == ["Own hook two"]


def test_import_multi_account_batch_allows_ids_that_match_no_item() -> None:
    # An id that names no video at all is garbage, not another row's reply, and
    # reel-number routing already handles it. Only a real foreign item can prove
    # the paste belongs somewhere else.
    _a1, own = _make_second_account_items("Nu History", 2)
    reply = _batch_reply(
        (1, 999001, "Garbled hook one", None),
        (2, 999002, "Garbled hook two", None),
    )

    result = draft_handoff.import_multi_account_batch_draft(reply, own)

    assert result["imported"] == own
    assert draft_revisions.latest_revision(own[0]).title_options == ["Garbled hook one"]


def test_import_account_batch_rejects_a_reply_labelled_with_another_account() -> None:
    # Single-account rows echo account-RELATIVE ids, which collide across
    # accounts by design, so the id can never prove a wrong-row paste here. The
    # account name trailing the header is the only evidence, and an exact match
    # on a real, different account is enough to stop the import.
    _a1, own = _make_account_items(2)
    _a2, _other = _make_second_account_items("Xi History", 2)
    reply = _batch_reply(
        (1, None, "Foreign hook one", "Xi History"),
        (2, None, "Foreign hook two", "Xi History"),
    )

    with pytest.raises(DraftRevisionError) as excinfo:
        draft_handoff.import_account_batch_draft(reply, own)

    assert "Xi History" in str(excinfo.value)
    assert [draft_revisions.latest_revision(item_id) for item_id in own] == [None, None]


def test_import_account_batch_accepts_headers_labelled_with_its_own_account() -> None:
    _account_id, own = _make_second_account_items("Omicron History", 2)
    reply = _batch_reply(
        (1, None, "Own hook one", "**Omicron History**"),
        (2, None, "Own hook two", "omicron   history"),
    )

    result = draft_handoff.import_account_batch_draft(reply, own)

    assert result["imported"] == own
    assert draft_revisions.latest_revision(own[0]).title_options == ["Own hook one"]


def test_import_account_batch_ignores_a_label_that_names_no_account() -> None:
    # Models paraphrase the trailing label ("Reel set B"). That is not evidence
    # of anything, so it must never block an otherwise valid import.
    _account_id, own = _make_second_account_items("Pi History", 2)
    reply = _batch_reply(
        (1, None, "Own hook one", "Reel set B"),
        (2, None, "Own hook two", "Reel set B"),
    )

    result = draft_handoff.import_account_batch_draft(reply, own)

    assert result["imported"] == own


def test_beats_v2_short_length_adds_a_hard_character_rule() -> None:
    short_rules = "\n".join(
        draft_handoff._effective_title_rules(
            draft_handoff.TITLE_STYLE_BEATS_V2, None, "history", title_length="short"
        )
    )
    limit = draft_guard.SHORT_TITLE_MAX_CHARS
    assert f"at most {limit} characters" in short_rules
    # A capped account needs no spread rule: the cap already fixes the band.
    assert "SPREAD THE THREE OPTIONS" not in short_rules
    assert "OVERRIDING THE 130-CHARACTER CEILING" in short_rules

    for other in ("auto", "long", None):
        rules = "\n".join(
            draft_handoff._effective_title_rules(
                draft_handoff.TITLE_STYLE_BEATS_V2, None, "history", title_length=other
            )
        )
        assert f"at most {limit} characters" not in rules
        # ...but an uncapped account must still be asked for a real choice.
        assert "SPREAD THE THREE OPTIONS" in rules


def test_import_pasted_draft_moves_pick_off_overlong_title_for_short_account() -> None:
    item_id = _make_item()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.processing_preferences = json.dumps(
            {"prompt_title_style": "beats_v2", "title_length": "short"}
        )
        session.commit()

    long_title = (
        "She walked into the room, everyone went quiet, and then the crowd slowly "
        "realised who had just arrived at the party"
    )
    revision = draft_handoff.import_pasted_draft(
        item_id,
        f"""Title Option 1:
Nobody expected her at the party
Recommended Style 1:
Editorial Italic
Caption Option 1:
First caption.

Title Option 2:
The room went quiet when she arrived
Recommended Style 2:
Editorial Italic
Caption Option 2:
Second caption.

Title Option 3:
{long_title}
Recommended Style 3:
Editorial Italic
Caption Option 3:
Third caption.

Recommended Pick: Title Option 3 + Caption Option 3
Why: It carries the most beats.
""",
    )

    assert revision.recommended_title_index in (1, 2)
    assert revision.recommended_title_index == revision.recommended_caption_index


def test_prompt_header_reports_the_styles_the_rules_actually_use() -> None:
    """The header is what a human reads to check a prompt; it must not contradict.

    It used to read ``settings`` while the rules read the account's saved
    preferences, so a batch screen sending ``{}`` printed "Title style: Auto"
    and "Title length: Long" above a beats_v2 rulebook with a Short cap.
    """
    short_account = Account(
        id=1,
        name="Beneath History",
        platform="instagram",
        instagram_handle="beneathhistory",
        niche_label="history",
        processing_preferences=json.dumps(
            {"prompt_title_style": draft_handoff.TITLE_STYLE_BEATS_V2, "title_length": "short"}
        ),
    )
    header = "\n".join(
        draft_handoff._account_prompt_header(short_account, settings={}, item_ids=[1])
    )
    assert f"Title style: {draft_handoff.TITLE_STYLE_BEATS_V2}" in header
    assert "Title length: Short" in header
    assert "Title length: Long" not in header

    auto_account = Account(
        id=2,
        name="Vaulted History",
        platform="instagram",
        instagram_handle="vaultedhistory",
        niche_label="history",
        processing_preferences=json.dumps(
            {"prompt_title_style": draft_handoff.TITLE_STYLE_BEATS_V2, "title_length": "auto"}
        ),
    )
    auto_header = "\n".join(
        draft_handoff._account_prompt_header(auto_account, settings={}, item_ids=[1])
    )
    # v2 ignores every length but "short", so printing one would be the same
    # bug in the other direction.
    assert "Title length:" not in auto_header
