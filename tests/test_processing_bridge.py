from __future__ import annotations

from pathlib import Path
import threading

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
    assert "preview_url" in result["data"]["item"]
    assert "original_preview_url" in result["data"]["item"]
    assert "exported_preview_url" in result["data"]["item"]


def test_bridge_get_context_waits_for_media_mapping(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    ready = threading.Event()
    bridge = ProcessingBridge(ready)

    result = bridge.get_context(item_id)

    assert result["ok"] is True
    assert result["data"]["item"]["preview_url"] is None


def test_bridge_get_context_unknown_item_returns_error_envelope() -> None:
    bridge = ProcessingBridge()

    result = bridge.get_context(99999)

    assert result["ok"] is False
    assert "99999" in result["error"]


def test_bridge_list_pool_source_clips_passes_include_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, bool]] = []

    def list_source_clips(
        niche: str, source_label: str, include_removed: bool = False
    ) -> list[dict]:
        calls.append((niche, source_label, include_removed))
        return []

    monkeypatch.setattr(
        "nicheflow_studio.app.processing_bridge.pooling.list_source_clips",
        list_source_clips,
    )

    result = ProcessingBridge().list_pool_source_clips("history", "theanomalists", True)

    assert result["ok"] is True
    assert result["data"] == []
    assert calls == [("history", "theanomalists", True)]


def test_bridge_get_crop_preview_returns_media_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preview = tmp_path / "data" / "processed" / "crop-previews" / "item-1.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"jpeg")
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "nicheflow_studio.app.processing_bridge.export_svc.crop_preview_frame",
        lambda _item_id: preview,
    )

    result = ProcessingBridge().get_crop_preview(1)

    assert result["ok"] is True
    assert result["data"]["preview_url"].endswith("/processed/crop-previews/item-1.jpg")


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
    # A generation that raises inside the worker is caught and reported as a
    # failed job (the JobManager error path), not surfaced as an exception.
    item_id = _make_item(transcript="A transcript with enough grounding to generate.")
    bridge = ProcessingBridge()

    def boom(**_):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(smart_drafts, "generate_smart_drafts", boom)

    started = bridge.start_generation(item_id, {})
    job_id = started["data"]["job_id"]
    bridge._jobs.join(job_id)

    job = bridge.get_job(job_id)
    assert job["data"]["status"] == "failed"
    assert "provider exploded" in job["data"]["error"]


def test_bridge_get_unknown_job_returns_error() -> None:
    bridge = ProcessingBridge()
    result = bridge.get_job("nope")
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# account manager
# --------------------------------------------------------------------------- #


def test_bridge_account_crud_roundtrip() -> None:
    bridge = ProcessingBridge()

    created = bridge.create_account({"name": "Bridge Acc", "instagram_handle": "@bridge"})
    assert created["ok"] is True
    account_id = created["data"]["id"]
    assert created["data"]["instagram_handle"] == "bridge"

    listed = bridge.list_accounts()
    assert any(a["id"] == account_id for a in listed["data"])

    updated = bridge.update_account(account_id, {"writing_tone": "playful"})
    assert updated["data"]["writing_tone"] == "playful"

    deleted = bridge.delete_account(account_id)
    assert deleted["ok"] is True
    assert deleted["data"]["deleted_account_id"] == account_id


def test_bridge_create_account_requires_name() -> None:
    bridge = ProcessingBridge()
    result = bridge.create_account({"platform": "instagram"})
    assert result["ok"] is False
    assert "name" in result["error"].lower()


