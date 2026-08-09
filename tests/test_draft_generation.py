from __future__ import annotations

import pytest

import datetime as dt

from nicheflow_studio.db.models import AccountPostMetric, Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import smart_drafts
from nicheflow_studio.services import draft_generation
from nicheflow_studio.services.draft_revisions import DraftRevisionError


def _make_item(*, transcript: str | None = "A real transcript with enough words.") -> int:
    with get_session() as session:
        account = Account(
            name="Movie",
            platform="instagram",
            niche_label="movie",
            writing_tone="cinematic",
            target_audience="film fans",
        )
        session.add(account)
        session.commit()
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Source clip",
            file_path="C:/clips/x.mp4",
            transcript_text=transcript,
            status="completed",
            account_id=account.id,
        )
        session.add(item)
        session.commit()
        return item.id


def _fake_drafts() -> smart_drafts.SmartDrafts:
    return smart_drafts.SmartDrafts(
        summary="A clip about something.",
        title_options=["Title one", "Title two", "Title three"],
        caption_options=["Cap one", "Cap two", "Cap three"],
        provider_label="Groq (test)",
        recommended_title_index=1,  # 0-based -> expect 2 stored (1-based)
        recommended_caption_index=0,  # -> expect 1
        recommendation_reason="strongest",
        option_notes=["n1", "n2", "n3"],
        option_tiers=["green", "yellow", "yellow"],
    )


def test_generate_saves_revision_and_maps_recommended_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = _make_item()
    captured: dict = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return _fake_drafts()

    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", fake_generate)

    dto = draft_generation.generate_revision_for_item(
        item_id, caption_style="contextual_info", title_style="meme_setup_punchline"
    )

    assert dto.revision_number == 1
    assert dto.title_options == ["Title one", "Title two", "Title three"]
    assert dto.source == "ui"
    # 0-based SmartDrafts index becomes 1-based on the revision.
    assert dto.recommended_title_index == 2
    assert dto.recommended_caption_index == 1
    assert dto.title_style_preset == "meme_setup_punchline"
    # Account voice was assembled from Account columns and passed through.
    assert captured["account_voice"]["tone"] == "cinematic"
    assert captured["niche_label"] == "movie"


def test_generate_passes_accounts_measured_top_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    captured: dict = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return _fake_drafts()

    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", fake_generate)

    draft_generation.generate_revision_for_item(item_id)

    assert captured["few_shot_winners"] == ["Measured winner one", "Measured winner two"]


def test_generate_threads_title_length_and_requires_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = _make_item()
    captured: dict = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return _fake_drafts()

    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", fake_generate)

    draft_generation.generate_revision_for_item(item_id, title_length="short")

    assert captured["title_length"] == "short"
    assert captured["require_vision"] is True


def test_generate_flags_unsupported_claim_and_moves_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The "heavy pram" case: the model writes a concrete claim no signal
    # supports and even rates it green. The deterministic guard downgrades the
    # option to red and moves the recommendation (title+caption together) to
    # the first clean option.
    item_id = _make_item()
    flagged_drafts = smart_drafts.SmartDrafts(
        summary="Vintage prams.",
        title_options=[
            "Would you push one of these today?",
            "These prams were insanely heavy",
            "A stroll through old London",
        ],
        caption_options=["Cap one", "Cap two", "Cap three"],
        provider_label="Groq (test)",
        recommended_title_index=1,
        recommended_caption_index=1,
        recommendation_reason="boldest hook",
        option_notes=["n1", "n2", "n3"],
        option_tiers=["green", "green", "green"],
        claim_supports=["none needed", "none", "none needed"],
    )
    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", lambda **_: flagged_drafts)

    dto = draft_generation.generate_revision_for_item(item_id)

    assert dto.option_tiers == ["green", "red", "green"]
    assert dto.recommended_title_index == 1
    assert dto.recommended_caption_index == 1
    assert "no support in the clip signals" in dto.recommendation_reason
    assert "'heavy'" in dto.option_notes[1]


