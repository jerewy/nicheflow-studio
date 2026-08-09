from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from nicheflow_studio.core.ui_prefs import set_ui_pref
from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import draft_revisions as svc

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_cli():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import nicheflow_drafts

    return nicheflow_drafts


def _make_item(*, file_path: str | None = "C:/clips/x.mp4", with_account: bool = True) -> int:
    with get_session() as session:
        account_id = None
        if with_account:
            account = Account(
                name="Movie Account",
                platform="instagram",
                niche_label="movie",
                writing_tone="cinematic",
                target_audience="film fans",
            )
            session.add(account)
            session.commit()
            account_id = account.id
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Source title",
            file_path=file_path,
            status="completed",
            account_id=account_id,
        )
        session.add(item)
        session.commit()
        return item.id


# --------------------------------------------------------------------------- #
# save / list / latest
# --------------------------------------------------------------------------- #


def test_save_revision_increments_revision_number() -> None:
    item_id = _make_item()

    first = svc.save_revision(
        item_id,
        title_options=["A", "B", "C"],
        caption_options=["ca", "cb", "cc"],
        summary="A clip.",
    )
    second = svc.save_revision(
        item_id,
        title_options=["A2", "B2", "C2"],
        caption_options=["ca2", "cb2", "cc2"],
    )

    assert first.revision_number == 1
    assert second.revision_number == 2
    assert svc.latest_revision(item_id).revision_number == 2
    assert [r.revision_number for r in svc.list_revisions(item_id)] == [1, 2]


def test_save_revision_round_trips_all_fields() -> None:
    item_id = _make_item()

    dto = svc.save_revision(
        item_id,
        title_options=["t1", "t2", "t3"],
        caption_options=["c1", "c2", "c3"],
        summary="summary",
        option_notes=["n1", "n2", "n3"],
        option_tiers=["green", "yellow", "yellow"],
        recommended_title_index=1,
        recommended_caption_index=2,
        recommendation_reason="strongest pair",
        title_style_preset="meme_setup_punchline",
        caption_style_preset="contextual_info",
        provider_label="Codex",
        generation_meta={"writer_model": "codex"},
        vision_payload={"main_subject": "actor"},
    )

    assert dto.title_options == ["t1", "t2", "t3"]
    assert dto.option_tiers == ["green", "yellow", "yellow"]
    assert dto.recommended_title_index == 1
    assert dto.generation_meta == {"writer_model": "codex"}
    assert dto.vision_payload == {"main_subject": "actor"}
    assert dto.source == "codex"


def test_save_revision_unknown_item_raises() -> None:
    with pytest.raises(svc.DraftRevisionError):
        svc.save_revision(999, title_options=["a"], caption_options=["b"])


def test_save_revision_requires_non_empty_options() -> None:
    item_id = _make_item()
    with pytest.raises(svc.DraftRevisionError):
        svc.save_revision(item_id, title_options=[], caption_options=["b"])
    with pytest.raises(svc.DraftRevisionError):
        svc.save_revision(item_id, title_options=["a"], caption_options=["   "])


# --------------------------------------------------------------------------- #
# revise
# --------------------------------------------------------------------------- #


def test_revise_option_replaces_one_and_preserves_rest() -> None:
    item_id = _make_item()
    svc.save_revision(
        item_id,
        title_options=["t1", "t2", "t3"],
        caption_options=["c1", "c2", "c3"],
        option_notes=["n1", "n2", "n3"],
        recommendation_reason="keep me",
    )

    revised = svc.revise_option(item_id, 2, title="t2-new", caption="c2-new")

    assert revised.revision_number == 2
    assert revised.title_options == ["t1", "t2-new", "t3"]
    assert revised.caption_options == ["c1", "c2-new", "c3"]
    # Untouched metadata carries forward.
    assert revised.recommendation_reason == "keep me"


def test_revise_option_without_base_raises() -> None:
    item_id = _make_item()
    with pytest.raises(svc.DraftRevisionError):
        svc.revise_option(item_id, 1, title="x")


