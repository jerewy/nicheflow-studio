from __future__ import annotations

from pathlib import Path

import pytest

from nicheflow_studio.app import webview_app
from nicheflow_studio.app.processing_bridge import ProcessingBridge
from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import smart_drafts
from nicheflow_studio.services import draft_revisions as svc


def _make_item(*, file_path: str | None = "C:/clips/x.mp4", transcript: str | None = None) -> int:
    with get_session() as session:
        account = Account(name="Acc", platform="instagram", niche_label="movie")
        session.add(account)
        session.commit()
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Source",
            file_path=file_path,
            transcript_text=transcript,
            status="completed",
            account_id=account.id,
        )
        session.add(item)
        session.commit()
        return item.id


# --------------------------------------------------------------------------- #
# envelope contract
# --------------------------------------------------------------------------- #


def test_bridge_get_context_ok_envelope() -> None:
    item_id = _make_item()
    bridge = ProcessingBridge()

    result = bridge.get_context(item_id)

    assert result["ok"] is True
    assert result["data"]["item"]["id"] == item_id


def test_bridge_get_context_unknown_item_returns_error_envelope() -> None:
    bridge = ProcessingBridge()

    result = bridge.get_context(99999)

    assert result["ok"] is False
    assert "99999" in result["error"]


def test_bridge_save_then_get_latest_revision() -> None:
    item_id = _make_item()
    bridge = ProcessingBridge()

    saved = bridge.save_revision(
        item_id,
        {"title_options": ["t1", "t2"], "caption_options": ["c1", "c2"]},
    )
    assert saved["ok"] is True
    assert saved["data"]["revision_number"] == 1
    # Bridge-originated revisions are tagged "ui".
    assert saved["data"]["source"] == "ui"

    latest = bridge.get_latest_revision(item_id)
    assert latest["ok"] is True
    assert latest["data"]["title_options"] == ["t1", "t2"]


def test_bridge_get_latest_revision_none_when_empty() -> None:
    item_id = _make_item()
    bridge = ProcessingBridge()

    latest = bridge.get_latest_revision(item_id)

    assert latest["ok"] is True
    assert latest["data"] is None


def test_bridge_save_invalid_returns_error_envelope() -> None:
    item_id = _make_item()
    bridge = ProcessingBridge()

    result = bridge.save_revision(item_id, {"title_options": [], "caption_options": ["c"]})

    assert result["ok"] is False
    assert result["error"]


def test_bridge_revise_and_apply() -> None:
    item_id = _make_item()
    bridge = ProcessingBridge()
    bridge.save_revision(
        item_id,
        {"title_options": ["t1", "t2", "t3"], "caption_options": ["c1", "c2", "c3"]},
    )

    revised = bridge.revise_option(item_id, 2, {"title": "t2-new"})
    assert revised["ok"] is True
    assert revised["data"]["title_options"][1] == "t2-new"

    applied = bridge.apply_revision(item_id, 2)
    assert applied["ok"] is True
    assert applied["data"]["title_draft"] == "t2-new"

    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        assert item.title_draft == "t2-new"


def test_bridge_set_active_item_updates_pref() -> None:
    older = _make_item()
    newer = _make_item()
    bridge = ProcessingBridge()

    # Fallback would pick the newest; setting the pref overrides that.
    result = bridge.set_active_item(older)
    assert result["ok"] is True
    assert result["data"]["active_processing_item_id"] == older
    assert svc.resolve_active_item_id() == older
    assert newer != older  # sanity: two distinct items exist


# --------------------------------------------------------------------------- #
# background generation job
# --------------------------------------------------------------------------- #


def test_bridge_start_generation_runs_job_and_saves_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = _make_item(transcript="A transcript with enough grounding to generate.")
    bridge = ProcessingBridge()

    monkeypatch.setattr(
        smart_drafts,
        "generate_smart_drafts",
        lambda **_: smart_drafts.SmartDrafts(
            summary="s",
            title_options=["t1", "t2", "t3"],
            caption_options=["c1", "c2", "c3"],
            provider_label="Groq (test)",
        ),
    )

    started = bridge.start_generation(item_id, {"caption_style": "contextual_info"})
    assert started["ok"] is True
    job_id = started["data"]["job_id"]

    # Wait for the daemon thread, then read the terminal status via the bridge.
    bridge._jobs.join(job_id)
    job = bridge.get_job(job_id)
    assert job["ok"] is True
    assert job["data"]["status"] == "succeeded"
    assert job["data"]["result"]["title_options"] == ["t1", "t2", "t3"]

    # The saved revision is now the latest for the item.
    latest = bridge.get_latest_revision(item_id)
    assert latest["data"]["title_options"] == ["t1", "t2", "t3"]


def test_bridge_generation_job_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # No transcript -> generate_revision_for_item raises -> job fails (handled).
    item_id = _make_item(transcript=None)
    bridge = ProcessingBridge()

    started = bridge.start_generation(item_id, {})
    job_id = started["data"]["job_id"]
    bridge._jobs.join(job_id)

    job = bridge.get_job(job_id)
    assert job["data"]["status"] == "failed"
    assert "transcript" in job["data"]["error"].lower()


def test_bridge_get_unknown_job_returns_error() -> None:
    bridge = ProcessingBridge()
    result = bridge.get_job("nope")
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# launcher entry resolution
# --------------------------------------------------------------------------- #


def test_resolve_entry_prefers_env_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NICHEFLOW_WEBVIEW_URL", "http://localhost:5173")
    assert webview_app.resolve_entry() == "http://localhost:5173"


def test_resolve_entry_missing_build_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NICHEFLOW_WEBVIEW_URL", raising=False)
    monkeypatch.setattr(webview_app, "_frontend_dist_index", lambda: tmp_path / "nope.html")
    with pytest.raises(FileNotFoundError):
        webview_app.resolve_entry()