def test_bridge_start_export_runs_job(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from nicheflow_studio.processing import video

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    bridge = ProcessingBridge()

    monkeypatch.setattr(video, "probe_video", lambda _p: object())
    monkeypatch.setattr(
        video, "suggest_title_replacement_crop", lambda _p, _probe: video.CropSettings()
    )
    monkeypatch.setattr(video, "export_cropped_video", lambda **kw: kw["output_path"])

    started = bridge.start_export(item_id)
    assert started["ok"] is True
    bridge._jobs.join(started["data"]["job_id"])
    job = bridge.get_job(started["data"]["job_id"])

    assert job["data"]["status"] == "succeeded"
    assert job["data"]["progress"] == 1.0
    assert job["data"]["result"]["processed_path"]


def test_bridge_export_failure_surfaces_message() -> None:
    # No file_path -> ExportError -> handled job failure with a real message.
    item_id = _make_item(file_path=None)
    bridge = ProcessingBridge()

    started = bridge.start_export(item_id)
    bridge._jobs.join(started["data"]["job_id"])
    job = bridge.get_job(started["data"]["job_id"])

    assert job["data"]["status"] == "failed"
    assert "video file" in job["data"]["error"].lower()


# --------------------------------------------------------------------------- #
# items + publish queue
# --------------------------------------------------------------------------- #


def test_bridge_list_items_and_queue_for_publish() -> None:
    item_id = _make_item()
    bridge = ProcessingBridge()
    # Mark the item as exported so it can be queued.
    with get_session() as session:
        session.get(DownloadItem, item_id).processed_path = "C:/processed/out.mp4"
        session.commit()

    listed = bridge.list_items()
    assert listed["ok"] is True
    assert any(it["id"] == item_id for it in listed["data"])

    queued = bridge.queue_for_publish(item_id)
    assert queued["ok"] is True
    assert queued["data"]["status"] == "draft"

    jobs = bridge.list_publish_jobs(item_id)
    assert jobs["ok"] is True
    assert len(jobs["data"]) == 1


def test_bridge_queue_without_export_returns_error() -> None:
    item_id = _make_item()  # no processed_path
    bridge = ProcessingBridge()

    result = bridge.queue_for_publish(item_id)

    assert result["ok"] is False
    assert "export" in result["error"].lower()


def test_bridge_auto_schedule_for_publish() -> None:
    item_id = _make_item()
    bridge = ProcessingBridge()
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        item.processed_path = "C:/processed/out.mp4"
        account = session.get(Account, item.account_id)
        account.upload_schedule_slots = "09:00, 18:00"
        session.commit()

    result = bridge.auto_schedule_for_publish(item_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "scheduled"
    assert result["data"]["scheduled_at"] is not None


def test_bridge_start_queue_for_publish_runs_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = ProcessingBridge()
    calls: list[tuple[int, str | None]] = []
    running = threading.Event()
    release = threading.Event()

    def fake_queue(item_id: int, *, scheduled_at: str | None = None) -> dict:
        calls.append((item_id, scheduled_at))
        running.set()
        assert release.wait(2)
        return {"job_id": 7, "status": "cloud", "scheduled_at": scheduled_at, "created": False}

    monkeypatch.setattr("nicheflow_studio.services.publishing.queue_for_publish", fake_queue)

    started = bridge.start_queue_for_publish(12, "2026-06-15T04:30:00+00:00")
    assert started["ok"] is True
    assert running.wait(1)
    assert bridge.get_job(started["data"]["job_id"])["data"]["status"] == "running"
    release.set()
    snapshot = bridge._jobs.join(started["data"]["job_id"])

    assert snapshot is not None
    assert snapshot["status"] == "succeeded"
    assert snapshot["result"]["status"] == "cloud"
    assert calls == [(12, "2026-06-15T04:30:00+00:00")]


def test_bridge_start_auto_schedule_for_publish_runs_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = ProcessingBridge()
    calls: list[int] = []

    def fake_auto_schedule(item_id: int) -> dict:
        calls.append(item_id)
        return {
            "job_id": 8,
            "status": "cloud",
            "scheduled_at": "2026-06-15T04:30:00+00:00",
            "created": False,
        }

    monkeypatch.setattr(
        "nicheflow_studio.services.publishing.auto_schedule_for_publish",
        fake_auto_schedule,
    )

    started = bridge.start_auto_schedule_for_publish(12)
    snapshot = bridge._jobs.join(started["data"]["job_id"])

    assert snapshot is not None
    assert snapshot["status"] == "succeeded"
    assert snapshot["result"]["status"] == "cloud"
    assert calls == [12]


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