def test_revise_option_out_of_range_raises() -> None:
    item_id = _make_item()
    svc.save_revision(item_id, title_options=["t1"], caption_options=["c1"])
    with pytest.raises(svc.DraftRevisionError):
        svc.revise_option(item_id, 5, title="x")


def test_revise_option_requires_a_change() -> None:
    item_id = _make_item()
    svc.save_revision(item_id, title_options=["t1"], caption_options=["c1"])
    with pytest.raises(svc.DraftRevisionError):
        svc.revise_option(item_id, 1)


# --------------------------------------------------------------------------- #
# apply (the only op that touches the live item)
# --------------------------------------------------------------------------- #


def test_apply_revision_sets_final_draft_and_mirrors_smart_fields() -> None:
    item_id = _make_item()
    svc.save_revision(
        item_id,
        title_options=["Title one", "Title two", "Title three"],
        caption_options=["Cap one", "Cap two", "Cap three"],
        summary="A clip about something.",
        provider_label="Codex",
        title_style_preset="meme_setup_punchline",
    )

    result = svc.apply_revision(item_id, 2)

    assert result["applied_option"] == 2
    assert result["title_draft"] == "Title two"

    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        assert item.title_draft == "Title two"
        assert item.caption_draft == "Cap two"
        # Mirrored so the existing export/UI path sees the same content.
        assert item.smart_summary == "A clip about something."
        assert json.loads(item.smart_title_options) == [
            "Title one",
            "Title two",
            "Title three",
        ]
        assert item.smart_provider_label == "Codex"
        assert item.smart_generated_at is not None
        assert item.title_style_preset == "meme_setup_punchline"

    applied = svc.latest_revision(item_id)
    assert applied.applied_at is not None
    assert applied.applied_title_index == 2


def test_apply_revision_out_of_range_raises() -> None:
    item_id = _make_item()
    svc.save_revision(item_id, title_options=["only"], caption_options=["c"])
    with pytest.raises(svc.DraftRevisionError):
        svc.apply_revision(item_id, 3)


# --------------------------------------------------------------------------- #
# active item resolution / context
# --------------------------------------------------------------------------- #


def test_resolve_active_item_prefers_explicit_then_pref_then_latest_file() -> None:
    older = _make_item(file_path="C:/clips/old.mp4")
    newer = _make_item(file_path="C:/clips/new.mp4")
    _make_item(file_path=None)  # no file -> ignored by fallback

    # Fallback = most recent item that has a local file.
    assert svc.resolve_active_item_id() == newer

    # Pref wins over fallback when valid.
    set_ui_pref(svc.ACTIVE_PROCESSING_ITEM_PREF_KEY, older)
    assert svc.resolve_active_item_id() == older

    # Explicit wins over everything.
    assert svc.resolve_active_item_id(newer) == newer


def test_active_context_includes_item_account_and_latest_revision() -> None:
    item_id = _make_item()
    svc.save_revision(item_id, title_options=["t1"], caption_options=["c1"])

    context = svc.active_context(item_id)

    assert context["item"]["id"] == item_id
    assert context["account"]["niche_label"] == "movie"
    assert context["account"]["writing_tone"] == "cinematic"
    assert context["latest_revision"]["title_options"] == ["t1"]
    assert context["revision_count"] == 1


def test_active_context_no_item_raises() -> None:
    with pytest.raises(svc.DraftRevisionError):
        svc.active_context(None)


# --------------------------------------------------------------------------- #
# CLI adapter
# --------------------------------------------------------------------------- #