def test_generate_keeps_supported_claim_and_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the transcript actually backs the claim, the guard must not touch
    # the tier or the recommendation — grounded facts are good hooks.
    item_id = _make_item(
        transcript="Narrator: each of these prams weighed more than a modern bicycle."
    )
    drafts = smart_drafts.SmartDrafts(
        summary="Vintage prams.",
        title_options=[
            "Would you push one of these today?",
            "These prams were insanely heavy",
            "A stroll through old London",
        ],
        caption_options=["Cap one", "Cap two", "Cap three"],
        provider_label="Groq (test)",
        recommended_title_index=1,
        recommended_caption_index=1,
        recommendation_reason="boldest hook",
        option_notes=["n1", "n2", "n3"],
        option_tiers=["green", "yellow", "green"],
        claim_supports=["none needed", "weighed more than a modern bicycle", "none needed"],
    )
    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", lambda **_: drafts)

    dto = draft_generation.generate_revision_for_item(item_id)

    assert dto.option_tiers == ["green", "yellow", "green"]
    assert dto.recommended_title_index == 2
    assert dto.recommendation_reason == "boldest hook"
    assert dto.option_notes == ["n1", "n2", "n3"]


def test_generate_without_transcript_succeeds_via_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No transcript is fine: speechless reels are grounded by vision + the source
    # caption/niche, so generation should proceed instead of being gated.
    item_id = _make_item(transcript=None)
    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", lambda **_: _fake_drafts())

    dto = draft_generation.generate_revision_for_item(item_id)

    assert dto.revision_number == 1
    assert dto.title_options == ["Title one", "Title two", "Title three"]


def test_generate_without_any_grounding_raises() -> None:
    # No transcript, no video on disk, and no title/caption/niche — there is
    # nothing for the generator to anchor to, so it still fails cleanly.
    with get_session() as session:
        account = Account(name="Bare", platform="instagram")
        session.add(account)
        session.commit()
        item = DownloadItem(
            source_url="https://instagram.com/reel/bare",
            title=None,
            file_path=None,
            transcript_text=None,
            status="completed",
            account_id=account.id,
        )
        session.add(item)
        session.commit()
        item_id = item.id

    with pytest.raises(DraftRevisionError, match="nothing to generate"):
        draft_generation.generate_revision_for_item(item_id)


def test_generate_unknown_item_raises() -> None:
    with pytest.raises(DraftRevisionError):
        draft_generation.generate_revision_for_item(99999)

def test_caption_outro_disabled_for_all_accounts() -> None:
    from types import SimpleNamespace

    history = SimpleNamespace(instagram_handle="pastmomentsdaily", niche="history")
    movie = SimpleNamespace(instagram_handle="@cinemafilesdaily", niche="movie")
    unknown = SimpleNamespace(instagram_handle="someacct", niche=None)
    no_handle = SimpleNamespace(instagram_handle=None, niche="history")

    # Disabled globally: every account (with or without a handle) gets no outro.
    for account in (history, movie, unknown, no_handle, None):
        assert draft_generation.caption_outro_for_account(account) is None


def test_caption_outro_templates_restore_when_re_enabled(monkeypatch) -> None:
    # The per-niche templates are dormant, not deleted: flipping the flag back on
    # restores them (handle-aware, leading @ not doubled).
    from types import SimpleNamespace

    monkeypatch.setattr(draft_generation, "_CAPTION_OUTRO_ENABLED", True)
    history = SimpleNamespace(instagram_handle="pastmomentsdaily", niche="history")
    movie = SimpleNamespace(instagram_handle="@cinemafilesdaily", niche="movie")
    unknown = SimpleNamespace(instagram_handle="someacct", niche=None)
    no_handle = SimpleNamespace(instagram_handle=None, niche="history")

    assert (
        draft_generation.caption_outro_for_account(history)
        == "Lost moments from history, every day → @pastmomentsdaily"
    )
    # Leading @ in the stored handle must not double up.
    assert (
        draft_generation.caption_outro_for_account(movie)
        == "One unforgettable scene a day → @cinemafilesdaily"
    )
    assert (
        draft_generation.caption_outro_for_account(unknown)
        == "More every day → @someacct"
    )
    assert draft_generation.caption_outro_for_account(no_handle) is None


