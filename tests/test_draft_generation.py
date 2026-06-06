from __future__ import annotations

import pytest

from nicheflow_studio.db.models import Account, DownloadItem
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


def test_generate_without_transcript_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    item_id = _make_item(transcript=None)
    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", lambda **_: _fake_drafts())

    with pytest.raises(DraftRevisionError):
        draft_generation.generate_revision_for_item(item_id)


def test_generate_unknown_item_raises() -> None:
    with pytest.raises(DraftRevisionError):
        draft_generation.generate_revision_for_item(99999)