def test_cli_save_apply_history_roundtrip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _load_cli()
    item_id = _make_item()

    payload = {
        "title_options": ["CLI t1", "CLI t2", "CLI t3"],
        "caption_options": ["CLI c1", "CLI c2", "CLI c3"],
        "summary": "from cli",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["save", "--item-id", str(item_id), "--stdin"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["revision_number"] == 1
    assert saved["title_options"] == ["CLI t1", "CLI t2", "CLI t3"]

    assert cli.main(["apply", "--item-id", str(item_id), "--option", "3"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["title_draft"] == "CLI t3"

    assert cli.main(["history", "--item-id", str(item_id)]) == 0
    history = json.loads(capsys.readouterr().out)
    assert len(history) == 1
    assert history[0]["applied_title_index"] == 3


def test_cli_current_outputs_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _load_cli()
    item_id = _make_item(file_path="C:/clips/current.mp4")

    assert cli.main(["current"]) == 0
    context = json.loads(capsys.readouterr().out)
    assert context["item"]["id"] == item_id


def test_cli_save_invalid_json_returns_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _load_cli()
    item_id = _make_item()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))

    assert cli.main(["save", "--item-id", str(item_id), "--stdin"]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_save_reads_utf8_file_without_pipe_corruption(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --file must read the JSON directly as UTF-8, bypassing the PowerShell
    # Get-Content pipe that corrupts em dashes and emoji. The em dash itself
    # then gets normalized away by save_revision's dash rule.
    cli = _load_cli()
    item_id = _make_item()
    payload = {
        "title_options": ["Café title 🎬"],
        "caption_options": ["A 1974–1980 era caption — still sharp."],
    }
    draft_file = tmp_path / "draft.json"
    draft_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["save", "--item-id", str(item_id), "--file", str(draft_file)]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["title_options"] == ["Café title 🎬"]
    assert saved["caption_options"] == ["A 1974-1980 era caption, still sharp."]


def test_cli_save_missing_file_returns_error(capsys: pytest.CaptureFixture[str]) -> None:
    cli = _load_cli()
    item_id = _make_item()

    assert cli.main(["save", "--item-id", str(item_id), "--file", "Z:/nope/missing.json"]) == 1
    assert "error:" in capsys.readouterr().err


def test_save_revision_normalizes_dashes_in_published_copy() -> None:
    # Em/en dashes and '--' read as AI-generated copy; every intake path
    # funnels through save_revision, so they must come out normalized.
    item_id = _make_item()

    saved = svc.save_revision(
        item_id,
        title_options=["Fosse danced -- then the world copied", "Plain title"],
        caption_options=["He was precise — almost mechanical.\n\nThe 1974–1980 years proved it."],
    )

    assert saved.title_options[0] == "Fosse danced, then the world copied"
    assert saved.title_options[1] == "Plain title"
    assert saved.caption_options[0] == (
        "He was precise, almost mechanical.\n\nThe 1974-1980 years proved it."
    )


def test_save_revision_coerces_dict_notes_and_path_source() -> None:
    # Agents saving via the CLI have sent option_notes as an object (storing
    # its KEYS as junk notes) and a video file path in the source field.
    item_id = _make_item()

    saved = svc.save_revision(
        item_id,
        title_options=["T1", "T2"],
        caption_options=["C1", "C2"],
        option_notes={"option_1": "clearest hook", "option_2": "best for reach"},  # type: ignore[arg-type]
        source="C:\\clips\\video.mp4",
    )

    assert saved.option_notes == ["clearest hook", "best for reach"]
    assert saved.source == "codex"


def test_save_revision_keeps_hyphenated_words_intact() -> None:
    item_id = _make_item()

    saved = svc.save_revision(
        item_id,
        title_options=["A well-known never-before-told story"],
        caption_options=["Self-taught, one-take performance."],
    )

    assert saved.title_options == ["A well-known never-before-told story"]
    assert saved.caption_options == ["Self-taught, one-take performance."]


def test_save_revision_fans_a_single_caption_across_titles() -> None:
    # The chat handoff returns three titles and ONE shared caption. Apply
    # indexes titles and captions with the same option number, so an unpadded
    # caption list would apply an empty caption for options 2 and 3.
    item_id = _make_item()

    saved = svc.save_revision(
        item_id,
        title_options=["First hook", "Second hook", "Third hook"],
        caption_options=["One caption for all three."],
        recommended_title_index=3,
        recommended_caption_index=3,
    )

    assert saved.caption_options == ["One caption for all three."] * 3

    applied = svc.apply_revision(item_id, 3)
    assert applied["title_draft"] == "Third hook"
    assert applied["caption_draft"] == "One caption for all three."