def test_smart_draft_prompt_injects_caption_outro() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="T",
        source_description=None,
        niche_label="history",
        vision_payload=None,
        account_voice=None,
        prompt_profile=None,
        caption_style="history_lost_archive",
        caption_outro="Lost moments from history, every day → @pastmomentsdaily",
    )

    assert "FOLLOW OUTRO (MANDATORY)" in prompt
    assert "@pastmomentsdaily" in prompt
    assert "one blank line immediately BEFORE it" in prompt
    assert "one blank line immediately AFTER it" in prompt

    without = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="T",
        source_description=None,
        niche_label="history",
        vision_payload=None,
        account_voice=None,
        prompt_profile=None,
        caption_style="history_lost_archive",
    )

    assert "FOLLOW OUTRO" not in without


def test_space_caption_outro_adds_blank_lines_above_and_below() -> None:
    outro = "Lost moments from history, every day → @pastmomentsdaily"
    caption = f"Context paragraph.\n{outro}\n#history #royalfamily"

    assert draft_generation._space_caption_outro(caption, outro) == (
        f"Context paragraph.\n\n{outro}\n\n#history #royalfamily"
    )


def test_space_caption_outro_keeps_existing_readable_spacing() -> None:
    outro = "Lost moments from history, every day → @pastmomentsdaily"
    caption = f"Context paragraph.\n\n{outro}\n\n#history"

    assert draft_generation._space_caption_outro(caption, outro) == caption


def _vision_pass(payload: dict | None, error: str | None = None) -> smart_drafts.VisionPass:
    return smart_drafts.VisionPass(
        payload=payload,
        response=None,
        error=error,
        attempted=True,
        retry_attempted=False,
        frame_count=5,
        model="test-vision",
    )


def test_ensure_batch_vision_stops_after_a_provider_level_failure(monkeypatch, tmp_path) -> None:
    # A missing vision model 404s identically for every reel. Paying that
    # round-trip once per item added ~3s x batch size to "Prepare batch".
    items = [_make_item() for _ in range(4)]
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    with get_session() as session:
        for item_id in items:
            session.get(DownloadItem, item_id).file_path = str(video)
        session.commit()

    calls: list[int] = []

    def fake_describe(path, **kwargs):
        calls.append(1)
        return _vision_pass(None, error="request failed: 404 model_not_found")

    monkeypatch.setattr(smart_drafts, "describe_video_frames", fake_describe)

    result = draft_generation.ensure_batch_vision_payloads(items)

    assert len(calls) == 1, "only the first reel should pay the failing round-trip"
    assert result["described"] == []
    assert "404" in result["unavailable"]
    assert len(result["skipped"]) == 4


def test_ensure_batch_vision_keeps_going_after_a_transient_failure(monkeypatch, tmp_path) -> None:
    # A rate limit is per-key and transient, so the batch must not give up.
    items = [_make_item() for _ in range(3)]
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    with get_session() as session:
        for item_id in items:
            session.get(DownloadItem, item_id).file_path = str(video)
        session.commit()

    calls: list[int] = []

    def fake_describe(path, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _vision_pass(None, error="429 rate_limit_exceeded")
        return _vision_pass({"main_subject": "a clip"})

    monkeypatch.setattr(smart_drafts, "describe_video_frames", fake_describe)

    result = draft_generation.ensure_batch_vision_payloads(items)

    assert len(calls) == 3
    assert result["described"] == items[1:]
    assert result["unavailable"] is None


def test_ensure_vision_payload_reuses_stored_evidence(monkeypatch) -> None:
    item_id = _make_item()
    with get_session() as session:
        session.get(DownloadItem, item_id).smart_vision_payload = '{"main_subject": "a bridge"}'
        session.commit()

    def fail(*args, **kwargs):
        raise AssertionError("stored evidence must not trigger a vision call")

    monkeypatch.setattr(smart_drafts, "describe_video_frames", fail)

    assert draft_generation.ensure_vision_payload(item_id) == {"main_subject": "a bridge"}
