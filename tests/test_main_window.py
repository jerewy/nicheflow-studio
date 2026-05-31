from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QSizePolicy, QTextEdit

from nicheflow_studio.app.main_window import (
    InstagramDiscoverRankJobConfig,
    InstagramDiscoverRankWorker,
    MainWindow,
    ProcessJobConfig,
    ProcessWorker,
    SuggestCropJobConfig,
    parse_pasted_smart_draft,
)
from nicheflow_studio.core.paths import downloads_dir, processed_dir
from nicheflow_studio.processing.video import CropSettings, PreprocessingOcrDiagnostics, VideoProbe
from nicheflow_studio.db.models import (
    Account,
    DownloadItem,
    ScrapeCandidate,
    ScrapeRun,
    Source,
    UploadJob,
)
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.scraper.youtube import DiscoveryWeights, ScrapedVideoCandidate


@pytest.fixture(autouse=True)
def _disable_smart_draft_providers_by_default(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_DISABLED", "1")


def _run_scrape_job_immediately(window: MainWindow, job) -> None:  # noqa: ANN001
    window._scrape_in_progress = True
    window._refresh_candidate_action_state()
    total_created = 0
    total_refreshed = 0
    total_skipped = 0
    total_rejected = 0
    sources = [
        source
        for source in window._load_sources_for_account(job.account_id)
        if source.id in job.source_ids
    ]
    for source in sources:
        (
            created_count,
            refreshed_count,
            skipped_count,
            rejected_count,
        ) = window._run_scrape_for_source(
            account_id=job.account_id,
            source=source,
            keywords=job.keywords,
            max_items=job.max_items,
            max_age_days=job.max_age_days,
            min_view_count=job.min_view_count,
            min_like_count=job.min_like_count,
            weights=job.weights,
            archive_backfill=job.archive_backfill,
        )
        total_created += created_count
        total_refreshed += refreshed_count
        total_skipped += skipped_count
        total_rejected += rejected_count
    auto_queued = 0
    if job.discovery_mode == "auto_queue" and job.auto_queue_limit > 0:
        auto_queued = window._auto_queue_top_candidates(
            account_id=job.account_id,
            limit=job.auto_queue_limit,
        )
    window._on_scrape_completed(
        {
            "sources": len(sources),
            "created": total_created,
            "refreshed": total_refreshed,
            "skipped": total_skipped,
            "rejected": total_rejected,
            "auto_queued": auto_queued,
        }
    )


def _complete_processing_job_immediately(window: MainWindow, job) -> None:  # noqa: ANN001
    window._processing_in_progress = True
    window._on_processing_completed({"output_path": str(job.output_path)})


def _complete_suggest_job_immediately(window: MainWindow, job) -> None:  # noqa: ANN001
    window._processing_in_progress = True
    window._on_suggest_crop_completed(
        {
            "crop": CropSettings(left=18, top=24, right=12, bottom=96),
            "reasons": [
                "removed detected border margins",
                "trimmed repeated OCR text near the frame edges",
            ],
            "used_border_detection": True,
            "used_ocr": True,
        }
    )


def test_processing_ocr_summary_reports_detected_regions() -> None:
    diagnostics = PreprocessingOcrDiagnostics(
        top_text_detected=True,
        bottom_text_detected=True,
        top_snippets=("Top Hook",),
        bottom_snippets=("Subscribe Now",),
        average_confidence=83.3,
        sample_count=3,
        ffmpeg_available=True,
        tesseract_available=True,
        region_signals=(),
        debug_messages=(),
    )

    summary = MainWindow._format_preprocessing_ocr_summary(diagnostics)

    assert "top and bottom text" in summary
    assert "83.3%" in summary
    assert "Top Hook" in summary
    assert "Subscribe Now" in summary


def _complete_draft_job_immediately(window: MainWindow, job) -> None:  # noqa: ANN001
    window._processing_in_progress = True
    window._on_transcript_draft_completed(
        {
            "transcript_text": "This is a generated transcript. It has two sentences.",
            "title_draft": "Generated title draft",
            "caption_draft": "This is a generated caption draft.",
        }
    )


def _complete_smart_draft_job_immediately(window: MainWindow, job) -> None:  # noqa: ANN001
    window._processing_in_progress = True
    window._on_smart_draft_completed(
        {
            "summary": "A funny zoo moment with a clear elephant hook.",
            "title_options": ["Elephant Chaos", "Zoo Hook", "Watch The Elephant"],
            "caption_options": [
                "This elephant stole the whole clip",
                "Wait for the elephant reveal",
            ],
            "recommended_title_index": 1,
            "recommended_caption_index": 1,
            "recommendation_reason": "Best fit because it names the reveal clearly.",
            "option_notes": [
                "Fastest direct hook.",
                "Best overall for reach.",
                "Backup broader hook.",
            ],
            "provider_label": "Groq Scout + Llama 3.3",
            "used_fallback": False,
            "vision_payload": {
                "scene_summary": "An elephant moves into frame and steals the moment.",
                "ocr_text": [],
                "main_subject": "elephant",
                "main_action": "steals the moment",
                "tone": "funny",
                "confidence": "high",
                "hook_moments": ["elephant reveal"],
                "uncertainty_notes": "",
            },
            "generation_meta": {
                "writer_model": "llama-3.3-70b-versatile",
                "vision_model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "frame_count": 3,
                "recommended_title_option_index": 1,
                "recommended_caption_option_index": 1,
                "recommendation_reason": "Best fit because it names the reveal clearly.",
                "option_notes": [
                    "Fastest direct hook.",
                    "Best overall for reach.",
                    "Backup broader hook.",
                ],
            },
        }
    )


def _fail_draft_job_immediately(window: MainWindow, job) -> None:  # noqa: ANN001
    window._processing_in_progress = True
    window._on_transcript_draft_failed("No speech was detected in this video.")


def test_instagram_profile_username_accepts_username_handle_and_profile_url() -> None:
    assert MainWindow._instagram_profile_username("meme.ig") == "meme.ig"
    assert MainWindow._instagram_profile_username("@meme.ig") == "meme.ig"
    assert (
        MainWindow._instagram_profile_username("https://www.instagram.com/meme.ig/")
        == "meme.ig"
    )
    assert MainWindow._instagram_profile_username("https://www.instagram.com/reel/DYdxGRpO7Am/") == ""


def test_processing_loading_badge_tracks_generation_state(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._set_current_page("processing")
        qt_app.processEvents()

        window._start_processing_loading_state("Generating drafts")

        assert window._processing_loading_badge.isHidden() is False
        assert window._processing_generate_drafts_button.text() == "Generating..."
        assert "Generating drafts" in window._processing_loading_badge.text()

        window._stop_processing_loading_state()

        assert window._processing_loading_badge.isHidden() is True
        assert window._processing_generate_drafts_button.text() == "Generate"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_failed_item_keeps_error_in_row_without_global_false_alarm(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=broken",
                status="failed",
                account_id=account.id,
                error_message="yt-dlp could not fetch metadata",
            )
        )
        session.commit()
        account_id = account.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._table.rowCount() == 1
        assert window._table.item(0, 5).text() == "yt-dlp could not fetch metadata"
        assert window._status_label.text() == "Showing 1 item for YT Main."
        assert window._detail_panel.isVisible() is False

        window._table.selectRow(0)
        qt_app.processEvents()

        assert window._detail_panel.isVisible() is False
        assert window._detail_fields["status"].text() == "failed"
        assert window._detail_fields["review"].text() == "Needs Review"
        assert window._detail_fields["account"].text() == "YT Main"
        assert window._detail_fields["extractor"].text() == "(unknown)"
        assert window._detail_fields["video_id"].text() == "(unknown)"
        assert window._detail_fields["error"].text() == "yt-dlp could not fetch metadata"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_downloaded_item_shows_extractor_and_video_id_in_detail_panel(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=meta123",
                extractor="youtube",
                video_id="meta123",
                title="Meta clip",
                status="downloaded",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._table.selectRow(0)
        qt_app.processEvents()

        assert window._detail_fields["extractor"].text() == "youtube"
        assert window._detail_fields["video_id"].text() == "meta123"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_source_intake_persists_candidates_for_selected_account(monkeypatch, qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(
            name="YT Main",
            platform="youtube",
            scrape_max_items=5,
            scrape_max_age_days=30,
            discovery_keywords="funny gaming",
        )
        session.add(account)
        session.flush()
        session.add(
            Source(
                account_id=account.id,
                platform="youtube",
                source_type="youtube_profile",
                label="@clips",
                source_url="https://www.youtube.com/@clips",
                enabled=1,
                priority=100,
            )
        )
        session.commit()

    def fake_scrape(*, source_url: str, max_items: int, max_age_days: int | None):
        assert source_url == "https://www.youtube.com/@clips"
        assert max_items == 5
        assert max_age_days == 30
        return [
            ScrapedVideoCandidate(
                scrape_source_url=source_url,
                source_url="https://www.youtube.com/watch?v=intake123",
                extractor="youtube",
                video_id="intake123",
                title="Intake clip",
                channel_name="Clips Channel",
                published_at=None,
            )
        ]

    monkeypatch.setattr("nicheflow_studio.app.main_window.scrape_youtube_source", fake_scrape)
    monkeypatch.setattr(
        MainWindow,
        "_start_scrape_job",
        _run_scrape_job_immediately,
    )
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._on_scrape_clicked()
        qt_app.processEvents()

        assert window._candidate_table.rowCount() == 1
        assert window._candidate_table.item(0, 0).text() == "ready"
        assert window._candidate_table.item(0, 8).text() == "Intake clip"
        assert window._scrape_summary_label.text().startswith(
            "1 of 1 source(s) enabled, 1 keyword(s)"
        )
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_workspace_can_add_scrape_source_to_selected_account(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._scrape_source_input.setText("https://www.youtube.com/@clips")
        window._on_add_scrape_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(Account).filter(Account.name == "YT Main").one()

        assert saved.scrape_source_urls == "https://www.youtube.com/@clips"
        assert window._status_label.text() == "Added source to the current account."
        assert window._scrape_source_input.text() == ""
        assert window._source_table.rowCount() == 1
        assert window._selected_source_id is not None
        assert window._source_table.selectionModel().hasSelection() is True
        assert window._scrape_selected_button.text() == "Scrape Selected"
        assert window._scrape_selected_button.isEnabled() is True
        assert "Scrape Selected" in window._candidate_action_hint.text()
        assert window._scrape_summary_label.text().startswith(
            "1 of 1 source(s) enabled, 0 keyword(s)"
        )
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_workspace_normalizes_scrape_source_subpage_to_root(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._scrape_source_input.setText("https://www.youtube.com/@clips/shorts")
        window._on_add_scrape_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(Account).filter(Account.name == "YT Main").one()

        assert saved.scrape_source_urls == "https://www.youtube.com/@clips"
        assert (
            window._status_label.text()
            == "Added source and normalized it to the channel/profile root URL."
        )
        assert window._source_table.rowCount() == 1
        assert window._source_table.item(0, 3).text() == "https://www.youtube.com/@clips"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_source_remove_uses_highlighted_row_when_widget_cell_has_focus(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@whezery",
            source_url="https://www.instagram.com/whezery/",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._selected_source_id = None
        window._source_table.setCurrentCell(0, 0)
        qt_app.processEvents()
        window._refresh_candidate_action_state()

        assert window._source_remove_button.isEnabled() is True

        window._on_remove_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            assert session.query(Source).count() == 0

        assert window._source_table.rowCount() == 0
        assert window._status_label.text() == "Removed source."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_lists_downloaded_videos_and_updates_output_resolution(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=proc123",
                title="Processing Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1920, height=1080, duration_seconds=12.5),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert window._processing_item_combo.count() == 2
        assert window._processing_preview_path == video_path.resolve()
        assert "Processing Clip" in window._processing_preview_meta_label.text()
        assert "1920 x 1080" in window._processing_preview_meta_label.text()
        assert "Crop output: 1920 x 1080" in window._processing_preview_meta_label.text()
        assert window._processing_export_button.isEnabled() is True
        assert "auto-crop" in window._processing_summary_label.text().lower()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_can_export_cropped_video(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=proc456",
                title="Export Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=10.0),
    )

    captured: dict[str, object] = {}

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured["input_path"] = job.input_path
        captured["output_path"] = job.output_path
        captured["crop"] = job.crop
        captured["title_text"] = job.title_text
        captured["title_font_size"] = job.title_font_size
        captured["title_layout"] = job.title_layout
        captured["watermark_replacement_text"] = job.watermark_replacement_text
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        job.output_path.write_bytes(b"processed")
        _complete_processing_job_immediately(window, job)

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_title_draft_input.setText("Hook Title")
        window._on_process_video_clicked()
        qt_app.processEvents()

        assert captured["input_path"] == video_path
        assert Path(captured["output_path"]).name == "reel_001.mp4"
        assert Path(captured["output_path"]).suffix == ".mp4"
        assert captured["crop"].top == 24
        assert captured["crop"].bottom == 96
        assert captured["title_text"] == "Hook Title"
        assert captured["title_font_size"] == window._processing_title_font_size.value()
        assert captured["title_layout"] == "top_band"
        assert window._processing_progress_label.text() == "Processing complete."
        assert window._status_label.text() == "Processed video saved to reel_001.mp4."
        assert window._status_label.text().endswith(".mp4.")
        assert window._processing_latest_output_label.text() == "reel_001.mp4"
        assert window._processing_latest_output_label.text().endswith(".mp4")
        assert window._processing_open_latest_output_button.isEnabled() is True
        assert window._processing_open_latest_output_button.isVisible() is True
        assert window._processing_open_latest_output_button.text() == "Open Video"
        assert window._processing_preview_mode_combo.findData("output") >= 0
        assert captured.get("watermark_replacement_text") is None
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_instagram_account_passes_watermark_handle(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(
            name="Memeists Daily",
            platform="instagram",
            login_identifier="@memeistsdaily",
        )
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://instagram.com/reel/test/",
                title="Export Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=720, height=1280, duration_seconds=10.0),
    )

    captured: dict[str, object] = {}

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured["watermark_replacement_text"] = job.watermark_replacement_text
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        job.output_path.write_bytes(b"processed")
        _complete_processing_job_immediately(window, job)

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._on_process_video_clicked()
        qt_app.processEvents()

        assert captured["watermark_replacement_text"] == "@memeistsdaily"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_completion_mentions_replaced_watermark(qt_app, tmp_path: Path) -> None:
    init_db()
    output_path = tmp_path / "clip_cropped.mp4"
    output_path.write_bytes(b"processed")

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._processing_in_progress = True

        window._on_processing_completed(
            {
                "output_path": str(output_path),
                "watermark_replaced": True,
                "watermark_detected_text": "@comedyslam",
                "watermark_replacement_text": "@memeistsdaily",
            }
        )

        assert (
            window._processing_progress_label.text()
            == "Processing complete. Replaced @comedyslam with @memeistsdaily."
        )
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_process_worker_replaces_detected_watermark(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"input")

    def fake_export_cropped_video(**kwargs):  # noqa: ANN003
        output = kwargs["output_path"]
        output.write_bytes(b"exported")
        return output

    class FakeReplacement:
        output_path = tmp_path / "output_watermark.mp4"
        skipped_reason = None
        replacement_text = "@memeistsdaily"

        class region:
            text = "@comedyslam"

    def fake_replace_detected_watermark(path, *, replacement_text, output_path, sample_count=3):  # noqa: ANN001
        assert path == output_path.parent / "output.mp4"
        assert replacement_text == "@memeistsdaily"
        output_path.write_bytes(b"watermarked")
        replacement = FakeReplacement()
        replacement.output_path = output_path
        return replacement

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.export_cropped_video",
        fake_export_cropped_video,
    )
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.replace_detected_watermark",
        fake_replace_detected_watermark,
    )
    worker = ProcessWorker(
        ProcessJobConfig(
            input_path=input_path,
            output_path=output_path,
            crop=CropSettings(),
            watermark_replacement_text="@memeistsdaily",
        )
    )
    completed: list[dict] = []
    worker.completed.connect(completed.append)

    worker.run()

    assert output_path.read_bytes() == b"watermarked"
    assert completed[0]["watermark_replaced"] is True
    assert completed[0]["watermark_detected_text"] == "@comedyslam"


def test_process_worker_forwards_audio_mode(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"input")

    captured: dict = {}

    def fake_export_cropped_video(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        output = kwargs["output_path"]
        output.write_bytes(b"exported")
        return output

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.export_cropped_video",
        fake_export_cropped_video,
    )

    worker = ProcessWorker(
        ProcessJobConfig(
            input_path=input_path,
            output_path=output_path,
            crop=CropSettings(),
            audio_mode="alter",
        )
    )
    completed: list[dict] = []
    worker.completed.connect(completed.append)

    worker.run()

    assert captured["audio_mode"] == "alter"
    assert completed and completed[0]["output_path"] == str(output_path)


def test_processing_page_can_switch_preview_to_processed_output(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=proc999",
                title="Preview Output Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=10.0),
    )

    loaded_paths: list[Path] = []

    def fake_load_processing_preview(window: MainWindow, path: Path) -> None:  # noqa: ANN001
        loaded_paths.append(path)
        window._processing_preview_path = path

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        job.output_path.write_bytes(b"processed")
        _complete_processing_job_immediately(window, job)

    monkeypatch.setattr(MainWindow, "_load_processing_preview", fake_load_processing_preview)
    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        output_index = window._processing_preview_mode_combo.findData("output")
        assert output_index >= 0
        assert window._processing_preview_mode_combo.model().item(output_index).isEnabled() is False  # type: ignore[attr-defined]

        window._processing_title_draft_input.setText("Hook Title")
        window._on_process_video_clicked()
        qt_app.processEvents()

        assert window._processing_preview_mode_combo.model().item(output_index).isEnabled() is True  # type: ignore[attr-defined]
        window._processing_preview_mode_combo.setCurrentIndex(output_index)
        qt_app.processEvents()

        assert loaded_paths[-1].name == "reel_001.mp4"
        assert loaded_paths[-1].suffix == ".mp4"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_manual_top_crop_exports_without_auto_crop(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/manualcrop/",
                title="Manual Crop Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=10.0),
    )
    captured: dict[str, object] = {}

    def fail_suggest(window: MainWindow, job) -> None:  # noqa: ANN001
        raise AssertionError("manual crop should skip automatic crop detection")

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured["crop"] = job.crop
        captured["title_layout"] = job.title_layout
        captured["title_text"] = job.title_text

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", fail_suggest)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_title_draft_input.setText("Our Meme Title")
        window._processing_top_crop_spin.setValue(280)
        window._on_process_video_clicked()

        assert captured["crop"] == CropSettings(top=280)
        assert captured["title_layout"] == "top_band"
        assert captured["title_text"] == "Our Meme Title"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_export_path_is_organized_by_account(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="Test IG", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/accountpath/",
                title="Account Path Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=10.0),
    )
    captured: dict[str, object] = {}

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured["output_path"] = job.output_path

    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)
    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_title_draft_input.setText("Our Meme Title")
        window._on_process_video_clicked()

        assert captured["output_path"].parent == processed_dir() / "Test_IG"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_black_canvas_replaces_source_title_with_automatic_crop(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram", niche_label="meme clips")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/replace-title/",
                title="Source Title",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=10.0),
    )
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.suggest_title_replacement_crop",
        lambda path, probe: CropSettings(top=420),
    )
    captured: dict[str, object] = {}

    def fail_suggest(window: MainWindow, job) -> None:  # noqa: ANN001
        raise AssertionError("replacement crop should skip generic crop detection")

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured["crop"] = job.crop
        captured["title_layout"] = job.title_layout
        captured["title_text"] = job.title_text

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", fail_suggest)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_title_draft_input.setText("Our Meme Title")
        window._processing_vision_payload = {"top_text_type": "meme_joke"}
        window._on_process_video_clicked()

        assert captured["crop"] == CropSettings(top=420)
        assert captured["title_layout"] == "top_band"
        assert captured["title_text"] == "Our Meme Title"
        assert "replacement crop applied automatically" in window._processing_suggestion_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_black_canvas_replaces_source_title_without_vision_payload(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram", niche_label="meme clips")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/no-vision/",
                title="Video by meme.ig",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
                smart_vision_payload=None,
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=10.0),
    )
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.suggest_title_replacement_crop",
        lambda path, probe: CropSettings(top=360),
    )
    captured: dict[str, object] = {}

    def fail_suggest(window: MainWindow, job) -> None:  # noqa: ANN001
        raise AssertionError("top-band export should replace source title before generic crop")

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured["crop"] = job.crop
        captured["title_layout"] = job.title_layout
        captured["title_text"] = job.title_text

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", fail_suggest)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_title_draft_input.setText("Our Meme Title")
        assert window._processing_vision_payload is None
        window._on_process_video_clicked()

        assert captured["crop"] == CropSettings(top=360)
        assert captured["title_layout"] == "top_band"
        assert captured["title_text"] == "Our Meme Title"
        assert "replacement crop applied automatically" in window._processing_suggestion_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_open_video_button_opens_latest_processed_file(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    output_path = tmp_path / "clip_cropped.mp4"
    output_path.write_bytes(b"processed")
    opened_paths: list[str] = []

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.os.startfile",
        lambda path: opened_paths.append(path),
        raising=False,
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._processing_last_output_path = output_path
        window._refresh_processing_latest_output_state(output_path)
        window._on_open_latest_processed_output_clicked()

        assert opened_paths == [str(output_path)]
        assert "Opening the latest processed output." in window._status_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_open_video_re_resolves_latest_export_for_selected_item_after_re_export(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    """Exporting the same item twice should make Open Video open the NEW file.

    The second export updates ``item.processed_path`` in the DB and refreshes
    the panel state. Even if some intermediate refresh leaves the in-memory
    cache pointing at the first file, the click handler re-resolves from the
    current item's DB record and opens the latest export.
    """
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    output_dir = processed_dir() / "AccountA"
    output_dir.mkdir(parents=True, exist_ok=True)
    first_output = output_dir / "reel_001.mp4"
    second_output = output_dir / "reel_002.mp4"
    first_output.write_bytes(b"first")
    second_output.write_bytes(b"second")

    with get_session() as session:
        account = Account(name="AccountA", platform="instagram")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://instagram.com/reel/a/",
            title="Clip A",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
            processed_path=str(second_output),  # latest export wins
        )
        session.add(item)
        session.commit()
        item_id = item.id

    opened_paths: list[str] = []
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.os.startfile",
        lambda path: opened_paths.append(path),
        raising=False,
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._selected_processing_item_id = item_id
        # Cache is stale (still points at the first export). The click handler
        # MUST re-resolve from the item's DB record and open the second one.
        window._processing_last_output_path = first_output

        window._on_open_latest_processed_output_clicked()

        assert opened_paths == [str(second_output)]
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_open_video_state_does_not_carry_over_when_switching_to_unexported_item(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    """Selecting a never-exported item must clear the previous item's state.

    Without this, the panel kept showing the previous item's filename and the
    Open Video button stayed enabled — clicking it opened a file from a
    different video.
    """
    init_db()
    exported_video = tmp_path / "exported.mp4"
    exported_video.write_bytes(b"video")
    fresh_video = tmp_path / "fresh.mp4"
    fresh_video.write_bytes(b"video")
    output_dir = processed_dir() / "AccountA"
    output_dir.mkdir(parents=True, exist_ok=True)
    exported_output = output_dir / "reel_001.mp4"
    exported_output.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="AccountA", platform="instagram")
        session.add(account)
        session.flush()
        exported_item = DownloadItem(
            source_url="https://instagram.com/reel/exp/",
            title="Exported Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(exported_video),
            processed_path=str(exported_output),
        )
        fresh_item = DownloadItem(
            source_url="https://instagram.com/reel/fresh/",
            title="Fresh Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(fresh_video),
        )
        session.add_all([exported_item, fresh_item])
        session.commit()
        exported_id = exported_item.id
        fresh_id = fresh_item.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=720, height=1280, duration_seconds=10.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()
        # Start with the exported item — state should show its output.
        window._selected_processing_item_id = exported_id
        window._refresh_processing_output_preview()
        qt_app.processEvents()
        assert window._processing_latest_output_label.text() == "reel_001.mp4"
        assert window._processing_last_output_path == exported_output
        assert window._processing_open_latest_output_button.isVisible() is True

        # Switch to the never-exported item — state must clear, button hidden,
        # cache reset so no Open Video click can leak the previous file.
        window._selected_processing_item_id = fresh_id
        window._refresh_processing_output_preview()
        qt_app.processEvents()
        assert window._processing_last_output_path is None
        assert (
            window._processing_latest_output_label.text()
            == "No processed output yet in this session."
        )
        assert window._processing_open_latest_output_button.isVisible() is False
        assert window._processing_open_latest_output_button.isEnabled() is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_open_video_opens_each_items_own_latest_export_after_round_trip(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    """A → B → A: clicking Open Video on each must open THAT item's export.

    Previously the panel fell back to ``_latest_numbered_processed_output_path``
    on the account folder, which meant when you came back to item A the Open
    Video button pointed at item B's most recent file instead of A's.
    """
    init_db()
    video_a = tmp_path / "video_a.mp4"
    video_b = tmp_path / "video_b.mp4"
    video_a.write_bytes(b"a")
    video_b.write_bytes(b"b")
    output_dir = processed_dir() / "AccountA"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_a = output_dir / "reel_001.mp4"
    output_b = output_dir / "reel_002.mp4"
    output_a.write_bytes(b"export a")
    output_b.write_bytes(b"export b")

    with get_session() as session:
        account = Account(name="AccountA", platform="instagram")
        session.add(account)
        session.flush()
        item_a = DownloadItem(
            source_url="https://instagram.com/reel/a/",
            title="Clip A",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_a),
            processed_path=str(output_a),
        )
        item_b = DownloadItem(
            source_url="https://instagram.com/reel/b/",
            title="Clip B",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_b),
            processed_path=str(output_b),
        )
        session.add_all([item_a, item_b])
        session.commit()
        item_a_id = item_a.id
        item_b_id = item_b.id

    opened_paths: list[str] = []
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.os.startfile",
        lambda path: opened_paths.append(path),
        raising=False,
    )
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=720, height=1280, duration_seconds=10.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        # A → Open Video should open A's file.
        window._selected_processing_item_id = item_a_id
        window._refresh_processing_output_preview()
        qt_app.processEvents()
        window._on_open_latest_processed_output_clicked()

        # B → Open Video should open B's file.
        window._selected_processing_item_id = item_b_id
        window._refresh_processing_output_preview()
        qt_app.processEvents()
        window._on_open_latest_processed_output_clicked()

        # Back to A → Open Video should open A's file again (not B's).
        window._selected_processing_item_id = item_a_id
        window._refresh_processing_output_preview()
        qt_app.processEvents()
        window._on_open_latest_processed_output_clicked()

        assert opened_paths == [
            str(output_a),
            str(output_b),
            str(output_a),
        ]
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_preview_player_has_larger_review_controls(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert window._processing_video_widget.minimumHeight() == 520
        assert window._processing_video_widget.maximumHeight() == 520
        assert window._processing_video_widget.sizePolicy().verticalPolicy() == (
            QSizePolicy.Policy.Fixed
        )
        assert window._processing_preview_back_button.text() == "-1s"
        assert window._processing_preview_back_large_button.text() == "-5s"
        assert window._processing_preview_forward_large_button.text() == "+5s"
        assert window._processing_preview_forward_button.text() == "+1s"
        assert window._processing_preview_timer.isSingleShot() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_preview_seek_keeps_playback_when_already_playing(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window._processing_preview_duration_ms = 10_000
        window._processing_preview_position_ms = 4_000
        window._processing_preview_path = Path("C:/videos/clip.mp4")
        window._processing_preview_timer.start(1000)
        sought_positions: list[int] = []

        def fake_seek(position_ms: int) -> None:
            sought_positions.append(position_ms)
            window._processing_preview_position_ms = position_ms
            window._processing_preview_timer.stop()

        window._seek_processing_preview = fake_seek  # type: ignore[method-assign]

        window._shift_processing_preview(1000)

        assert sought_positions == [5_000]
        assert window._processing_preview_position_ms == 5_000
        assert window._processing_preview_timer.isActive() is True
        assert window._processing_toggle_preview_button.text() == "Pause Video"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_next_frame_delay_uses_video_frame_timing(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window._processing_preview_position_ms = 1033

        assert window._processing_next_frame_delay(1000) == 33
        assert window._processing_next_frame_delay(None) == 16

        window._processing_preview_position_ms = 1200
        assert window._processing_next_frame_delay(1000) == 80
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_applies_auto_suggested_crop_state(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=proc789",
                title="Suggest Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )
    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._start_suggest_crop_job(SuggestCropJobConfig(input_path=video_path))
        qt_app.processEvents()

        assert window._processing_crop_settings() == CropSettings(
            left=18, top=24, right=12, bottom=96
        )
        assert window._processing_progress_label.text() == "Automatic crop suggestion applied."
        assert "OCR text" in window._processing_suggestion_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_can_generate_and_save_text_drafts(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=proc999",
                title="Source title",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )
    monkeypatch.setattr("nicheflow_studio.app.main_window.can_generate_smart_drafts", lambda: False)
    monkeypatch.setattr(MainWindow, "_start_transcript_draft_job", _complete_draft_job_immediately)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._on_generate_text_drafts_clicked()
        qt_app.processEvents()

        assert window._processing_title_draft_input.text() == "Generated title draft"
        assert (
            window._processing_caption_draft_input.toPlainText()
            == "This is a generated caption draft."
        )
        assert "Source title" in window._processing_transcript_input.toPlainText()
        assert "generated transcript" in window._processing_transcript_input.toPlainText()

        window._processing_title_draft_input.setText("Edited draft")
        window._processing_caption_draft_input.setPlainText("Edited caption")
        window._on_save_text_drafts_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(DownloadItem).one()

        assert saved.title_draft == "Edited draft"
        assert saved.caption_draft == "Edited caption"
        assert saved.transcript_text == "This is a generated transcript. It has two sentences."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_saves_and_restores_style_settings(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=style123",
                title="Styled Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_template_combo.setCurrentIndex(
            window._processing_template_combo.findData("story_reel_clean")
        )
        window._processing_title_style_combo.setCurrentIndex(
            window._processing_title_style_combo.findData("boxed_banner")
        )
        window._processing_title_font_size.setValue(72)
        window._processing_title_font_combo.setCurrentIndex(
            window._processing_title_font_combo.findData("impact")
        )
        window._processing_title_color_input.setText("#FFD700")
        window._processing_title_background_combo.setCurrentIndex(
            window._processing_title_background_combo.findData("light")
        )
        window._processing_title_layout_combo.setCurrentIndex(
            window._processing_title_layout_combo.findData("overlay")
        )
        window._on_save_text_drafts_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(DownloadItem).one()

        assert saved.title_style_preset == "boxed_banner"
        assert '"template": "story_reel_clean"' in (saved.title_style_config or "")
        assert '"prompt_profile": "story_reel"' in (saved.title_style_config or "")
        assert '"font_size": 72' in (saved.title_style_config or "")
        assert '"font_name": "impact"' in (saved.title_style_config or "")
        assert '"background": "light"' in (saved.title_style_config or "")
        assert '"layout": "overlay"' in (saved.title_style_config or "")

        window._apply_refresh(force=True)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert window._processing_template_combo.currentData() == "story_reel_clean"
        assert window._processing_title_style_combo.currentData() == "boxed_banner"
        assert window._processing_title_font_size.value() == 72
        assert window._processing_title_font_combo.currentData() == "impact"
        assert window._processing_title_color_input.text() == "#FFD700"
        assert window._processing_title_background_combo.currentData() == "light"
        assert window._processing_title_layout_combo.currentData() == "overlay"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_can_apply_cinematic_study_template(
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="Cinema Files Daily", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/p/DVvuv6jEryy/",
                title="Cinema Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_template_combo.setCurrentIndex(
            window._processing_template_combo.findData("cinematic_study")
        )

        assert window._processing_template_combo.currentData() == "cinematic_study"
        assert window._processing_title_style_combo.currentData() == "cinematic_soft_italic"
        assert window._processing_title_layout_combo.currentData() == "top_band"
        assert window._processing_title_font_combo.currentData() == "comic_italic"
        assert window._processing_title_color_input.text() == "#F7F3EA"
        assert window._processing_title_font_size.value() == 58
        assert window._processing_prompt_profile() == "cinema_study"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_preferences_persist_per_account_across_switches(
    qt_app,
) -> None:
    """Each account remembers its own Processing widget snapshot (template,
    caption style, title style, font, size, colour, background, layout)
    so the user doesn't have to re-pick niche-appropriate styles every
    time they switch accounts. Switching back must restore the original
    snapshot — not bleed in values from the other account."""
    import json

    init_db()
    with get_session() as session:
        session.add(Account(name="Cinema A", platform="instagram"))
        session.add(Account(name="Meme B", platform="instagram"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._set_current_page("processing")
        qt_app.processEvents()

        # Activate Cinema A and pick the cinema template. The change
        # signals fire _save_processing_preferences_for_current_account,
        # which writes the snapshot to that account row.
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        cinema_idx = window._processing_template_combo.findData("cinematic_study")
        window._processing_template_combo.setCurrentIndex(cinema_idx)
        qt_app.processEvents()
        cinema_caption_idx = window._processing_caption_style_combo.findData("cinema_hook")
        window._processing_caption_style_combo.setCurrentIndex(cinema_caption_idx)
        qt_app.processEvents()
        cinema_template = window._processing_template_combo.currentData()
        cinema_caption = window._processing_caption_style_combo.currentData()
        cinema_font_size = window._processing_title_font_size.value()
        cinema_font = window._processing_title_font_combo.currentData()
        cinema_color = window._processing_title_color_input.text()

        # Activate Meme B and pick gaming meme. Meme B's first change
        # triggers its own save — the cinema snapshot for Cinema A must
        # NOT be overwritten by Meme B's selections.
        window._current_account_combo.setCurrentIndex(2)
        qt_app.processEvents()
        meme_idx = window._processing_template_combo.findData("gaming_meme_black")
        window._processing_template_combo.setCurrentIndex(meme_idx)
        qt_app.processEvents()
        meme_caption_idx = window._processing_caption_style_combo.findData("meme_friend_group")
        window._processing_caption_style_combo.setCurrentIndex(meme_caption_idx)
        qt_app.processEvents()

        # Sanity: both rows have distinct stored snapshots in the DB.
        with get_session() as session:
            cinema_row = session.query(Account).filter(Account.name == "Cinema A").one()
            meme_row = session.query(Account).filter(Account.name == "Meme B").one()
        cinema_prefs = json.loads(cinema_row.processing_preferences or "{}")
        meme_prefs = json.loads(meme_row.processing_preferences or "{}")
        assert cinema_prefs.get("template") == "cinematic_study"
        assert cinema_prefs.get("caption_style") == "cinema_hook"
        assert meme_prefs.get("template") == "gaming_meme_black"
        assert meme_prefs.get("caption_style") == "meme_friend_group"

        # Switch back to Cinema A — every cinema-side widget value must
        # be restored from the snapshot, not left at Meme B's values.
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        assert window._processing_template_combo.currentData() == cinema_template
        assert window._processing_caption_style_combo.currentData() == cinema_caption
        assert window._processing_title_font_size.value() == cinema_font_size
        assert window._processing_title_font_combo.currentData() == cinema_font
        assert window._processing_title_color_input.text() == cinema_color
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_alter_audio_checkbox_round_trips_through_preferences(qt_app) -> None:
    """The 'Alter audio' toggle is part of the per-account Processing snapshot:
    it serializes into the saved snapshot and is restored onto the widget by
    _apply_processing_preferences_for_account. (Account-switch restore order is
    covered by the template-persistence test; this isolates the checkbox's
    snapshot/save/apply contract deterministically.)"""
    import json

    init_db()
    with get_session() as session:
        session.add(Account(name="Repost A", platform="instagram"))
        session.commit()
        account_id = session.query(Account).filter(Account.name == "Repost A").one().id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._set_current_page("processing")
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        # 1) The snapshot reflects the checkbox state.
        window._processing_alter_audio_checkbox.setChecked(True)
        assert window._processing_preferences_snapshot()["alter_audio"] is True

        # 2) Saving writes it to the account row.
        window._suppress_processing_prefs_save = False
        window._save_processing_preferences_for_current_account()
        with get_session() as session:
            stored = session.get(Account, account_id).processing_preferences
        assert json.loads(stored)["alter_audio"] is True

        # 3) Applying restores it onto the widget even after it's flipped off.
        #    Block signals on the flip so it doesn't re-save over the stored value.
        window._processing_alter_audio_checkbox.blockSignals(True)
        window._processing_alter_audio_checkbox.setChecked(False)
        window._processing_alter_audio_checkbox.blockSignals(False)
        assert window._apply_processing_preferences_for_account(account_id) is True
        assert window._processing_alter_audio_checkbox.isChecked() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_loading_fresh_video_inherits_account_style_without_clobbering(
    qt_app,
) -> None:
    """Regression: in real usage an account HAS downloaded videos, so a
    refresh selects one and runs ``_load_processing_style_state``. That used to
    (a) overwrite the account's saved style with the video's gaming-meme
    fallback (the per-account snapshot was destroyed every refresh) and
    (b) reset the widgets to gaming_meme_black instead of the account's style.

    A fresh video (no per-video ``title_style_config``) must instead inherit
    the account's saved default style, and loading it must NOT change the
    account's stored preferences."""
    import json

    init_db()
    with get_session() as session:
        session.add(Account(name="Cinema A", platform="instagram"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._set_current_page("processing")
        qt_app.processEvents()

        # Activate Cinema A and set its niche style (persists to the account).
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        cinema_idx = window._processing_template_combo.findData("cinematic_study")
        window._processing_template_combo.setCurrentIndex(cinema_idx)
        qt_app.processEvents()

        with get_session() as session:
            account_id = session.query(Account).filter(Account.name == "Cinema A").one().id
            saved_before = (
                session.query(Account).filter(Account.id == account_id).one().processing_preferences
            )
        assert json.loads(saved_before).get("template") == "cinematic_study"

        # Simulate selecting a freshly downloaded video with no saved style.
        fresh_item = DownloadItem(
            source_url="https://instagram.com/reel/fresh/",
            title="Fresh Clip",
            status="downloaded",
            account_id=account_id,
            title_style_config=None,
            title_style_preset=None,
        )
        window._load_processing_style_state(fresh_item)
        qt_app.processEvents()

        # (b) The fresh video inherited the account's style, not gaming_meme.
        assert window._processing_template_combo.currentData() == "cinematic_study"
        # (a) Loading the video did NOT overwrite the account's saved snapshot.
        with get_session() as session:
            saved_after = (
                session.query(Account).filter(Account.id == account_id).one().processing_preferences
            )
        assert json.loads(saved_after).get("template") == "cinematic_study"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_combo_boxes_ignore_mouse_wheel_so_page_scroll_is_safe(
    qt_app,
) -> None:
    """Regression: rolling the wheel over a QComboBox normally changes the
    selection. With the processing panel inside a scroll area, every page
    scroll that crossed a combo silently flipped Template / Style / Font.

    Verifies the NoScrollComboBox + NoScrollSpinBox subclasses ignore
    wheelEvent (so the event bubbles up to the scroll area) instead of
    consuming it and changing value."""
    from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent

    init_db()
    with get_session() as session:
        session.add(Account(name="Wheel Account", platform="instagram"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        # Sample a few representative widgets across the processing panel.
        combos = [
            window._processing_template_combo,
            window._processing_title_style_combo,
            window._processing_title_font_combo,
            window._processing_title_layout_combo,
        ]
        spins = [
            window._processing_title_font_size,
        ]

        def _send_wheel(widget, delta: int) -> None:
            event = QWheelEvent(
                QPointF(5, 5),
                widget.mapToGlobal(QPoint(5, 5)).toPointF()
                if hasattr(widget.mapToGlobal(QPoint(5, 5)), "toPointF")
                else QPointF(widget.mapToGlobal(QPoint(5, 5))),
                QPoint(0, delta),
                QPoint(0, delta),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            qt_app.sendEvent(widget, event)

        for combo in combos:
            assert combo.count() > 1, f"combo {combo} should have >1 entries"
            original_index = combo.currentIndex()
            _send_wheel(combo, 120)
            _send_wheel(combo, -120)
            assert combo.currentIndex() == original_index, (
                f"combo {combo} index changed from {original_index} to "
                f"{combo.currentIndex()} after wheel event — scroll should be ignored"
            )

        for spin in spins:
            original_value = spin.value()
            _send_wheel(spin, 120)
            _send_wheel(spin, -120)
            assert spin.value() == original_value, (
                f"spinbox value changed from {original_value} to {spin.value()} "
                "after wheel event"
            )
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_style_panel_shows_template_and_hides_manual_title_overrides(
    qt_app,
) -> None:
    """Regression: the style panel hosting Template / Title Style / Font /
    Size / Color / Background was set to ``setVisible(False)`` in two places
    and never flipped on, so the user couldn't see (or pick) the Cinema
    Viral Bold template the source code already shipped. The test we had
    before (``…_can_apply_cinema_viral_bold_template``) passed only because
    Qt lets you set values on hidden widgets programmatically — it didn't
    catch that the dropdown was invisible to humans."""
    init_db()
    # An account is needed because the workspace is hidden behind the
    # library gate panel until one is selected.
    with get_session() as session:
        session.add(Account(name="Test Account", platform="instagram"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()
        assert window._processing_style_panel.isVisible(), (
            "style panel is hidden — Template/Style/Font dropdowns unreachable"
        )
        assert window._processing_template_combo.isVisible()
        assert not window._processing_title_style_combo.isVisible()
        assert not window._processing_title_font_combo.isVisible()
        assert not window._processing_title_font_size.isVisible()
        assert not window._processing_title_color_input.isVisible()
        assert not window._processing_title_background_combo.isVisible()
        assert not window._processing_title_layout_combo.isVisible()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_can_apply_cinema_viral_bold_template(
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="Cinema Files Daily", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/p/DWTz3EgATbi/",
                title="Cinema Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_template_combo.setCurrentIndex(
            window._processing_template_combo.findData("cinema_viral_bold")
        )

        assert window._processing_template_combo.currentData() == "cinema_viral_bold"
        assert window._processing_title_style_combo.currentData() == "cinema_bold_rounded"
        assert window._processing_title_layout_combo.currentData() == "top_band"
        assert window._processing_title_font_combo.currentData() == "arial_rounded_bold"
        assert window._processing_title_color_input.text() == "#FFFFFF"
        assert window._processing_title_font_size.value() == 56
        assert window._processing_prompt_profile() == "cinema_study"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_skips_invalid_video_files(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    invalid_path = tmp_path / "invalid.mp4"
    invalid_path.write_text("test", encoding="utf-8")
    valid_path = tmp_path / "valid.mp4"
    valid_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=badfile",
                title="Invalid Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(invalid_path),
            )
        )
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=goodfile",
                title="Valid Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(valid_path),
            )
        )
        session.commit()

    def fake_probe(path: Path) -> VideoProbe:
        if path.resolve() == invalid_path.resolve():
            raise RuntimeError("invalid video")
        return VideoProbe(width=1280, height=720, duration_seconds=12.0)

    monkeypatch.setattr("nicheflow_studio.app.main_window.probe_video", fake_probe)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert window._processing_item_combo.count() == 2
        assert window._processing_item_combo.currentText() == "Valid Clip"
        assert "invalid" not in window._processing_item_combo.itemText(1).lower()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_inbox_shows_status_source_and_selects_rows(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    processed_path = processed_dir() / "clip_cropped.mp4"
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    processed_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://youtube.com/watch?v=goodfile",
            video_id="goodfile",
            title="Valid Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
        )
        session.add(item)
        session.flush()
        item_id = item.id
        session.add(
            ScrapeCandidate(
                account_id=account.id,
                scrape_source_url="https://youtube.com/@clips",
                source_url="https://youtube.com/watch?v=goodfile",
                video_id="goodfile",
                title="Valid Clip",
                state="downloaded",
                queued_download_item_id=item.id,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert window._processing_item_combo.isVisible() is False
        assert window._processing_inbox_table.rowCount() == 1
        assert window._processing_inbox_table.item(0, 0).text() == "Processed"
        assert window._processing_inbox_table.item(0, 1).text() == "Scraped"
        assert window._processing_inbox_table.item(0, 2).text() == "Valid Clip"
        assert window._processing_inbox_table.item(0, 4).text() == "Reprocess"
        assert window._processing_inbox_table.isColumnHidden(1) is True
        assert window._processing_inbox_table.isColumnHidden(3) is True
        assert window._processing_inbox_table.isColumnHidden(4) is True

        window._processing_inbox_table.clearSelection()
        window._processing_inbox_table.selectRow(0)
        qt_app.processEvents()

        assert window._selected_processing_item_id == item_id
        assert "Valid Clip" in window._processing_preview_meta_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_reuses_cached_probe_results_between_refreshes(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "valid.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=goodfile",
                title="Valid Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    probe_calls: list[Path] = []

    def fake_probe(path: Path) -> VideoProbe:
        probe_calls.append(path)
        return VideoProbe(width=1280, height=720, duration_seconds=12.0)

    monkeypatch.setattr("nicheflow_studio.app.main_window.probe_video", fake_probe)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        initial_calls = len(probe_calls)
        assert initial_calls >= 1

        window._refresh_processing_page()
        window._refresh_processing_page()
        qt_app.processEvents()

        assert len(probe_calls) == initial_calls
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_non_processing_pages_do_not_probe_videos_during_refresh(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "valid.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://youtube.com/watch?v=goodfile",
            title="Valid Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
        )
        session.add(item)
        session.flush()
        processed_path = processed_dir() / "valid_clip_cropped.mp4"
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        processed_path.write_bytes(b"processed")
        session.add(
            UploadJob(
                account_id=account.id,
                download_item_id=item.id,
                processed_path=str(processed_path),
                title="Valid Clip Draft",
                privacy_status="private",
                status="draft",
            )
        )
        session.commit()

    probe_calls: list[Path] = []

    def fake_probe(path: Path) -> VideoProbe:
        probe_calls.append(path)
        return VideoProbe(width=1280, height=720, duration_seconds=12.0)

    monkeypatch.setattr("nicheflow_studio.app.main_window.probe_video", fake_probe)

    window = MainWindow()
    try:
        window.resize(1280, 760)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert probe_calls == []
        assert window._schedule_table.rowCount() == 1
        assert window._schedule_table.item(0, 1).text() == "Valid Clip"
        assert window._schedule_table.item(0, 2).text() == "Valid Clip Draft"
        assert window._schedule_table.item(0, 5).text() == "Draft"
        assert window._schedule_table.item(0, 5).foreground().color().name() == "#f5cd79"
        assert window._schedule_table.objectName() == "downloadQueueTable"
        assert window._schedule_table.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
        assert window._schedule_table.minimumHeight() >= 230
        assert window._schedule_table.maximumHeight() == window._schedule_table.height()

        window._apply_refresh(force=True)
        qt_app.processEvents()

        assert probe_calls == []
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_can_generate_and_apply_smart_drafts(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube", niche_label="animal comedy")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=smart123",
                title="Zoo source",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
                transcript_text="This is already transcribed.",
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )
    monkeypatch.setattr(MainWindow, "_start_smart_draft_job", _complete_smart_draft_job_immediately)
    monkeypatch.setattr("nicheflow_studio.app.main_window.can_generate_smart_drafts", lambda: True)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._on_generate_smart_drafts_clicked()
        qt_app.processEvents()

        assert "elephant hook" in window._processing_smart_summary_label.text().lower()
        assert len(window._processing_smart_option_buttons) == 3
        assert window._processing_smart_option_title_inputs[0].text() == "Elephant Chaos"
        assert (
            window._processing_smart_option_caption_inputs[0].toPlainText()
            == "This elephant stole the whole clip"
        )
        assert "Recommended Pick: Title Option 2 + Caption Option 2" in (
            window._processing_smart_recommendation_label.text()
        )
        assert "Best fit because it names the reveal clearly." in (
            window._processing_smart_recommendation_label.text()
        )
        assert window._processing_smart_option_note_labels[1].text() == "Best overall for reach."
        assert window._processing_smart_option_buttons[1].text() == "Apply Recommended"
        assert window._processing_smart_option_buttons[1].isChecked()
        assert window._processing_title_draft_input.text() == "Zoo Hook"
        assert (
            window._processing_caption_draft_input.toPlainText()
            == "Wait for the elephant reveal"
        )
        window._apply_refresh(force=True)
        window._set_current_page("processing")
        qt_app.processEvents()
        assert window._processing_title_draft_input.text() == "Zoo Hook"
        assert (
            window._processing_caption_draft_input.toPlainText()
            == "Wait for the elephant reveal"
        )

        window._on_processing_smart_option_clicked(0)
        assert window._processing_title_draft_input.text() == "Elephant Chaos"

        window._on_processing_smart_option_clicked(1)
        assert window._processing_title_draft_input.text() == "Zoo Hook"
        assert (
            window._processing_caption_draft_input.toPlainText() == "Wait for the elephant reveal"
        )

        window._on_save_text_drafts_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(DownloadItem).one()

        assert saved.smart_summary == "A funny zoo moment with a clear elephant hook."
        assert saved.smart_provider_label == "Groq Scout + Llama 3.3"
        assert "frame_count" in (saved.smart_generation_meta or "")
        assert "scene_summary" in (saved.smart_vision_payload or "")
        assert "Elephant Chaos" in (saved.smart_title_options or "")
        assert "Wait for the elephant reveal" in (saved.smart_caption_options or "")
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_smart_draft_multiline_title_survives_apply_save_reload(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    multiline_title = 'Friend: "stop sending me reels"\n\nMe:'

    with get_session() as session:
        account = Account(name="Memeists Daily", platform="instagram", niche_label="gaming memes")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://instagram.com/reel/multiline/",
                title="Source title",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=8.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._set_processing_smart_options(
            [multiline_title],
            ["That friend who keeps sending reels even after the warning."],
        )

        assert window._processing_smart_option_title_inputs[0].text() == multiline_title
        assert "\n\n" in window._processing_smart_option_title_inputs[0].text()

        window._on_processing_smart_option_clicked(0)
        assert window._processing_title_draft_input.text() == multiline_title
        assert "\n\n" in window._processing_title_draft_input.text()

        window._on_save_text_drafts_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(DownloadItem).one()
            assert saved.title_draft == multiline_title
            assert json.loads(saved.smart_title_options or "[]") == [multiline_title]

        window._apply_refresh(force=True)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert window._processing_title_draft_input.text() == multiline_title
        assert window._processing_smart_option_title_inputs[0].text() == multiline_title
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_smart_option_edits_survive_save_reload(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="Memeists Daily", platform="instagram", niche_label="movie memes")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://instagram.com/reel/edit-options/",
                title="Source title",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=8.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._set_processing_smart_options(
            ["POV: you thought 'Migration' was a kid's movie"],
            ["Old caption about Migration."],
        )
        title_input = window._processing_smart_option_title_inputs[0]
        caption_input = window._processing_smart_option_caption_inputs[0]
        title_input.setFocus()
        qt_app.processEvents()
        QTest.keyClick(title_input, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(title_input, "POV: you thought 'Hoppers' was a kid's movie")
        caption_input.setFocus()
        qt_app.processEvents()
        QTest.keyClick(caption_input, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(caption_input, "Corrected caption about Hoppers.")

        assert title_input.text() == "POV: you thought 'Hoppers' was a kid's movie"
        assert caption_input.toPlainText() == "Corrected caption about Hoppers."

        window._request_refresh()
        qt_app.processEvents()

        assert title_input.text() == "POV: you thought 'Hoppers' was a kid's movie"
        assert caption_input.toPlainText() == "Corrected caption about Hoppers."

        window._on_save_text_drafts_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(DownloadItem).one()
            assert json.loads(saved.smart_title_options or "[]") == [
                "POV: you thought 'Hoppers' was a kid's movie"
            ]
            assert json.loads(saved.smart_caption_options or "[]") == [
                "Corrected caption about Hoppers."
            ]

        window._apply_refresh(force=True)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert (
            window._processing_smart_option_title_inputs[0].text()
            == "POV: you thought 'Hoppers' was a kid's movie"
        )
        assert (
            window._processing_smart_option_caption_inputs[0].toPlainText()
            == "Corrected caption about Hoppers."
        )
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_smart_option_title_editors_have_bounded_height(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        for title_input in window._processing_smart_option_title_inputs:
            assert title_input.minimumHeight() == 58
            assert title_input.maximumHeight() == 92
            assert title_input.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed

        assert window._processing_title_draft_input.maximumHeight() == 118
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_shows_eval_debug_metadata(monkeypatch, qt_app, tmp_path: Path) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube", niche_label="animal comedy")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=smartdebug",
                title="Zoo source",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
                transcript_text="This is already transcribed.",
                smart_provider_label="Groq Scout + Llama 3.3",
                smart_generation_meta='{"writer_model":"llama-3.3-70b-versatile","frame_count":3}',
                smart_vision_payload='{"scene_summary":"An elephant reveal","main_action":"enters frame"}',
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert "Groq Scout + Llama 3.3" in window._processing_eval_provider_label.text()
        assert window._processing_debug_panel.isVisible() is False
        assert "frame_count" in window._processing_eval_meta_input.toPlainText()
        assert "scene_summary" in window._processing_eval_vision_input.toPlainText()
        window._processing_debug_toggle.click()
        qt_app.processEvents()
        assert window._processing_debug_panel.isVisible() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_shows_usage_budget_summary(monkeypatch, qt_app, tmp_path: Path) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube", niche_label="animal comedy")
        session.add(account)
        session.flush()
        session.add_all(
            [
                DownloadItem(
                    source_url="https://youtube.com/watch?v=smartbudget",
                    title="Zoo source",
                    status="downloaded",
                    account_id=account.id,
                    file_path=str(video_path),
                    smart_generation_meta='{"estimated_cost_usd":0.25}',
                    smart_generated_at=dt.datetime.now(dt.timezone.utc),
                ),
                DownloadItem(
                    source_url="https://youtube.com/watch?v=oldbudget",
                    title="Old source",
                    status="downloaded",
                    account_id=account.id,
                    file_path=str(video_path),
                    smart_generation_meta='{"estimated_cost_usd":0.40}',
                    smart_generated_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45),
                ),
            ]
        )
        session.commit()

    monkeypatch.setenv("GROQ_MONTHLY_BUDGET_USD", "1")
    monkeypatch.setenv("GROQ_MONTHLY_VIDEO_CAP", "1000")
    monkeypatch.setenv("GROQ_DAILY_VIDEO_CAP", "40")
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        usage_text = window._processing_usage_label.text()
        assert "$0.2500 / $1.00" in usage_text
        assert "1 / 1000 videos" in usage_text
        assert "daily cap 40" in usage_text
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_smart_draft_applies_ai_layout_suggestion(monkeypatch, qt_app, tmp_path: Path) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram", niche_label="meme clips")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/test/",
                title="Meme source",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=12.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_in_progress = True
        window._on_smart_draft_completed(
            {
                "summary": "A meme clip with source text.",
                "title_options": ["Generated Hook", "Second Hook", "Third Hook"],
                "caption_options": ["Caption one", "Caption two", "Caption three"],
                "provider_label": "Groq Scout + Llama 3.3",
                "used_fallback": False,
                "generation_meta": {},
                "vision_payload": {
                    "scene_summary": "A meme clip with built-in hook text.",
                    "top_text_type": "meme_joke",
                    "bottom_text_type": "subtitle",
                    "keep_top_text": True,
                    "keep_bottom_text": True,
                    "suggested_title_layout": "no_title",
                    "content_box": {"top": 0.0, "bottom": 1.0, "left": 0.0, "right": 1.0},
                    "crop_reason": "Keep source meme text and dialogue subtitles.",
                },
            }
        )
        qt_app.processEvents()

        assert window._processing_title_layout_combo.currentData() == "no_title"
        assert window._processing_auto_crop == CropSettings()
        assert window._processing_using_ai_layout_crop is True
        assert "AI layout suggestion" in window._processing_suggestion_label.text()
        assert "meme_joke" in window._processing_suggestion_label.text()
        assert "subtitle" in window._processing_suggestion_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_manual_black_canvas_override_runs_crop_detection_after_ai_no_title(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram", niche_label="meme clips")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/test/",
                title="Meme source",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=12.0),
    )
    captured: dict[str, object] = {}

    def fake_suggest(window: MainWindow, job) -> None:  # noqa: ANN001
        captured["suggest_input"] = job.input_path

    def fail_process(window: MainWindow, job) -> None:  # noqa: ANN001
        raise AssertionError("manual layout override should run crop detection first")

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", fake_suggest)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fail_process)
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.suggest_title_replacement_crop",
        lambda path, probe: CropSettings(),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_in_progress = True
        window._on_smart_draft_completed(
            {
                "summary": "A meme clip with source text.",
                "title_options": ["Generated Hook", "Second Hook", "Third Hook"],
                "caption_options": ["Caption one", "Caption two", "Caption three"],
                "provider_label": "Groq Scout + Llama 3.3",
                "used_fallback": False,
                "generation_meta": {},
                "vision_payload": {
                    "top_text_type": "meme_joke",
                    "bottom_text_type": "subtitle",
                    "suggested_title_layout": "no_title",
                    "content_box": {"top": 0.0, "bottom": 1.0, "left": 0.0, "right": 1.0},
                },
            }
        )
        qt_app.processEvents()
        assert window._processing_title_layout_combo.currentData() == "no_title"
        assert window._processing_using_ai_layout_crop is True

        black_canvas_index = window._processing_title_layout_combo.findData("top_band")
        window._processing_title_layout_combo.setCurrentIndex(black_canvas_index)
        qt_app.processEvents()

        assert window._processing_using_ai_layout_crop is False
        window._on_process_video_clicked()

        assert captured["suggest_input"] == video_path
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_ai_top_band_suggestion_does_not_block_replacement_crop(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    """Regression: generating drafts must not pre-fill the crop from the vision
    content_box. Doing so registered as a manual override and blocked the reliable
    heuristic content-rectangle crop from running on export."""
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram", niche_label="meme clips")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/test/",
                title="Meme source",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=12.0),
    )
    captured: dict[str, object] = {}

    def fake_replacement(path, probe):  # noqa: ANN001, ARG001
        captured["replacement_called"] = True
        return CropSettings(left=80, top=814, right=88, bottom=600)

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.suggest_title_replacement_crop",
        fake_replacement,
    )
    monkeypatch.setattr(
        MainWindow,
        "_start_processing_job",
        lambda self, job: captured.setdefault("job", job),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_in_progress = True
        window._on_smart_draft_completed(
            {
                "summary": "A meme clip.",
                "title_options": ["Hook one", "Hook two", "Hook three"],
                "caption_options": ["Cap one", "Cap two", "Cap three"],
                "provider_label": "Groq",
                "used_fallback": False,
                "generation_meta": {},
                "vision_payload": {
                    "top_text_type": "source_title",
                    "bottom_text_type": "none",
                    "suggested_title_layout": "top_band",
                    # An inaccurate vision content_box must not become the crop.
                    "content_box": {
                        "top": 0.1,
                        "bottom": 0.6,
                        "left": 0.2,
                        "right": 0.8,
                    },
                },
            }
        )
        qt_app.processEvents()

        # Generation must leave the crop fields untouched.
        assert window._has_manual_crop_override() is False
        assert window._processing_auto_crop == CropSettings()

        # Processing must then run the heuristic crop and adopt its result.
        window._on_process_video_clicked()
        assert captured.get("replacement_called") is True
        assert window._processing_auto_crop == CropSettings(
            left=80, top=814, right=88, bottom=600
        )
        assert captured["job"].crop != CropSettings()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_smart_generation_stops_at_monthly_budget(
    monkeypatch, qt_app, tmp_path: Path
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube", niche_label="animal comedy")
        session.add(account)
        session.flush()
        session.add_all(
            [
                DownloadItem(
                    source_url="https://youtube.com/watch?v=selected",
                    title="Zoo source",
                    status="downloaded",
                    account_id=account.id,
                    file_path=str(video_path),
                    transcript_text="This is already transcribed.",
                ),
                DownloadItem(
                    source_url="https://youtube.com/watch?v=spent",
                    title="Spent source",
                    status="downloaded",
                    account_id=account.id,
                    file_path=str(video_path),
                    smart_generation_meta='{"estimated_cost_usd":1.01}',
                    smart_generated_at=dt.datetime.now(dt.timezone.utc),
                ),
            ]
        )
        session.commit()

    monkeypatch.setenv("GROQ_MONTHLY_BUDGET_USD", "1")
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )
    monkeypatch.setattr("nicheflow_studio.app.main_window.can_generate_smart_drafts", lambda: True)
    started_jobs = []
    monkeypatch.setattr(
        MainWindow, "_start_smart_draft_job", lambda self, job: started_jobs.append(job)
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._on_generate_smart_drafts_clicked()
        qt_app.processEvents()

        assert started_jobs == []
        assert "budget" in window._toast_label.text().lower()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_smart_draft_fallback_surfaces_primary_provider_error(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/reel/fallback/",
                title="Fallback clip",
                status="downloaded",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_in_progress = True
        window._on_smart_draft_completed(
            {
                "summary": "Fallback result",
                "title_options": ["Fallback one", "Fallback two", "Fallback three"],
                "caption_options": ["Caption one", "Caption two", "Caption three"],
                "provider_label": "Local fallback",
                "used_fallback": True,
                "generation_meta": {
                    "errors": ["Groq reasoning model request failed: timed out"],
                    "writer_model": None,
                    "vision_model": None,
                },
            }
        )

        assert "Local fallback" in window._processing_draft_status_label.text()
        assert "timed out" in window._processing_draft_status_label.text()
        assert "primary provider failed" in window._toast_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_account_writing_preferences_persist(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._show_new_account_form()
        window._account_name_input.setText("YT Main")
        window._account_niche_input.setText("animal comedy")
        window._account_instagram_profile_input.setText("alt1")
        window._account_writing_tone_input.setText("playful")
        window._account_target_audience_input.setText("short-form animal fans")
        window._account_hook_style_input.setText("reaction-first")
        window._account_banned_phrases_input.setText("like and follow")
        window._account_title_style_notes_input.setText("short punchy hooks")
        window._account_caption_style_notes_input.setText("comment-style reactions")
        window._on_save_account_clicked()

        with get_session() as session:
            account = session.query(Account).filter(Account.name == "YT Main").one()

        assert account.writing_tone == "playful"
        assert account.target_audience == "short-form animal fans"
        assert account.hook_style == "reaction-first"
        assert account.banned_phrases == "like and follow"
        assert account.title_style_notes == "short punchy hooks"
        assert account.caption_style_notes == "comment-style reactions"
        assert account.instagram_profile == "alt1"
        assert account.scrape_max_items == 20
        assert account.scrape_max_age_days is None
        assert account.discovery_mode == "review_only"
        assert account.auto_queue_limit == 0
        assert account.min_view_count == 0
        assert account.min_like_count == 0
        assert account.ranking_weight_views == 35
        assert account.ranking_weight_likes == 20
        assert account.ranking_weight_recency == 25
        assert account.ranking_weight_keyword_match == 20
        assert account.upload_timezone == "Asia/Jakarta"
        assert account.upload_schedule_slots is None
        assert account.upload_default_privacy == "private"
        assert account.upload_made_for_kids == 0
        assert account.upload_contains_synthetic_media == 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_page_adds_processed_video_to_upload_schedule(qt_app, tmp_path: Path) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    output_path = processed_dir() / "clip_cropped.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(
            name="YT Main",
            platform="youtube",
            upload_timezone="Asia/Bangkok",
            upload_default_privacy="unlisted",
            upload_schedule_slots="09:00, 18:00",
        )
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://youtube.com/watch?v=clip",
            title="Original Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()
        window._selected_processing_item_id = item_id
        window._processing_last_output_path = output_path
        window._processing_title_draft_input.setText("Scheduled Hook")
        window._processing_caption_draft_input.setPlainText("Scheduled caption")

        window._on_add_processed_to_schedule_clicked()

        with get_session() as session:
            job = session.query(UploadJob).one()

        assert job.download_item_id == item_id
        assert job.processed_path == str(output_path)
        assert job.title == "Scheduled Hook"
        assert job.description == "Scheduled caption"
        assert job.privacy_status == "unlisted"
        assert job.timezone == "Asia/Bangkok"
        assert job.status == "scheduled"
        assert job.scheduled_at is not None
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_export_path_is_unique_per_run(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://instagram.com/reel/clip/",
            title="Original Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=720, height=1280, duration_seconds=10.0),
    )
    captured_paths: list[Path] = []

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured_paths.append(job.output_path)
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        job.output_path.write_bytes(b"processed")

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()
        window._selected_processing_item_id = item_id

        window._on_process_video_clicked()
        window._on_process_video_clicked()

        assert captured_paths[0].name == "reel_001.mp4"
        assert captured_paths[1].name == "reel_002.mp4"
        assert captured_paths[0] != captured_paths[1]
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_export_path_counts_existing_legacy_exports(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="Memeists Daily", platform="instagram")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://instagram.com/reel/clip/",
            title="Original Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    output_dir = processed_dir() / "Memeists_Daily"
    output_dir.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        (output_dir / f"Instagram_old_{index}_cropped.mp4").write_bytes(b"processed")

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=720, height=1280, duration_seconds=10.0),
    )
    captured_paths: list[Path] = []

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured_paths.append(job.output_path)

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()
        window._selected_processing_item_id = item_id

        window._on_process_video_clicked()

        assert captured_paths[0] == output_dir / "reel_006.mp4"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_export_persists_latest_numbered_output_for_queue(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "degenerate.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="LifeLagDaily", platform="instagram")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://instagram.com/reel/degenerate/",
            title="degenerate",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    output_dir = processed_dir() / "LifeLagDaily"
    output_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, 6):
        (output_dir / f"reel_{index:03d}.mp4").write_bytes(b"old")

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=720, height=1280, duration_seconds=10.0),
    )

    def fake_start_processing_job(window: MainWindow, job) -> None:  # noqa: ANN001
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        job.output_path.write_bytes(b"processed")
        _complete_processing_job_immediately(window, job)

    monkeypatch.setattr(MainWindow, "_start_suggest_crop_job", _complete_suggest_job_immediately)
    monkeypatch.setattr(MainWindow, "_start_processing_job", fake_start_processing_job)

    first_window = MainWindow()
    try:
        first_window.show()
        qt_app.processEvents()
        first_window._current_account_combo.setCurrentIndex(1)
        first_window._set_current_page("processing")
        qt_app.processEvents()
        first_window._selected_processing_item_id = item_id

        first_window._on_process_video_clicked()
        qt_app.processEvents()

        expected_output = output_dir / "reel_006.mp4"
        assert expected_output.exists()
        assert first_window._processing_latest_output_label.text() == "reel_006.mp4"
        with get_session() as session:
            saved_item = session.get(DownloadItem, item_id)
            assert saved_item is not None
            assert saved_item.processed_path == str(expected_output)
    finally:
        first_window._refresh_timer.stop()
        first_window._toast_timer.stop()
        first_window._hide_toast()
        first_window.close()

    second_window = MainWindow()
    try:
        second_window.show()
        qt_app.processEvents()
        second_window._current_account_combo.setCurrentIndex(1)
        second_window._set_current_page("processing")
        qt_app.processEvents()
        second_window._selected_processing_item_id = item_id
        second_window._refresh_processing_output_preview()
        qt_app.processEvents()

        assert second_window._processing_latest_output_label.text() == "reel_006.mp4"
        assert second_window._processing_add_to_schedule_button.isEnabled() is True

        second_window._on_add_processed_to_schedule_clicked()

        with get_session() as session:
            job = session.query(UploadJob).one()

        assert job.download_item_id == item_id
        assert job.processed_path == str(expected_output)
    finally:
        second_window._refresh_timer.stop()
        second_window._toast_timer.stop()
        second_window._hide_toast()
        second_window.close()


def test_processing_page_adds_latest_export_as_new_selected_publish_job(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")
    first_output = processed_dir() / "reel_001.mp4"
    second_output = processed_dir() / "reel_002.mp4"
    first_output.parent.mkdir(parents=True, exist_ok=True)
    first_output.write_bytes(b"first")
    second_output.write_bytes(b"second")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://instagram.com/reel/clip/",
            title="Original Clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(video_path),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=720, height=1280, duration_seconds=10.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()
        window._selected_processing_item_id = item_id

        window._processing_last_output_path = first_output
        window._processing_title_draft_input.setText("first title")
        window._processing_caption_draft_input.setPlainText("first caption")
        window._on_add_processed_to_schedule_clicked()

        window._processing_last_output_path = second_output
        window._processing_title_draft_input.setText("second title")
        window._processing_caption_draft_input.setPlainText("second caption")
        window._on_add_processed_to_schedule_clicked()

        with get_session() as session:
            jobs = session.query(UploadJob).order_by(UploadJob.created_at.asc()).all()

        assert len(jobs) == 2
        assert jobs[0].processed_path == str(first_output)
        assert jobs[1].processed_path == str(second_output)
        assert window._selected_schedule_job_id() == jobs[1].id
        assert window._schedule_caption_preview.toPlainText() == "second caption"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_schedule_page_auto_adds_processed_outputs(qt_app, tmp_path: Path) -> None:
    init_db()
    video_path = tmp_path / "auto.mp4"
    video_path.write_bytes(b"video")
    output_path = processed_dir() / "auto_cropped.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(
            name="YT Main",
            platform="youtube",
            upload_timezone="Asia/Bangkok",
            upload_default_privacy="unlisted",
        )
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=auto",
                title="Auto Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
                title_draft="Auto Hook",
                caption_draft="Auto caption",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.resize(1280, 760)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert window._schedule_table.rowCount() == 1
        assert window._schedule_table.item(0, 1).text() == "Auto Clip"
        assert window._schedule_table.item(0, 2).text() == "Auto Hook"
        assert window._schedule_table.item(0, 4).text() == "unlisted"
        assert window._schedule_table.item(0, 5).text() == "Draft"
        assert window._scroll_area.verticalScrollBar().maximum() == 0
        assert window._uploads_page.height() == window._scroll_area.viewport().height()
        assert window._schedule_table.height() == window._schedule_table.maximumHeight()

        with get_session() as session:
            job = session.query(UploadJob).one()

        assert job.processed_path == str(output_path)
        assert job.title == "Auto Hook"
        assert job.description == "Auto caption"
        assert job.status == "draft"

        window._refresh_schedule_page()
        with get_session() as session:
            assert session.query(UploadJob).count() == 1
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_uses_instagram_first_copy(qt_app) -> None:
    init_db()
    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert window._module_buttons["uploads"].toolTip() == "Publish"
        assert window._schedule_title_label.text() == "Publish Queue"
        assert "Instagram-ready Reels" in window._schedule_message_label.text()
        assert window._schedule_status_combo.count() == 4
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_has_manual_instagram_actions(qt_app, tmp_path: Path) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description="That ending was wild #gaming #reels",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        assert window._schedule_copy_caption_button.isEnabled() is True
        assert window._schedule_open_output_button.isEnabled() is True
        assert window._schedule_instagram_assist_button.isEnabled() is True
        assert window._schedule_status_combo.isEnabled() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_assisted_instagram_upload_opens_profile_and_copies_caption(
    qt_app,
    tmp_path: Path,
    monkeypatch,
) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")
    opened_paths: list[str] = []
    launched_profiles: list[str] = []

    with get_session() as session:
        account = Account(
            name="Memeists Daily",
            platform="instagram",
            instagram_profile="alt1",
        )
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="Cope title",
                description="Cope caption #memes",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.os.startfile",
        lambda path: opened_paths.append(str(path)),
    )
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.launch_instagram_upload_assist",
        lambda *, profile_name: launched_profiles.append(profile_name),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        qt_app.clipboard().clear()
        window._schedule_instagram_assist_button.click()
        qt_app.processEvents()

        assert qt_app.clipboard().text() == "Cope caption #memes"
        assert opened_paths == [str(output_path.parent)]
        assert launched_profiles == ["alt1"]
        assert "Caption copied" in window._status_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_copy_caption_copies_selected_description(qt_app, tmp_path: Path) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description="That ending was wild #gaming #reels",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        qt_app.clipboard().clear()
        window._schedule_copy_caption_button.click()
        qt_app.processEvents()

        assert qt_app.clipboard().text() == "That ending was wild #gaming #reels"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_selects_first_job_and_shows_caption_preview(qt_app, tmp_path: Path) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description="That ending was wild #gaming #reels",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert window._selected_schedule_job_id() is not None
        assert window._schedule_copy_caption_button.isEnabled() is True
        assert window._schedule_caption_preview.toPlainText() == "That ending was wild #gaming #reels"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_preserves_selection_after_status_change(qt_app, tmp_path: Path) -> None:
    init_db()
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"processed")
    second_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add_all(
            [
                UploadJob(
                    account_id=account.id,
                    processed_path=str(first_path),
                    title="First",
                    description="First caption",
                    privacy_status="public",
                    status="draft",
                ),
                UploadJob(
                    account_id=account.id,
                    processed_path=str(second_path),
                    title="Second",
                    description="Second caption",
                    privacy_status="public",
                    status="draft",
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(1)
        selected_id = window._selected_schedule_job_id()

        window._schedule_status_combo.setCurrentIndex(
            window._schedule_status_combo.findData("ready")
        )
        qt_app.processEvents()

        assert window._selected_schedule_job_id() == selected_id
        assert window._schedule_caption_preview.toPlainText() == "Second caption"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_caption_dropdown_can_copy_title_and_caption(
    qt_app, tmp_path: Path
) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description="That ending was wild #gaming #reels",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        window._schedule_caption_combo.setCurrentIndex(
            window._schedule_caption_combo.findData("title_caption")
        )
        qt_app.clipboard().clear()
        window._schedule_copy_caption_button.click()
        qt_app.processEvents()

        assert qt_app.clipboard().text() == (
            "This respawn was personal\n\nThat ending was wild #gaming #reels"
        )
        assert window._schedule_caption_preview.toPlainText() == qt_app.clipboard().text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_copy_path_and_status_dropdown(qt_app, tmp_path: Path) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description="That ending was wild #gaming #reels",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        qt_app.clipboard().clear()
        window._schedule_copy_path_button.click()
        window._schedule_status_combo.setCurrentIndex(
            window._schedule_status_combo.findData("ready")
        )
        qt_app.processEvents()

        with get_session() as session:
            job = session.query(UploadJob).one()

        assert qt_app.clipboard().text() == str(output_path)
        assert job.status == "ready"
        assert job.posted_at is None
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_sorts_scheduled_jobs_before_unscheduled(
    qt_app, tmp_path: Path
) -> None:
    init_db()
    early_path = tmp_path / "early.mp4"
    later_path = tmp_path / "later.mp4"
    unscheduled_path = tmp_path / "unscheduled.mp4"
    early_path.write_bytes(b"processed")
    later_path.write_bytes(b"processed")
    unscheduled_path.write_bytes(b"processed")
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add_all(
            [
                UploadJob(
                    account_id=account.id,
                    processed_path=str(unscheduled_path),
                    title="No Time",
                    privacy_status="public",
                    status="draft",
                ),
                UploadJob(
                    account_id=account.id,
                    processed_path=str(later_path),
                    title="Later",
                    scheduled_at=now + dt.timedelta(days=2),
                    privacy_status="public",
                    status="scheduled",
                ),
                UploadJob(
                    account_id=account.id,
                    processed_path=str(early_path),
                    title="Early",
                    scheduled_at=now + dt.timedelta(days=1),
                    privacy_status="public",
                    status="scheduled",
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert window._schedule_table.item(0, 2).text() == "Early"
        assert window._schedule_table.item(1, 2).text() == "Later"
        assert window._schedule_table.item(2, 2).text() == "No Time"
        assert window._schedule_table.item(2, 3).text() == "(unscheduled)"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_can_save_and_clear_manual_schedule_time(
    qt_app, tmp_path: Path
) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")
    selected_time = dt.datetime.now().astimezone().replace(
        hour=10,
        minute=30,
        second=0,
        microsecond=0,
    ) + dt.timedelta(days=3)

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="Manual Time",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        window._schedule_datetime_edit.setDateTime(window._datetime_to_qdatetime(selected_time))
        window._schedule_save_time_button.click()
        qt_app.processEvents()

        with get_session() as session:
            job = session.query(UploadJob).one()
            assert job.status == "scheduled"
            assert job.scheduled_at is not None
            scheduled_at = job.scheduled_at
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=dt.timezone.utc)
            assert scheduled_at.astimezone().strftime("%Y-%m-%d %H:%M") == selected_time.strftime(
                "%Y-%m-%d %H:%M"
            )

        assert window._schedule_table.item(0, 3).text().startswith(
            selected_time.strftime("%Y-%m-%d %H:%M")
        )

        window._schedule_clear_time_button.click()
        qt_app.processEvents()

        with get_session() as session:
            job = session.query(UploadJob).one()
            assert job.status == "draft"
            assert job.scheduled_at is None
        assert window._schedule_table.item(0, 3).text() == "(unscheduled)"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_queue_open_reel_opens_selected_output(qt_app, tmp_path: Path, monkeypatch) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")
    opened_paths: list[str] = []

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description="That ending was wild #gaming #reels",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    monkeypatch.setattr("nicheflow_studio.app.main_window.os.startfile", opened_paths.append)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        window._schedule_open_output_button.click()
        qt_app.processEvents()

        assert opened_paths == [str(output_path)]
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_schedule_page_keeps_overflow_inside_fixed_table(qt_app, tmp_path: Path) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        for index in range(40):
            output_path = tmp_path / f"queued_{index}.mp4"
            output_path.write_bytes(b"processed")
            session.add(
                UploadJob(
                    account_id=account.id,
                    processed_path=str(output_path),
                    title=f"Queued {index}",
                    privacy_status="private",
                    status="draft",
                )
            )
        session.commit()

    window = MainWindow()
    try:
        window.resize(1280, 520)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert window._schedule_table.rowCount() == 40
        assert window._scroll_area.verticalScrollBar().maximum() == 0
        assert window._schedule_table.height() == window._schedule_table.maximumHeight()
        assert window._schedule_table.verticalScrollBar().maximum() > 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_generate_drafts_uses_visual_first_smart_drafts(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube", niche_label="animal comedy")
        other_account = Account(name="Other Main", platform="youtube", niche_label="animal comedy")
        session.add(account)
        session.add(other_account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=chain123",
                title="Zoo source",
                source_description="Original caption says the elephant reacts to the keeper.",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=recent",
                title="Recent source",
                status="downloaded",
                account_id=account.id,
                title_draft="Already Used Elephant Hook",
                caption_draft="This caption was already used for this niche account.",
                smart_generated_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=other",
                title="Other source",
                status="downloaded",
                account_id=other_account.id,
                title_draft="Other Account Hook",
                caption_draft="This caption belongs to a different account.",
                smart_generated_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr("nicheflow_studio.app.main_window.can_generate_smart_drafts", lambda: True)
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )

    def fail_if_transcription_starts(window: MainWindow, job) -> None:  # noqa: ANN001
        raise AssertionError("visual-first generation should skip local transcription")

    captured_jobs = []

    def complete_and_capture_smart_job(window: MainWindow, job) -> None:  # noqa: ANN001
        captured_jobs.append(job)
        _complete_smart_draft_job_immediately(window, job)

    monkeypatch.setattr(MainWindow, "_start_transcript_draft_job", fail_if_transcription_starts)
    monkeypatch.setattr(MainWindow, "_start_smart_draft_job", complete_and_capture_smart_job)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()
        window._processing_clip_premise_input.setPlainText(
            "The elephant is reacting to a zoo keeper's bucket."
        )
        style_index = window._processing_caption_style_combo.findData("meme_friend_group")
        window._processing_caption_style_combo.setCurrentIndex(style_index)
        title_style_index = window._processing_prompt_title_style_combo.findData(
            "meme_setup_punchline"
        )
        window._processing_prompt_title_style_combo.setCurrentIndex(title_style_index)

        window._on_generate_text_drafts_clicked()
        qt_app.processEvents()

        assert window._processing_title_draft_input.text() == "Zoo Hook"
        assert (
            "No speech transcript is available" in window._processing_transcript_input.toPlainText()
        )
        assert captured_jobs
        assert captured_jobs[0].prompt_profile == "gaming_meme"
        assert captured_jobs[0].caption_style == "meme_friend_group"
        assert captured_jobs[0].title_style == "meme_setup_punchline"
        assert captured_jobs[0].source_description == (
            "Original caption says the elephant reacts to the keeper."
        )
        assert captured_jobs[0].recent_titles == ["Already Used Elephant Hook"]
        assert captured_jobs[0].recent_captions == [
            "This caption was already used for this niche account."
        ]
        assert (
            captured_jobs[0].account_voice["clip_context"]
            == "The elephant is reacting to a zoo keeper's bucket."
        )
        assert len(window._processing_smart_option_buttons) == 3
        assert "smart draft options" in window._processing_draft_status_label.text().lower()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_copy_chat_prompt_includes_local_file_and_niche_context(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "past_moments_clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(
            name="Past Moments Daily",
            platform="instagram",
            niche_label="history moments, old clips, and strange facts",
            writing_tone="curious, clear, factual",
            target_audience="history and curiosity viewers",
            hook_style="specific subject-first history hook",
            banned_phrases="you won't believe, shocking",
            title_style_notes="5-11 word lost archive hooks",
            caption_style_notes="3 short paragraphs with factual context",
        )
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://instagram.com/reel/example",
                title="Old footage with a strange backstory",
                source_description="Original post mentions a forgotten TV moment.",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
                transcript_text="A narrator describes the old clip.",
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=18.0),
    )
    qt_app.clipboard().clear()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._apply_processing_template("lost_archive_black")
        caption_index = window._processing_caption_style_combo.findData(
            "history_lost_archive"
        )
        window._processing_caption_style_combo.setCurrentIndex(caption_index)
        title_index = window._processing_prompt_title_style_combo.findData(
            "history_lost_archive"
        )
        window._processing_prompt_title_style_combo.setCurrentIndex(title_index)
        window._processing_clip_premise_input.setPlainText(
            "Focus on why the clip feels like a recovered file."
        )

        window._on_copy_generation_chat_prompt_clicked()
        prompt = qt_app.clipboard().text()

        assert "Please analyze this local NicheFlow video" in prompt
        assert str(video_path.resolve()) in prompt
        assert "File URL: file:///" in prompt
        assert "Past Moments Daily" in prompt
        assert "history moments, old clips, and strange facts" in prompt
        assert "Past Moments Black" in prompt
        assert "history_lost_archive" in prompt
        assert "Original post mentions a forgotten TV moment." in prompt
        assert "Focus on why the clip feels like a recovered file." in prompt
        assert "Style contract copied from NicheFlow smart drafts" in prompt
        assert "Caption word target: 90-150" in prompt
        assert "PAST MOMENTS DAILY" in prompt
        # History hooks now use the explanatory 9-16 word rules with a concrete
        # subject mandate (the Copy Chat Prompt must mirror live generation).
        assert "9-16 words" in prompt
        assert "NAMES the concrete visible subject" in prompt
        assert "Do not use generic filler hashtags like #fyp" in prompt
        assert "Generate 3 on-screen title options and 3 caption options" in prompt
        assert "recommend the strongest title/caption pair" in prompt
        assert "Recommended Pick:" in prompt
        assert "Selection Notes:" in prompt
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_copy_chat_prompt_includes_cinema_visual_style_recommendations(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "cinema_clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(
            name="Cinema Files Daily",
            platform="instagram",
            niche_label="Movie scenes, film recommendations, cinematic moments",
        )
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.instagram.com/p/DWTz3EgATbi/",
                title="Beautiful transition scene",
                source_description="Test category: beautiful cinematography.",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()
        account_id = account.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=18.0),
    )
    qt_app.clipboard().clear()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        account_index = window._current_account_combo.findData(account_id)
        window._current_account_combo.setCurrentIndex(account_index)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._apply_processing_template("cinema_viral_bold")
        caption_index = window._processing_caption_style_combo.findData("cinema_hook")
        window._processing_caption_style_combo.setCurrentIndex(caption_index)

        window._on_copy_generation_chat_prompt_clicked()
        prompt = qt_app.clipboard().text()

        assert "Cinema visual style recommendation:" in prompt
        assert "Editorial Italic = use Cinematic Study / Cinematic Soft Italic" in prompt
        assert "Bold Rounded = use Cinema Viral Bold / Cinema Bold Rounded" in prompt
        assert "beautiful cinematography -> Bold Rounded" in prompt
        assert "twist scene -> Editorial Italic" in prompt
        assert "Cinema Normal" in prompt
        assert "Recommended Style 1:" in prompt
        assert "Recommended Style 2:" in prompt
        assert "Recommended Style 3:" in prompt
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_clip_premise_refresh_keeps_cursor_position(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=premise123",
                title="Premise Clip",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._processing_clip_premise_input.setPlainText("Being a bus driver is a dream")
        cursor = window._processing_clip_premise_input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        window._processing_clip_premise_input.setTextCursor(cursor)
        before_position = window._processing_clip_premise_input.textCursor().position()

        window._refresh_processing_selection()
        qt_app.processEvents()

        assert window._processing_clip_premise_input.toPlainText() == (
            "Being a bus driver is a dream"
        )
        assert window._processing_clip_premise_input.textCursor().position() == before_position
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_generate_drafts_uses_visual_first_for_silent_clips(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube", niche_label="minecraft gameplay")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=silent123",
                title="Hoe hoe hoe 2",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr("nicheflow_studio.app.main_window.can_generate_smart_drafts", lambda: True)
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )

    def fail_if_transcription_starts(window: MainWindow, job) -> None:  # noqa: ANN001
        raise AssertionError("visual-first generation should skip local transcription")

    monkeypatch.setattr(MainWindow, "_start_transcript_draft_job", fail_if_transcription_starts)
    monkeypatch.setattr(MainWindow, "_start_smart_draft_job", _complete_smart_draft_job_immediately)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._on_generate_text_drafts_clicked()
        qt_app.processEvents()

        assert len(window._processing_smart_option_buttons) == 3
        assert window._processing_title_draft_input.text() == "Zoo Hook"
        assert (
            window._processing_caption_draft_input.toPlainText()
            == "Wait for the elephant reveal"
        )
        assert "smart draft options" in window._processing_draft_status_label.text().lower()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_generate_drafts_does_not_auto_chain_without_smart_draft_provider(
    monkeypatch,
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube", niche_label="animal comedy")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=chainrouter",
                title="Zoo source",
                status="downloaded",
                account_id=account.id,
                file_path=str(video_path),
            )
        )
        session.commit()

    monkeypatch.setattr("nicheflow_studio.app.main_window.can_generate_smart_drafts", lambda: False)
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.probe_video",
        lambda _: VideoProbe(width=1280, height=720, duration_seconds=12.0),
    )
    monkeypatch.setattr(MainWindow, "_start_transcript_draft_job", _complete_draft_job_immediately)
    monkeypatch.setattr(MainWindow, "_start_smart_draft_job", _complete_smart_draft_job_immediately)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        window._on_generate_text_drafts_clicked()
        qt_app.processEvents()

        assert len(window._processing_smart_option_buttons) == 3
        assert window._processing_title_draft_input.text() == "Generated title draft"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_queue_selected_candidate_uses_existing_download_flow(monkeypatch, qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips",
            source_url="https://www.youtube.com/@clips",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.flush()
        run = ScrapeRun(account_id=account.id, source_id=source.id, status="completed")
        session.add(run)
        session.flush()
        session.add(
            ScrapeCandidate(
                scrape_source_url="https://www.youtube.com/@clips",
                source_url="https://www.youtube.com/watch?v=queue123",
                extractor="youtube",
                video_id="queue123",
                title="Queue me",
                channel_name="Clips Channel",
                source_id=source.id,
                scrape_run_id=run.id,
                account_id=account.id,
                state="candidate",
            )
        )
        session.commit()

    captured: dict[str, object] = {}

    def fake_enqueue_download(
        *, url: str, account_id: int | None, callback=None, source_description=None
    ) -> int:  # noqa: ANN001
        captured["url"] = url
        captured["account_id"] = account_id
        captured["source_description"] = source_description
        return 77

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
        fake_enqueue_download,
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._candidate_table.selectRow(0)
        qt_app.processEvents()

        window._on_candidate_queue_clicked()
        qt_app.processEvents()

        assert captured["url"] == "https://www.youtube.com/watch?v=queue123"
        assert captured["account_id"] is not None
        assert window._status_label.text() == "Queued selected candidate."
        assert window._candidate_table.item(0, 0).text() == "queued"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_downloaded_candidate_can_be_queued_again_for_redownload(monkeypatch, qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips",
            source_url="https://www.youtube.com/@clips",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.flush()
        run = ScrapeRun(account_id=account.id, source_id=source.id, status="completed")
        session.add(run)
        session.flush()
        existing_item = DownloadItem(
            source_url="https://www.youtube.com/watch?v=queue123",
            extractor="youtube",
            video_id="queue123",
            title="Existing clip",
            status="downloaded",
            account_id=account.id,
        )
        session.add(existing_item)
        session.flush()
        session.add(
            ScrapeCandidate(
                scrape_source_url="https://www.youtube.com/@clips",
                source_url="https://www.youtube.com/watch?v=queue123",
                extractor="youtube",
                video_id="queue123",
                title="Queue me again",
                channel_name="Clips Channel",
                source_id=source.id,
                scrape_run_id=run.id,
                account_id=account.id,
                state="downloaded",
                queued_download_item_id=existing_item.id,
            )
        )
        session.commit()

    captured: dict[str, object] = {}

    def fake_enqueue_download(
        *, url: str, account_id: int | None, callback=None, source_description=None
    ) -> int:  # noqa: ANN001
        captured["url"] = url
        captured["account_id"] = account_id
        captured["source_description"] = source_description
        return 88

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
        fake_enqueue_download,
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._candidate_state_filter.setCurrentIndex(
            window._candidate_state_filter.findData("downloaded")
        )
        qt_app.processEvents()
        window._candidate_table.selectRow(0)
        qt_app.processEvents()

        assert window._candidate_queue_button.text() == "Redownload Candidate"
        assert "redownload it" in window._candidate_action_hint.text().lower()

        window._on_candidate_queue_clicked()
        qt_app.processEvents()

        assert captured["url"] == "https://www.youtube.com/watch?v=queue123"
        assert captured["account_id"] is not None
        assert window._status_label.text() == "Queued candidate for redownload."

        with get_session() as session:
            saved_candidate = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.video_id == "queue123", ScrapeCandidate.account_id == 1)
                .one()
            )

        assert saved_candidate.state == "queued"
        assert saved_candidate.queued_download_item_id == 88
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_keyword_discovery_auto_queues_top_ranked_candidate(monkeypatch, qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(
            name="YT Main",
            platform="youtube",
            discovery_keywords="funny, gaming",
            discovery_mode="auto_queue",
            auto_queue_limit=1,
            scrape_max_items=5,
            min_view_count=1000,
            min_like_count=100,
        )
        session.add(account)
        session.flush()
        session.add(
            Source(
                account_id=account.id,
                platform="youtube",
                source_type="youtube_profile",
                label="@clips",
                source_url="https://www.youtube.com/@clips",
                enabled=1,
                priority=100,
            )
        )
        session.commit()
        account_id = account.id

    captured: dict[str, object] = {}

    def fake_enqueue_download(
        *, url: str, account_id: int | None, callback=None, source_description=None
    ) -> int:  # noqa: ANN001
        captured["url"] = url
        captured["account_id"] = account_id
        captured["source_description"] = source_description
        return 55

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
        fake_enqueue_download,
    )
    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.scrape_youtube_source",
        lambda *, source_url, max_items, max_age_days: [
            ScrapedVideoCandidate(
                scrape_source_url=source_url,
                source_url="https://www.youtube.com/watch?v=auto123",
                extractor="youtube",
                video_id="auto123",
                title="Funny gaming clip",
                channel_name="Clips Channel",
                published_at=None,
                description="funny gaming highlight",
                view_count=200000,
                like_count=12000,
            )
        ],
    )
    monkeypatch.setattr(
        MainWindow,
        "_start_scrape_job",
        _run_scrape_job_immediately,
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(
            window._current_account_combo.findData(account_id)
        )
        qt_app.processEvents()

        window._on_scrape_clicked()
        qt_app.processEvents()

        assert captured["url"] == "https://www.youtube.com/watch?v=auto123"
        assert captured["account_id"] is not None
        assert window._candidate_table.rowCount() == 1
        assert window._candidate_table.item(0, 0).text() == "queued"
        assert window._candidate_table.item(0, 9).text() != "(none)"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_failed_instagram_source_scrape_does_not_advance_last_scraped_at(
    qt_app, monkeypatch
) -> None:
    init_db()
    previous_scrape = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@meme.ig",
            source_url="https://www.instagram.com/meme.ig/",
            enabled=1,
            priority=100,
            last_scraped_at=previous_scrape,
        )
        session.add(source)
        session.commit()
        account_id = account.id
        source_id = source.id

    def fail_apify_source(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("Apify source scrape failed")

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.scrape_instagram_source_apify",
        fail_apify_source,
    )

    window = MainWindow()
    try:
        with get_session() as session:
            source_row = session.get(Source, source_id)
            assert source_row is not None

        with pytest.raises(RuntimeError, match="Apify source scrape failed"):
            window._run_scrape_for_source(
                account_id=account_id,
                source=source_row,
                keywords=[],
                max_items=5,
                max_age_days=None,
                min_view_count=0,
                min_like_count=0,
                weights=DiscoveryWeights(),
            )

        with get_session() as session:
            failed_source = session.get(Source, source_id)
            assert failed_source is not None
            assert failed_source.last_scraped_at is not None
            restored_scrape = failed_source.last_scraped_at
            if restored_scrape.tzinfo is None:
                restored_scrape = restored_scrape.replace(tzinfo=dt.timezone.utc)
            assert restored_scrape == previous_scrape
            assert failed_source.last_run_status == "failed"
            assert failed_source.last_error_summary == "Apify source scrape failed"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_scrape_controls_disable_during_running_job_and_reenable_on_completion(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            Source(
                account_id=account.id,
                platform="youtube",
                source_type="youtube_profile",
                label="@clips",
                source_url="https://www.youtube.com/@clips",
                enabled=1,
                priority=100,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._scrape_in_progress = True
        window._refresh_candidate_action_state()

        assert window._scrape_button.isEnabled() is False
        assert window._scrape_selected_button.isEnabled() is False
        assert window._scrape_add_source_button.isEnabled() is False
        assert window._source_remove_button.isEnabled() is False

        window._on_scrape_completed(
            {
                "sources": 1,
                "created": 0,
                "refreshed": 0,
                "skipped": 0,
                "rejected": 0,
                "auto_queued": 0,
            }
        )
        qt_app.processEvents()

        assert window._scrape_in_progress is False
        assert window._scrape_button.isEnabled() is True
        assert window._scrape_add_source_button.isEnabled() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_activity_progress_bar_tracks_source_level_progress(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            Source(
                account_id=account.id,
                platform="youtube",
                source_type="youtube_profile",
                label="@clips",
                source_url="https://www.youtube.com/@clips",
                enabled=1,
                priority=100,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        window._candidate_sort_combo.setCurrentIndex(window._candidate_sort_combo.findData("newest"))
        qt_app.processEvents()

        job = window._build_scrape_job_for_all_enabled_sources()
        assert job is not None

        window._scrape_in_progress = True
        window._prepare_scrape_progress(total_sources=len(job.source_ids))
        window._scrape_progress_label.setText("Preparing scrape job...")
        qt_app.processEvents()

        assert window._scrape_progress_bar.isHidden() is True
        assert window._scrape_progress_bar.maximum() == 1
        assert window._scrape_progress_bar.value() == 0

        window._on_scrape_progress({"current": 1, "total": 1, "source_label": "@clips"})
        qt_app.processEvents()

        assert window._scrape_progress_label.text() == "Scraping 1/1: @clips"
        assert window._scrape_progress_label.isHidden() is True
        assert window._scrape_progress_bar.format() == "0/1 sources complete"
        assert window._activity_bar.isVisible() is True
        assert window._activity_status_label.text() == "Scraping 1/1: @clips"
        assert window._activity_progress_bar.format() == "0/1 sources complete"

        window._on_scrape_source_completed(
            {"source_label": "@clips", "created": 1, "refreshed": 0, "skipped": 0, "rejected": 0}
        )
        qt_app.processEvents()

        assert window._scrape_progress_bar.value() == 1
        assert window._scrape_progress_bar.format() == "1/1 sources complete"
        assert window._activity_progress_bar.value() == 1
        assert window._activity_progress_bar.format() == "1/1 sources complete"

        window._on_scrape_completed(
            {
                "sources": 1,
                "created": 1,
                "refreshed": 0,
                "skipped": 0,
                "rejected": 0,
                "auto_queued": 0,
            }
        )
        qt_app.processEvents()

        assert window._scrape_progress_bar.isHidden() is True
        assert window._activity_bar.isHidden() is True
        assert window._scrape_tabs.currentIndex() == 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_window_has_stable_minimum_size(qt_app) -> None:
    window = MainWindow()
    try:
        assert window.minimumWidth() == 1100
        assert window.minimumHeight() == 720
        assert window.width() == 1220
        assert window.height() == 780
        assert window.maximumWidth() > window.width()
        assert window.maximumHeight() > window.height()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_workspace_is_blocked_without_current_account(qt_app) -> None:
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert window._current_account_combo.currentData() is None
        assert window._url_input.isEnabled() is False
        assert window._table.isEnabled() is False
        assert window._library_gate_panel.isVisible() is True
        assert window._workspace_content.isVisible() is False
        assert window._account_panel.isVisible() is True
        assert window._sidebar_toggle_button.isEnabled() is False
        assert window._sidebar_toggle_button.isChecked() is True
        assert window._library_gate_label.alignment() == Qt.AlignmentFlag.AlignCenter
        assert (
            window._status_label.text() == "Create and select a niche account to use the library."
        )
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_retry_handler_updates_status_message(monkeypatch, qt_app) -> None:
    init_db()
    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=retry",
                title="Retry clip",
                status="failed",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.retry_item",
            lambda item_id: True,
        )
        window._on_retry_clicked(1)
        qt_app.processEvents()

        assert window._status_label.text() == "Retrying download."
        assert window._toast_label.text() == "Retrying download."
        assert window._toast_label.isVisible() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_empty_input_shows_warning_toast(qt_app) -> None:
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._url_input.setText("   ")
        window._on_download_clicked()
        qt_app.processEvents()

        assert window._status_label.text() == "Paste a URL first."
        assert window._toast_label.text() == "Paste a URL first."
        assert window._toast_label.isVisible() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_requires_current_account(qt_app) -> None:
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._url_input.setText("https://youtube.com/watch?v=test")
        window._on_download_clicked()
        qt_app.processEvents()

        assert window._status_label.text() == "Create and select a niche account first."
        assert window._toast_label.text() == "Create and select a niche account first."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_rejects_unsupported_domain_before_queueing(monkeypatch, qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._account_name_input.setText("YT Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        enqueue_calls: list[str] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            lambda **kwargs: enqueue_calls.append(kwargs["url"]),
        )

        window._url_input.setText("https://vimeo.com/12345")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert (
            window._status_label.text()
            == "Only YouTube and YouTube Shorts URLs are supported right now."
        )
        assert (
            window._toast_label.text()
            == "Only YouTube and YouTube Shorts URLs are supported right now."
        )
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_rejects_watch_url_without_video_id(monkeypatch, qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._account_name_input.setText("YT Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        enqueue_calls: list[str] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            lambda **kwargs: enqueue_calls.append(kwargs["url"]),
        )

        window._url_input.setText("https://www.youtube.com/watch")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert window._status_label.text() == "Enter a valid YouTube watch URL."
        assert window._toast_label.text() == "Enter a valid YouTube watch URL."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_accepts_youtube_shorts_url(monkeypatch, qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._account_name_input.setText("YT Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        captured: dict[str, object] = {}

        def fake_enqueue_download(
            *, url: str, account_id: int | None, callback=None, source_description=None
        ) -> int:  # noqa: ANN001
            captured["url"] = url
            captured["account_id"] = account_id
            captured["source_description"] = source_description
            return 99

        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            fake_enqueue_download,
        )

        window._url_input.setText("https://www.youtube.com/shorts/abc123")
        window._on_download_clicked()
        qt_app.processEvents()

        assert captured["url"] == "https://www.youtube.com/shorts/abc123"
        assert captured["account_id"] is not None
        assert window._status_label.text() == "Queued download."
        assert window._toast_label.text() == "Queued download."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_rejects_playlist_url_before_queueing(monkeypatch, qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._account_name_input.setText("YT Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        enqueue_calls: list[str] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            lambda **kwargs: enqueue_calls.append(kwargs["url"]),
        )

        window._url_input.setText("https://www.youtube.com/playlist?list=PL123")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert window._status_label.text() == "Playlist URLs are not supported right now."
        assert window._toast_label.text() == "Playlist URLs are not supported right now."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_rejects_channel_url_before_queueing(monkeypatch, qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._account_name_input.setText("YT Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        enqueue_calls: list[str] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            lambda **kwargs: enqueue_calls.append(kwargs["url"]),
        )

        window._url_input.setText("https://www.youtube.com/@creator")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert (
            window._status_label.text() == "Channel and profile URLs are not supported right now."
        )
        assert window._toast_label.text() == "Channel and profile URLs are not supported right now."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_rejects_homepage_url_before_queueing(monkeypatch, qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._account_name_input.setText("YT Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        enqueue_calls: list[str] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            lambda **kwargs: enqueue_calls.append(kwargs["url"]),
        )

        window._url_input.setText("https://www.youtube.com/")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert window._status_label.text() == "Use a YouTube watch, share, or Shorts URL."
        assert window._toast_label.text() == "Use a YouTube watch, share, or Shorts URL."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_rejects_downloaded_duplicate_for_same_account(monkeypatch, qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.youtube.com/watch?v=dup123",
                title="Existing clip",
                status="downloaded",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        enqueue_calls: list[str] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            lambda **kwargs: enqueue_calls.append(kwargs["url"]),
        )

        window._url_input.setText("https://youtu.be/dup123")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert (
            window._status_label.text()
            == "This video is already in this account library. Use Redownload from history."
        )
        assert (
            window._toast_label.text()
            == "This video is already in this account library. Use Redownload from history."
        )
        assert window._selected_item_id is not None
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_downloaded_history_item_can_be_redownloaded(monkeypatch, qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://www.youtube.com/watch?v=redl123",
            title="Existing clip",
            status="downloaded",
            account_id=account.id,
            file_path=str(Path.cwd() / "data" / "downloads" / "existing.mp4"),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._table.selectRow(0)
        qt_app.processEvents()

        assert window._detail_retry_button.text() == "Redownload Video"
        assert window._detail_retry_button.isEnabled() is True

        def fake_retry(item_id_arg: int) -> bool:
            with get_session() as session:
                item_row = session.get(DownloadItem, item_id_arg)
                assert item_row is not None
                item_row.status = "queued"
                item_row.file_path = None
                session.commit()
            return True

        monkeypatch.setattr("nicheflow_studio.app.main_window.QueueManager.retry_item", fake_retry)

        window._on_retry_clicked(item_id)
        qt_app.processEvents()

        assert window._status_label.text() == "Redownloading video."
        assert window._toast_label.text() == "Redownloading video."

        with get_session() as session:
            saved = session.get(DownloadItem, item_id)

        assert saved is not None
        assert saved.status == "queued"
        assert saved.file_path is None
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_retries_failed_duplicate_directly(monkeypatch, qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.youtube.com/shorts/fail123",
                title="Broken clip",
                status="failed",
                account_id=account.id,
                error_message="temporary outage",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        enqueue_calls: list[str] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
            lambda **kwargs: enqueue_calls.append(kwargs["url"]),
        )
        retry_calls: list[int] = []
        monkeypatch.setattr(
            "nicheflow_studio.app.main_window.QueueManager.retry_item",
            lambda item_id: retry_calls.append(item_id) or True,
        )

        window._url_input.setText("https://www.youtube.com/watch?v=fail123")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert retry_calls == [window._selected_item_id]
        assert window._status_label.text() == "Retrying download."
        assert window._toast_label.text() == "Retrying download."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_search_review_and_account_filters_limit_visible_rows(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Alpha", platform="youtube")
        session.add(account)
        session.flush()
        session.add_all(
            [
                DownloadItem(
                    source_url="https://youtube.com/watch?v=one",
                    extractor="youtube",
                    video_id="one",
                    title="Alpha clip",
                    status="downloaded",
                    review_state="kept",
                    account_id=account.id,
                    file_path=str(Path.cwd() / "data" / "downloads" / "alpha.mp4"),
                ),
                DownloadItem(
                    source_url="https://youtube.com/watch?v=two",
                    extractor="youtube",
                    video_id="two",
                    title="Broken clip",
                    status="failed",
                    review_state="rejected",
                    account_id=account.id,
                    error_message="bad URL",
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._table.rowCount() == 2

        window._search_input.setText("broken")
        qt_app.processEvents()
        assert window._table.rowCount() == 1
        assert window._table.item(0, 3).text() == "Broken clip"

        window._search_input.clear()
        window._review_filter.setCurrentIndex(window._review_filter.findData("rejected"))
        qt_app.processEvents()
        assert window._table.rowCount() == 1
        assert window._table.item(0, 1).text() == "Ignored"

        window._review_filter.setCurrentIndex(window._review_filter.findData("all"))
        window._search_input.setText("two")
        qt_app.processEvents()
        assert window._table.rowCount() == 1
        assert window._table.item(0, 3).text() == "Broken clip"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_selection_persists_across_refresh_until_user_clears_it(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=sticky",
                title="Sticky clip",
                status="downloaded",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._table.selectRow(0)
        qt_app.processEvents()

        assert window._selected_item_id is not None
        selected_before = window._selected_item_id
        assert window._detail_panel.isVisible() is False

        window._apply_refresh()
        qt_app.processEvents()

        assert window._selected_item_id == selected_before
        assert window._detail_fields["title"].text() == "Sticky clip"

        window._clear_selection()
        qt_app.processEvents()

        assert window._selected_item_id is None
        assert window._detail_panel.isVisible() is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_scroll_position_is_preserved_across_refresh(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add_all(
            [
                DownloadItem(
                    source_url=f"https://youtube.com/watch?v={index}",
                    title=f"Clip {index}",
                    status="downloaded",
                    account_id=account.id,
                )
                for index in range(40)
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        scrollbar = window._table.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        qt_app.processEvents()
        before_value = scrollbar.value()

        window._apply_refresh()
        qt_app.processEvents()

        assert window._table.verticalScrollBar().value() == before_value
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_account_assignment_and_review_state_persist(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._show_new_account_form()
        window._account_name_input.setText("YouTube Main")
        window._account_niche_input.setText("music")
        window._on_save_account_clicked()
        qt_app.processEvents()

        assert window._account_picker.count() >= 2
        assert window._current_account_combo.currentIndex() == 1

        with get_session() as session:
            account = session.query(Account).filter(Account.name == "YouTube Main").one()
            item = DownloadItem(
                source_url="https://youtube.com/watch?v=assign",
                title="Assign clip",
                account_id=account.id,
            )
            session.add(item)
            session.commit()

        window._apply_refresh(force=True)
        qt_app.processEvents()

        window._table.selectRow(0)
        qt_app.processEvents()
        assert window._detail_assign_button.isEnabled() is False
        window._set_review_state_for_selected("kept")
        qt_app.processEvents()
        assert window._detail_assign_button.isEnabled() is True
        window._detail_account_combo.setCurrentIndex(1)
        window._on_detail_assign_clicked()
        qt_app.processEvents()

        with get_session() as session:
            item = (
                session.query(DownloadItem).filter(DownloadItem.source_url.contains("assign")).one()
            )
            account = session.get(Account, item.account_id)

        assert item.review_state == "kept"
        assert account is not None
        assert account.name == "YouTube Main"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_workspace_appears_and_buttons_become_clearer_after_account_selection(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert window._account_mode == "main"
        assert window._account_main_new_button.text() == "New Niche Account"
        assert window._account_main_actions.isVisible() is True
        assert window._account_form_panel.isVisible() is False
        assert window._account_delete_panel.isVisible() is False
        assert window._account_save_button.text() == "Create Niche Account"
        assert window._account_delete_button.isEnabled() is False
        assert window._account_delete_button.text() == "Delete Selected Niche"

        window._show_new_account_form()
        qt_app.processEvents()
        window._account_name_input.setText("YouTube Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        assert window._account_mode == "main"
        assert window._account_main_actions.isHidden() is False
        assert window._library_gate_panel.isVisible() is False
        assert window._workspace_content.isVisible() is True
        assert window._account_panel.isVisible() is False
        assert window._sidebar_toggle_button.isEnabled() is True
        assert window._sidebar_toggle_button.isChecked() is False
        assert window._detail_assign_button.text() == "Save Niche Assignment"
        assert window._detail_keep_button.text() == "Keep For This Account"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_edit_picker_copy_is_clearer_and_selection_reset_hides_workspace(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert window._account_picker.itemText(0) == "Select niche account to edit..."

        window._show_new_account_form()
        window._account_name_input.setText("YouTube Main")
        window._on_save_account_clicked()
        qt_app.processEvents()

        assert window._workspace_content.isVisible() is True

        window._current_account_combo.setCurrentIndex(0)
        qt_app.processEvents()

        assert window._current_account_combo.currentData() is None
        assert window._workspace_content.isVisible() is False
        assert window._library_gate_panel.isVisible() is True
        assert window._account_panel.isVisible() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_account_management_modes_switch_cleanly(qt_app) -> None:
    init_db()

    with get_session() as session:
        session.add(Account(name="YouTube Main", platform="youtube"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._show_edit_account_form()
        qt_app.processEvents()
        assert window._account_mode == "edit"
        assert window._account_picker_panel.isVisible() is True
        assert window._account_form_panel.isVisible() is True
        assert window._account_delete_panel.isVisible() is False
        assert window._account_save_button.text() == "Save Niche Changes"

        window._show_delete_account_panel()
        qt_app.processEvents()
        assert window._account_mode == "delete"
        assert window._account_delete_panel.isVisible() is True
        assert window._account_form_panel.isVisible() is False
        assert window._account_main_actions.isVisible() is False

        window._show_account_main()
        qt_app.processEvents()
        assert window._account_mode == "main"
        assert window._account_main_actions.isVisible() is True
        assert window._account_picker_panel.isVisible() is False
        assert window._account_form_panel.isVisible() is False
        assert window._account_delete_panel.isVisible() is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_account_form_bottom_fields_scroll_above_actions(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.resize(620, 650)
        window.show()
        qt_app.processEvents()

        window._show_new_account_form()
        qt_app.processEvents()

        long_strategy_text = (
            "relatable life-lag humor for people who feel brain-foggy, behind on life, "
            "chronically buffering, and likely to send the reel to a friend"
        )
        window._account_niche_input.setText(long_strategy_text)
        qt_app.processEvents()

        assert window._account_panel.maximumWidth() == 760
        assert window._account_form_scroll.minimumHeight() == 420
        assert window._account_niche_input.lineWrapMode() == QTextEdit.LineWrapMode.WidgetWidth
        assert window._account_niche_input.minimumHeight() >= 82

        scroll_bar = window._account_form_scroll.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        qt_app.processEvents()

        caption_bottom = window._account_caption_style_notes_input.mapTo(
            window,
            window._account_caption_style_notes_input.rect().bottomLeft(),
        ).y()
        actions_top = window._account_form_actions.mapTo(
            window,
            window._account_form_actions.rect().topLeft(),
        ).y()

        assert caption_bottom < actions_top
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_sidebar_toggle_and_compact_library_behavior(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=compact",
                title="Compact clip",
                status="downloaded",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._account_panel.isVisible() is False
        assert window._sidebar_toggle_button.isChecked() is False
        assert window._table.isColumnHidden(2) is True
        assert window._table.isColumnHidden(4) is True
        assert window._table.isColumnHidden(5) is True
        assert window._table.isColumnHidden(6) is False
        assert window._table.isColumnHidden(7) is False
        assert window._table.horizontalHeaderItem(6).text() == "Size"
        assert window._table.horizontalHeaderItem(7).text() == "Added"
        assert window._table.item(0, 6).text() == "-"
        assert window._table.item(0, 7).text()
        assert window._table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

        window._toggle_account_sidebar()
        qt_app.processEvents()

        assert window._account_panel.isVisible() is True
        assert window._sidebar_toggle_button.isChecked() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_sidebar_account_manager_replaces_brand_slot(qt_app) -> None:
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        sidebar_layout = window._sidebar_panel.layout()
        assert sidebar_layout is not None
        top_widget = sidebar_layout.itemAt(0).widget()

        assert top_widget is window._sidebar_toggle_button
        assert window._sidebar_toggle_button.text() == ""
        assert window._sidebar_toggle_button.toolTip() == "Hide account manager"
        assert window._sidebar_toggle_button.objectName() == "sidebarToggle"
        assert window._sidebar_toggle_button.width() <= 40
        assert not hasattr(window, "_sidebar_brand")
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_sidebar_selected_state_and_pipeline_width(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._set_current_page("downloads")
        qt_app.processEvents()

        assert tuple(window._module_buttons) == ("scraping", "downloads", "processing", "uploads")
        assert window._module_buttons["scraping"].text() == ""
        assert window._module_buttons["scraping"].toolTip() == "Scrape"
        assert window._module_buttons["downloads"].text() == ""
        assert window._module_buttons["downloads"].toolTip() == "Download"
        assert window._module_buttons["processing"].text() == ""
        assert window._module_buttons["processing"].toolTip() == "Preprocess"
        assert window._module_buttons["uploads"].text() == ""
        assert window._module_buttons["uploads"].toolTip() == "Publish"
        assert window._sidebar_panel.width() >= 60
        assert window._sidebar_panel.width() <= 72
        assert window._sidebar_nav.height() <= 210
        assert all(button.height() <= 40 for button in window._module_buttons.values())
        assert all(button.width() <= 40 for button in window._module_buttons.values())
        assert window._sidebar_account_combo.isVisible() is False
        assert window._module_buttons["downloads"].property("selected") is True
        assert window._module_buttons["scraping"].property("selected") is False
        assert window._sidebar_toggle_button.property("selected") is False
        assert window._sidebar_toggle_button.text() == ""
        assert window._sidebar_toggle_button.toolTip() == "Open account manager"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_sidebar_is_fixed_outside_scrollable_workspace(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.resize(1280, 760)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert window._scroll_area.widget() is not window._sidebar_panel
        assert window._sidebar_panel.parent() is window
        assert window._activity_bar.parent() is window
        assert window._sidebar_panel.height() >= window._scroll_area.height() - 4
        assert window._sidebar_panel.width() == 64
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_page_switch_resets_workspace_scroll_to_top(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.resize(1280, 760)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("processing")
        qt_app.processEvents()
        QTest.qWait(50)
        qt_app.processEvents()

        scroll_bar = window._scroll_area.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        qt_app.processEvents()
        assert scroll_bar.value() > 0

        window._set_current_page("uploads")
        qt_app.processEvents()
        QTest.qWait(150)
        qt_app.processEvents()

        assert scroll_bar.value() == 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_sidebar_account_switcher_changes_workspace(qt_app) -> None:
    init_db()

    with get_session() as session:
        first = Account(name="YT Main", platform="youtube")
        second = Account(name="Animal Shorts", platform="youtube")
        session.add_all([first, second])
        session.flush()
        session.add_all(
            [
                DownloadItem(
                    source_url="https://youtube.com/watch?v=main",
                    title="Main account clip",
                    status="downloaded",
                    account_id=first.id,
                ),
                DownloadItem(
                    source_url="https://youtube.com/watch?v=animal",
                    title="Animal account clip",
                    status="downloaded",
                    account_id=second.id,
                ),
            ]
        )
        session.commit()
        second_id = second.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        sidebar_index = window._sidebar_account_combo.findData(second_id)
        assert sidebar_index > 0
        window._sidebar_account_combo.setCurrentIndex(sidebar_index)
        qt_app.processEvents()

        assert window._current_account_id == second_id
        assert window._current_account_combo.currentData() == second_id
        assert window._table.rowCount() == 1
        assert window._table.item(0, 3).text() == "Animal account clip"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_account_panel_does_not_overlap_sidebar_or_workspace(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=layout",
                title="Layout clip",
                status="downloaded",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.resize(1280, 820)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._toggle_account_sidebar()
        qt_app.processEvents()

        sidebar_rect = window._sidebar_panel.geometry()
        account_rect = window._account_panel.geometry()
        workspace_rect = window._scroll_area.geometry()

        assert sidebar_rect.right() < account_rect.left()
        assert account_rect.right() < workspace_rect.left()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_rejected_item_clears_assignment(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YouTube Main", platform="youtube")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://youtube.com/watch?v=reject",
            title="Reject clip",
            review_state="kept",
            account_id=account.id,
        )
        session.add(item)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._table.selectRow(0)
        qt_app.processEvents()

        window._set_review_state_for_selected("rejected")
        qt_app.processEvents()

        with get_session() as session:
            saved = (
                session.query(DownloadItem).filter(DownloadItem.source_url.contains("reject")).one()
            )

        assert saved.review_state == "rejected"
        assert saved.account_id is None
        assert window._status_label.text() == "Ignored item from this library."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_download_review_ui_uses_clear_labels_and_hints(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=reviewhint",
                title="Review hint clip",
                status="downloaded",
                review_state="kept",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._table.selectRow(0)
        qt_app.processEvents()

        assert window._table.item(0, 1).text() == "Kept"
        assert window._detail_fields["review"].text() == "Kept"
        assert "Kept for this account." in window._detail_review_hint.text()
        assert window._detail_reject_button.text() == "Ignore From Library"
        assert window._detail_reset_button.text() == "Return To Review"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_batch_review_actions_update_multiple_download_rows(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add_all(
            [
                DownloadItem(
                    source_url="https://youtube.com/watch?v=batch1",
                    title="Batch clip 1",
                    status="downloaded",
                    account_id=account.id,
                ),
                DownloadItem(
                    source_url="https://youtube.com/watch?v=batch2",
                    title="Batch clip 2",
                    status="downloaded",
                    account_id=account.id,
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._batch_keep_button.isEnabled() is False
        selection_model = window._table.selectionModel()
        first_index = window._table.model().index(0, 0)
        second_index = window._table.model().index(1, 0)
        selection_model.select(
            first_index,
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.select(
            second_index,
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        qt_app.processEvents()

        assert window._batch_keep_button.isEnabled() is True
        window._set_review_state_for_selection("kept")
        qt_app.processEvents()

        with get_session() as session:
            items = (
                session.query(DownloadItem)
                .filter(
                    DownloadItem.source_url.in_(
                        [
                            "https://youtube.com/watch?v=batch1",
                            "https://youtube.com/watch?v=batch2",
                        ]
                    )
                )
                .order_by(DownloadItem.source_url.asc())
                .all()
            )

        assert [item.review_state for item in items] == ["kept", "kept"]
        assert window._status_label.text() == "Kept 2 items for this account."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_account_form_draft_is_not_reset_by_library_refresh(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._show_new_account_form()
        window._account_name_input.setText("Draft account name")
        window._account_niche_input.setText("draft niche")
        qt_app.processEvents()

        window._apply_refresh()
        qt_app.processEvents()

        assert window._account_name_input.text() == "Draft account name"
        assert window._account_niche_input.toPlainText() == "draft niche"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_remove_from_history_deletes_row_but_not_file(qt_app) -> None:
    init_db()
    file_path = Path.cwd() / "data" / "downloads" / "kept.mp4"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.touch()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://youtube.com/watch?v=keep",
            title="Keep file",
            status="downloaded",
            account_id=account.id,
            file_path=str(file_path),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()
        window._on_remove_clicked(item_id)
        qt_app.processEvents()

        with get_session() as session:
            deleted = session.get(DownloadItem, item_id)

        assert deleted is None
        assert file_path.exists() is True
        assert window._table.rowCount() == 0
        assert window._toast_label.text() == "Removed item from history."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_account_manager_is_global_drawer_not_pipeline_page(qt_app) -> None:
    init_db()

    with get_session() as session:
        session.add(Account(name="YT Main", platform="youtube"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._set_current_page("accounts")
        qt_app.processEvents()

        assert window._current_page == "downloads"
        assert "accounts" not in window._module_buttons
        assert window._account_panel.isVisible() is False
        assert window._sidebar_toggle_button.isEnabled() is True
        window._sidebar_toggle_button.click()
        qt_app.processEvents()
        assert window._account_panel.isVisible() is True
        assert window._runtime_fields["data_dir"].text().endswith("data")
        assert window._export_backup_button.text() == "Create Backup Zip"
        assert window._restore_backup_button.text() == "Restore Backup Zip"

        window._sidebar_toggle_button.click()
        qt_app.processEvents()

        assert window._current_page == "downloads"
        assert window._account_panel.isVisible() is False
        assert window._sidebar_toggle_button.isEnabled() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_runs_tab_table_is_not_capped_to_tiny_height(qt_app) -> None:
    init_db()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert window._run_table.minimumHeight() >= 180
        assert window._run_table.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding
        assert window._run_table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_scraping_page_keeps_overflow_inside_fixed_tables(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        for index in range(40):
            session.add(
                Source(
                    account_id=account.id,
                    platform="youtube",
                    source_type="youtube_profile",
                    label=f"@source{index}",
                    source_url=f"https://www.youtube.com/@source{index}",
                    enabled=1,
                    priority=index,
                )
            )
        session.commit()

    window = MainWindow()
    try:
        window.resize(1280, 520)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        window._scrape_tabs.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._source_table.rowCount() == 40
        assert window._scroll_area.verticalScrollBar().maximum() == 0
        assert window._scraping_page.height() == window._scroll_area.viewport().height()
        assert window._scrape_tabs.height() == window._scrape_tabs.maximumHeight()
        assert window._source_table.verticalScrollBar().maximum() > 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_account_adds_hashtag_source(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._scrape_source_input.setText("#gaming")
        window._on_add_scrape_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            source = session.query(Source).one()
            account = session.query(Account).one()

        assert source.platform == "instagram"
        assert source.source_type == "instagram_hashtag"
        assert source.label == "#gaming"
        assert source.source_url == "https://www.instagram.com/explore/tags/gaming/"
        assert account.scrape_source_urls == "https://www.instagram.com/explore/tags/gaming/"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_account_adds_direct_post_candidate(qt_app, monkeypatch) -> None:
    init_db()

    def fake_scrape_instagram_urls_apify(
        urls: list[str],
        *,
        results_limit: int | None = None,
        timeout_secs: int = 300,
    ):
        assert urls == ["https://www.instagram.com/p/DYdxGRpO7Am/"]
        assert results_limit == 1
        return [
            ScrapedVideoCandidate(
                scrape_source_url=urls[0],
                source_url="https://www.instagram.com/reel/DYdxGRpO7Am/",
                extractor="apify:instagram",
                video_id="DYdxGRpO7Am",
                title="when the group chat goes quiet",
                channel_name="meme.ig",
                published_at=dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc),
                description="when the group chat goes quiet",
                view_count=250_000,
                like_count=42_000,
                comment_count=900,
                duration_seconds=18,
                thumbnail_url="https://example.test/thumb.jpg",
                discovery_query="manual",
                match_reason="instagram video",
            )
        ]

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.scrape_instagram_urls_apify",
        fake_scrape_instagram_urls_apify,
    )

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._scrape_source_input.setText("https://www.instagram.com/p/DYdxGRpO7Am/")
        window._on_add_scrape_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            candidates = session.query(ScrapeCandidate).all()
            sources = session.query(Source).all()

        assert sources == []
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.extractor == "apify:instagram"
        assert candidate.video_id == "DYdxGRpO7Am"
        assert candidate.source_url == "https://www.instagram.com/reel/DYdxGRpO7Am/"
        assert candidate.title == "when the group chat goes quiet"
        assert candidate.channel_name == "meme.ig"
        assert candidate.view_count == 250_000
        assert candidate.like_count == 42_000
        assert candidate.comment_count == 900
        assert candidate.duration_seconds == 18
        assert candidate.discovery_query == "manual"
        assert candidate.ranking_score is not None
        assert candidate.ranking_score > 0
        assert candidate.state == "candidate"
        assert window._candidate_table.rowCount() == 1
        assert "Added Instagram candidate URL." in window._status_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_account_adds_plural_reels_candidate_and_scrolls_to_it(
    qt_app,
    monkeypatch,
) -> None:
    init_db()

    shortcode = "DYLhdxWBmHB"
    newer_date = dt.datetime(2026, 5, 22, tzinfo=dt.timezone.utc)
    older_date = dt.datetime(2026, 5, 11, tzinfo=dt.timezone.utc)

    def fake_scrape_instagram_urls_apify(
        urls: list[str],
        *,
        results_limit: int | None = None,
        timeout_secs: int = 300,
    ):
        assert urls == [f"https://www.instagram.com/reel/{shortcode}/"]
        return [
            ScrapedVideoCandidate(
                scrape_source_url=urls[0],
                source_url=urls[0],
                extractor="apify:instagram",
                video_id=shortcode,
                title="the cackle at the end",
                channel_name="strongblacklead",
                published_at=older_date,
                description="the cackle at the end",
                view_count=9_000_000,
                like_count=2_571_225,
                comment_count=12_346,
                duration_seconds=8,
                thumbnail_url="https://example.test/thumb.jpg",
                discovery_query="manual",
                match_reason="instagram video",
            )
        ]

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.scrape_instagram_urls_apify",
        fake_scrape_instagram_urls_apify,
    )

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram", candidate_min_like_filter=100)
        session.add(account)
        session.flush()
        session.add(
            ScrapeCandidate(
                scrape_source_url="https://www.instagram.com/reel/newer/",
                source_url="https://www.instagram.com/reel/newer/",
                extractor="instagram",
                video_id="newer",
                title="newer reel",
                channel_name="meme.ig",
                published_at=newer_date,
                like_count=1_000,
                comment_count=5,
                duration_seconds=10,
                ranking_score=20,
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    scrolled_to: list[int] = []

    def fake_scroll_selected_candidate_into_view() -> None:
        scrolled_to.append(window._selected_candidate_id)

    window._scroll_selected_candidate_into_view = fake_scroll_selected_candidate_into_view  # type: ignore[method-assign]
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        window._candidate_sort_combo.setCurrentIndex(window._candidate_sort_combo.findData("newest"))
        qt_app.processEvents()

        window._scrape_source_input.setText(f"https://www.instagram.com/reels/{shortcode}/")
        window._on_add_scrape_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            candidate = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.video_id == shortcode)
                .one()
            )

        assert candidate.source_url == f"https://www.instagram.com/reel/{shortcode}/"
        assert window._candidate_table.rowCount() == 2
        assert window._candidate_table.horizontalHeaderItem(5).text() == "Added"
        assert window._candidate_table.horizontalHeaderItem(6).text() == "Published"
        assert window._candidate_table.item(0, 5).text() == MainWindow._candidate_added_text(
            candidate
        )
        assert window._candidate_table.item(0, 6).text() == "2026-05-11"
        assert window._candidate_table.item(0, 8).text() == "the cackle at the end"
        assert window._selected_candidate_id == candidate.id
        assert scrolled_to[-1] == candidate.id
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_account_adds_direct_post_candidate_when_metadata_fails(
    qt_app,
    monkeypatch,
) -> None:
    init_db()

    def fake_scrape_instagram_urls_apify(*args, **kwargs):  # noqa: ANN002, ANN003
        from nicheflow_studio.scraper.instagram_apify import ApifyScrapeError

        raise ApifyScrapeError("metadata unavailable")

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.scrape_instagram_urls_apify",
        fake_scrape_instagram_urls_apify,
    )

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._scrape_source_input.setText("https://www.instagram.com/p/DYdxGRpO7Am/")
        window._on_add_scrape_source_clicked()
        qt_app.processEvents()

        with get_session() as session:
            candidate = session.query(ScrapeCandidate).one()

        assert candidate.source_url == "https://www.instagram.com/p/DYdxGRpO7Am/"
        assert candidate.title == "Instagram media DYdxGRpO7Am"
        assert candidate.match_reason == "Manual Instagram URL"
        assert candidate.ranking_score == 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_candidate_min_likes_and_sort_controls(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        session.add_all(
            [
                ScrapeCandidate(
                    scrape_source_url="https://www.instagram.com/meme.ig/",
                    source_url="https://www.instagram.com/reel/lowlike/",
                    extractor="instagram",
                    video_id="lowlike",
                    title="Low like clip",
                    channel_name="meme.ig",
                    account_id=account.id,
                    state="candidate",
                    ranking_score=90,
                    like_count=5_000,
                    comment_count=20,
                    published_at=dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc),
                ),
                ScrapeCandidate(
                    scrape_source_url="https://www.instagram.com/meme.ig/",
                    source_url="https://www.instagram.com/reel/viral/",
                    extractor="instagram",
                    video_id="viral",
                    title="Viral clip",
                    channel_name="meme.ig",
                    account_id=account.id,
                    state="candidate",
                    ranking_score=70,
                    like_count=50_000,
                    comment_count=300,
                    published_at=dt.datetime(2026, 5, 17, tzinfo=dt.timezone.utc),
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        assert window._instagram_discover_min_likes_input.value() == 20_000
        assert window._candidate_table.rowCount() == 1
        assert window._candidate_table.item(0, 2).text() == "50,000"
        assert "Showing 1 of 2 candidate(s) with 20,000+ likes" in (
            window._candidate_filter_label.text()
        )

        window._instagram_discover_min_likes_input.setValue(0)
        window._candidate_sort_combo.setCurrentIndex(window._candidate_sort_combo.findData("score"))
        qt_app.processEvents()

        assert window._candidate_table.rowCount() == 2
        assert "Showing all 2 candidate(s)" in window._candidate_filter_label.text()
        assert window._candidate_table.item(0, 8).text() == "Low like clip"

        window._candidate_sort_combo.setCurrentIndex(window._candidate_sort_combo.findData("likes"))
        qt_app.processEvents()

        assert window._candidate_table.item(0, 8).text() == "Viral clip"

        window._candidate_sort_direction_combo.setCurrentIndex(
            window._candidate_sort_direction_combo.findData("asc")
        )
        qt_app.processEvents()

        assert window._candidate_table.item(0, 8).text() == "Low like clip"
        assert "low to high" in window._candidate_filter_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_candidate_source_filter_limits_visible_rows(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram", candidate_min_like_filter=0)
        session.add(account)
        session.flush()
        session.add_all(
            [
                ScrapeCandidate(
                    scrape_source_url="https://www.instagram.com/family.guy.reels/",
                    source_url="https://www.instagram.com/reel/family1/",
                    extractor="instagram",
                    video_id="family1",
                    title="Family clip",
                    channel_name="family.guy.reels",
                    account_id=account.id,
                    state="candidate",
                    ranking_score=80,
                    like_count=12_000,
                    comment_count=120,
                ),
                ScrapeCandidate(
                    scrape_source_url="https://www.instagram.com/meme.ig/",
                    source_url="https://www.instagram.com/reel/meme1/",
                    extractor="instagram",
                    video_id="meme1",
                    title="Meme clip",
                    channel_name="meme.ig",
                    account_id=account.id,
                    state="candidate",
                    ranking_score=90,
                    like_count=50_000,
                    comment_count=300,
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(
            window._current_account_combo.findText("IG Main (instagram)")
        )
        window._set_current_page("scraping")
        qt_app.processEvents()

        assert window._candidate_table.rowCount() == 2
        source_index = window._candidate_source_filter.findData("family.guy.reels")
        assert source_index >= 0

        window._candidate_source_filter.setCurrentIndex(source_index)
        qt_app.processEvents()

        assert window._candidate_table.rowCount() == 1
        assert window._candidate_table.item(0, 7).text() == "family.guy.reels"
        assert window._candidate_table.item(0, 8).text() == "Family clip"
        assert "from family.guy.reels" in window._candidate_filter_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_candidate_min_likes_filter_is_saved_per_account(qt_app) -> None:
    init_db()

    with get_session() as session:
        first = Account(name="IG Main", platform="instagram", candidate_min_like_filter=12_345)
        second = Account(name="Other IG", platform="instagram")
        session.add_all([first, second])
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(
            window._current_account_combo.findText("IG Main (instagram)")
        )
        window._set_current_page("scraping")
        qt_app.processEvents()

        assert window._instagram_discover_min_likes_input.value() == 12_345

        window._instagram_discover_min_likes_input.setValue(1)
        qt_app.processEvents()

        with get_session() as session:
            first = session.query(Account).filter(Account.name == "IG Main").one()
            second = session.query(Account).filter(Account.name == "Other IG").one()
            assert first.candidate_min_like_filter == 1
            assert second.candidate_min_like_filter is None

        window._current_account_combo.setCurrentIndex(
            window._current_account_combo.findText("Other IG (instagram)")
        )
        qt_app.processEvents()

        assert window._instagram_discover_min_likes_input.value() == 20_000
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_candidate_newest_sort_handles_mixed_timezone_datetimes(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        session.add_all(
            [
                ScrapeCandidate(
                    scrape_source_url="https://www.instagram.com/meme.ig/",
                    source_url="https://www.instagram.com/reel/aware/",
                    extractor="instagram",
                    video_id="aware",
                    title="Aware clip",
                    channel_name="meme.ig",
                    account_id=account.id,
                    state="candidate",
                    ranking_score=70,
                    like_count=50_000,
                    published_at=dt.datetime(2026, 5, 17, tzinfo=dt.timezone.utc),
                ),
                ScrapeCandidate(
                    scrape_source_url="https://www.instagram.com/meme.ig/",
                    source_url="https://www.instagram.com/reel/naive/",
                    extractor="instagram",
                    video_id="naive",
                    title="Naive clip",
                    channel_name="meme.ig",
                    account_id=account.id,
                    state="candidate",
                    ranking_score=70,
                    like_count=50_000,
                    published_at=dt.datetime(2026, 5, 18),
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._instagram_discover_min_likes_input.setValue(0)
        window._candidate_sort_combo.setCurrentIndex(window._candidate_sort_combo.findData("newest"))
        qt_app.processEvents()

        assert window._candidate_table.rowCount() == 2
        assert window._candidate_table.item(0, 8).text() == "Naive clip"
        assert window._candidate_table.item(1, 8).text() == "Aware clip"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_discover_uses_selected_source_and_requested_result_limit(
    qt_app, monkeypatch
) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        first_source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@first",
            source_url="https://www.instagram.com/first/",
            enabled=1,
            priority=100,
        )
        second_source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@meme.ig",
            source_url="https://www.instagram.com/meme.ig/",
            enabled=1,
            priority=90,
        )
        session.add_all([first_source, second_source])
        session.flush()
        account_id = account.id
        second_source_id = second_source.id
        session.commit()

    captured: dict[str, object] = {}

    def fake_start(self, job) -> None:  # noqa: ANN001, ARG001
        captured["job"] = job

    monkeypatch.setattr(MainWindow, "_start_scrape_job", fake_start)
    monkeypatch.setattr(MainWindow, "_confirm_instagram_scrape", lambda self, **kwargs: True)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._instagram_discover_source_combo.setCurrentIndex(
            window._instagram_discover_source_combo.findText("@meme.ig")
        )
        window._instagram_result_limit_input.setValue(20)
        window._on_instagram_discover_clicked()

        job = captured["job"]
        assert job.account_id == account_id
        assert job.source_ids == [second_source_id]
        assert job.max_items == 20
        assert job.min_like_count == 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_archive_backfill_uses_selected_depth_and_disables_hard_filters(
    qt_app,
    monkeypatch,
) -> None:
    init_db()

    with get_session() as session:
        account = Account(
            name="IG Main",
            platform="instagram",
            scrape_max_items=20,
            scrape_max_age_days=7,
            min_view_count=50_000,
            min_like_count=5_000,
        )
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@epicfunnypage",
            source_url="https://www.instagram.com/epicfunnypage/",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.flush()
        account_id = account.id
        source_id = source.id
        session.commit()

    captured: dict[str, object] = {}

    def fake_start(self, job) -> None:  # noqa: ANN001, ARG001
        captured["job"] = job

    monkeypatch.setattr(MainWindow, "_start_scrape_job", fake_start)
    monkeypatch.setattr(MainWindow, "_confirm_instagram_scrape", lambda self, **kwargs: True)
    confirmations: list[dict[str, object]] = []

    def fake_confirm(self, **kwargs):  # noqa: ANN001
        confirmations.append(kwargs)
        return True

    monkeypatch.setattr(MainWindow, "_confirm_instagram_scrape", fake_confirm)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._instagram_discover_source_combo.setCurrentIndex(
            window._instagram_discover_source_combo.findText("@epicfunnypage")
        )
        window._instagram_result_limit_input.setValue(500)
        window._on_instagram_archive_clicked()

        job = captured["job"]
        assert job.account_id == account_id
        assert job.source_ids == [source_id]
        assert job.max_items == 500
        assert job.max_age_days is None
        assert job.min_view_count == 0
        assert job.min_like_count == 0
        assert job.archive_backfill is True
        assert confirmations == [
            {
                "mode_label": "Search Archive",
                "result_limit": 500,
                "uses_latest_cursor": False,
            }
        ]
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_archive_backfill_skips_latest_since_cursor(qt_app, monkeypatch) -> None:
    init_db()
    previous_scrape = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    captured: dict[str, object] = {}

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@epicfunnypage",
            source_url="https://www.instagram.com/epicfunnypage/",
            enabled=1,
            priority=100,
            last_scraped_at=previous_scrape,
        )
        session.add(source)
        session.commit()
        account_id = account.id
        source_id = source.id

    def fake_scrape_instagram_source_apify(
        *,
        source_url: str,
        max_items: int,
        max_age_days: int | None,
        since: dt.datetime | None = None,
    ) -> list[ScrapedVideoCandidate]:
        captured["source_url"] = source_url
        captured["max_items"] = max_items
        captured["max_age_days"] = max_age_days
        captured["since"] = since
        return [
            ScrapedVideoCandidate(
                scrape_source_url=source_url,
                source_url="https://www.instagram.com/p/archive1/",
                extractor="apify:instagram",
                video_id="archive1",
                title="Archive clip",
                channel_name="epicfunnypage",
                published_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
                description="funny archive clip",
                view_count=10,
                like_count=2,
                comment_count=1,
            )
        ]

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.scrape_instagram_source_apify",
        fake_scrape_instagram_source_apify,
    )

    window = MainWindow()
    try:
        with get_session() as session:
            source_row = session.get(Source, source_id)
            assert source_row is not None

        created, refreshed, skipped, rejected = window._run_scrape_for_source(
            account_id=account_id,
            source=source_row,
            keywords=["funny"],
            max_items=100,
            max_age_days=None,
            min_view_count=0,
            min_like_count=0,
            weights=DiscoveryWeights(),
            archive_backfill=True,
        )

        assert (created, refreshed, skipped, rejected) == (1, 0, 0, 0)
        assert captured == {
            "source_url": "https://www.instagram.com/epicfunnypage/",
            "max_items": 100,
            "max_age_days": None,
            "since": None,
        }
        with get_session() as session:
            candidate = session.query(ScrapeCandidate).filter_by(video_id="archive1").one()
            assert candidate.ranking_score is not None
            assert candidate.ranking_score > 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_discover_account_cache_path_is_account_scoped() -> None:
    assert InstagramDiscoverRankWorker._account_cache_output_path(
        account_id=42,
        account_name="Memeists Daily",
        username="meme.ig",
    ) == Path("data") / "discovered" / "accounts" / "42-memeists-daily" / "meme.ig-urls.json"


def test_instagram_discover_worker_caps_metadata_extraction(qt_app, monkeypatch) -> None:
    commands: list[list[str]] = []
    failures: list[str] = []
    job = InstagramDiscoverRankJobConfig(
        username="meme.ig",
        target_account_id=42,
        target_account_name="Memeists Daily",
        min_new=100,
        limit=500,
    )
    worker = InstagramDiscoverRankWorker(job)

    def fake_run_command(args: list[str]) -> None:
        commands.append(args)

    monkeypatch.setattr(worker, "_run_command", fake_run_command)
    worker.failed.connect(failures.append)

    worker.run()

    assert commands == []
    assert failures == [
        "Legacy Instagram Playwright discovery is disabled. "
        "Use the normal Scrape flow, which uses Apify without Instagram login cookies."
    ]


def test_local_mp4_import_creates_downloaded_library_item(qt_app, tmp_path: Path) -> None:
    init_db()

    source_video = tmp_path / "sample clip.mp4"
    source_video.write_bytes(b"fake mp4 data")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        account_id = account.id
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._import_local_video_path(
            source_video,
            source_url="https://www.instagram.com/p/DYdxGRpO7Am/",
        )
        qt_app.processEvents()

        with get_session() as session:
            item = session.query(DownloadItem).one()

        imported_path = Path(item.file_path or "")
        assert item.extractor == "local"
        assert item.status == "downloaded"
        assert item.review_state == "new"
        assert item.account_id == account_id
        assert item.title == "sample clip"
        assert item.source_url == "https://www.instagram.com/p/DYdxGRpO7Am/"
        assert imported_path.exists()
        assert imported_path.parent == downloads_dir() / "local"
        assert imported_path.read_bytes() == b"fake mp4 data"
        assert source_video.exists()
        assert window._table.rowCount() == 1
        assert "Imported local MP4." in window._status_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_download_button_enqueues_download_and_links_candidate(
    monkeypatch,
    qt_app,
) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        expected_account_id = account.id
        session.add(
            ScrapeCandidate(
                account_id=expected_account_id,
                scrape_source_url="https://www.instagram.com/p/DYdxGRpO7Am/",
                source_url="https://www.instagram.com/p/DYdxGRpO7Am/",
                extractor="instagram",
                video_id="DYdxGRpO7Am",
                title="Instagram media DYdxGRpO7Am",
                match_reason="Manual Instagram URL",
            )
        )
        session.commit()

    def fake_enqueue(*, url: str, account_id: int | None, callback=None) -> int:  # noqa: ANN001
        assert url == "https://www.instagram.com/p/DYdxGRpO7Am/"
        assert account_id == expected_account_id
        with get_session() as session:
            item = DownloadItem(source_url=url, account_id=account_id, status="queued")
            session.add(item)
            session.commit()
            return item.id

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
        fake_enqueue,
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._url_input.setText("https://www.instagram.com/p/DYdxGRpO7Am/")
        window._on_download_clicked()
        qt_app.processEvents()

        with get_session() as session:
            candidate = session.query(ScrapeCandidate).one()
            download = session.query(DownloadItem).one()

        assert download.source_url == "https://www.instagram.com/p/DYdxGRpO7Am/"
        assert download.status == "queued"
        assert candidate.state == "queued"
        assert candidate.queued_download_item_id == download.id
        assert window._url_input.text() == ""
        assert "Queued download." in window._status_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_download_button_requires_instagram_account(
    monkeypatch,
    qt_app,
) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.commit()

    def fail_if_enqueued(**_: object) -> int:
        raise AssertionError("Instagram URLs should not hit the downloader from YouTube accounts")

    monkeypatch.setattr(
        "nicheflow_studio.app.main_window.QueueManager.enqueue_download",
        fail_if_enqueued,
    )

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._url_input.setText("https://www.instagram.com/p/DYdxGRpO7Am/")
        window._on_download_clicked()
        qt_app.processEvents()

        with get_session() as session:
            assert session.query(ScrapeCandidate).count() == 0
            assert session.query(DownloadItem).count() == 0

        assert "Select or create an Instagram niche account" in window._status_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_local_mp4_import_marks_matching_candidate_downloaded(
    qt_app,
    tmp_path: Path,
) -> None:
    init_db()

    source_video = tmp_path / "sample.mp4"
    source_video.write_bytes(b"fake mp4 data")

    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            ScrapeCandidate(
                account_id=account.id,
                scrape_source_url="https://www.instagram.com/p/DYdxGRpO7Am/",
                source_url="https://www.instagram.com/p/DYdxGRpO7Am/",
                extractor="instagram",
                video_id="DYdxGRpO7Am",
                title="Instagram media DYdxGRpO7Am",
                match_reason="Manual Instagram URL",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._import_local_video_path(
            source_video,
            source_url="https://www.instagram.com/p/DYdxGRpO7Am/",
        )
        qt_app.processEvents()

        with get_session() as session:
            item = session.query(DownloadItem).one()
            candidate = session.query(ScrapeCandidate).one()

        assert candidate.state == "downloaded"
        assert candidate.queued_download_item_id == item.id
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_library_workspace_only_shows_current_account_items(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        other_account = Account(name="Other YT", platform="youtube")
        session.add_all([account, other_account])
        session.flush()
        account_id = account.id
        session.add_all(
            [
                DownloadItem(
                    source_url="https://youtube.com/watch?v=current1",
                    title="Current account clip",
                    status="downloaded",
                    account_id=account.id,
                ),
                DownloadItem(
                    source_url="https://youtube.com/watch?v=unassigned1",
                    title="Unassigned clip",
                    status="downloaded",
                    account_id=None,
                ),
                DownloadItem(
                    source_url="https://youtube.com/watch?v=other1",
                    title="Other account clip",
                    status="downloaded",
                    account_id=other_account.id,
                ),
            ]
        )
        session.commit()

        window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(
            window._current_account_combo.findData(account_id)
        )
        qt_app.processEvents()

        assert window._table.rowCount() == 1
        assert window._table.item(0, 3).text() == "Current account clip"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_candidate_state_filter_and_downloaded_color_are_visible(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips",
            source_url="https://www.youtube.com/@clips",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.flush()
        run = ScrapeRun(account_id=account.id, source_id=source.id, status="completed")
        session.add(run)
        session.flush()
        session.add_all(
            [
                ScrapeCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.youtube.com/watch?v=candidate1",
                    extractor="youtube",
                    video_id="candidate1",
                    title="Candidate clip",
                    channel_name="Clips Channel",
                    source_id=source.id,
                    scrape_run_id=run.id,
                    account_id=account.id,
                    state="candidate",
                ),
                ScrapeCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.youtube.com/watch?v=queued1",
                    extractor="youtube",
                    video_id="queued1",
                    title="Queued clip",
                    channel_name="Clips Channel",
                    source_id=source.id,
                    scrape_run_id=run.id,
                    account_id=account.id,
                    state="queued",
                ),
                ScrapeCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.youtube.com/watch?v=downloaded1",
                    extractor="youtube",
                    video_id="downloaded1",
                    title="Downloaded clip",
                    channel_name="Clips Channel",
                    source_id=source.id,
                    scrape_run_id=run.id,
                    account_id=account.id,
                    state="downloaded",
                ),
                ScrapeCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.youtube.com/watch?v=ignored1",
                    extractor="youtube",
                    video_id="ignored1",
                    title="Ignored clip",
                    channel_name="Clips Channel",
                    source_id=source.id,
                    scrape_run_id=run.id,
                    account_id=account.id,
                    state="ignored",
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._candidate_table.rowCount() == 4

        window._candidate_state_filter.setCurrentIndex(
            window._candidate_state_filter.findData("downloaded")
        )
        qt_app.processEvents()

        assert window._candidate_table.rowCount() == 1
        assert window._candidate_table.item(0, 0).text() == "downloaded"
        assert window._candidate_table.item(0, 8).text() == "Downloaded clip"
        assert window._candidate_table.item(0, 0).background().color().name() == "#11271a"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_candidate_review_actions_show_clear_hint_and_restore_ignored_candidate(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips",
            source_url="https://www.youtube.com/@clips",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.flush()
        run = ScrapeRun(account_id=account.id, source_id=source.id, status="completed")
        session.add(run)
        session.flush()
        candidate = ScrapeCandidate(
            scrape_source_url=source.source_url,
            source_url="https://www.youtube.com/watch?v=ignored1",
            extractor="youtube",
            video_id="ignored1",
            title="Ignored clip",
            channel_name="Clips Channel",
            source_id=source.id,
            scrape_run_id=run.id,
            account_id=account.id,
            state="ignored",
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()

        window._candidate_state_filter.setCurrentIndex(
            window._candidate_state_filter.findData("ignored")
        )
        qt_app.processEvents()
        window._candidate_table.selectRow(0)
        qt_app.processEvents()

        assert "Ignored for now." in window._candidate_action_hint.text()
        assert window._candidate_restore_button.isEnabled() is True

        window._on_candidate_restore_clicked()
        qt_app.processEvents()

        with get_session() as session:
            candidate_row = session.get(ScrapeCandidate, candidate_id)

        assert candidate_row is not None
        assert candidate_row.state == "candidate"
        assert window._status_label.text() == "Returned candidate to review."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_scrape_intake_allows_video_already_downloaded_in_another_account(qt_app) -> None:
    init_db()

    with get_session() as session:
        account_one = Account(name="YT One", platform="youtube")
        account_two = Account(name="YT Two", platform="youtube")
        session.add_all([account_one, account_two])
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://www.youtube.com/watch?v=shared123",
                video_id="shared123",
                title="Shared clip",
                status="downloaded",
                account_id=account_one.id,
            )
        )
        source = Source(
            account_id=account_two.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips-two",
            source_url="https://www.youtube.com/@clips-two",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.commit()
        account_two_id = account_two.id
        source_id = source.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        source = next(
            item
            for item in window._load_sources_for_account(account_two_id)
            if item.id == source_id
        )

        created_count, refreshed_count, skipped_count = window._persist_scrape_candidates(
            account_id=account_two_id,
            source=source,
            scrape_run_id=1,
            candidates=[
                ScrapedVideoCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.youtube.com/watch?v=shared123",
                    extractor="youtube",
                    video_id="shared123",
                    title="Shared clip duplicate",
                    channel_name="Clips Two",
                    published_at=None,
                )
            ],
        )

        assert created_count == 1
        assert refreshed_count == 0
        assert skipped_count == 0

        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == account_two_id)
                .all()
            )

        assert len(candidates) == 1
        assert candidates[0].video_id == "shared123"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_scrape_intake_allows_candidate_already_present_in_another_account(qt_app) -> None:
    init_db()

    with get_session() as session:
        account_one = Account(name="YT One", platform="youtube")
        account_two = Account(name="YT Two", platform="youtube")
        session.add_all([account_one, account_two])
        session.flush()
        source_one = Source(
            account_id=account_one.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips-one",
            source_url="https://www.youtube.com/@clips-one",
            enabled=1,
            priority=100,
        )
        source_two = Source(
            account_id=account_two.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips-two",
            source_url="https://www.youtube.com/@clips-two",
            enabled=1,
            priority=100,
        )
        session.add_all([source_one, source_two])
        session.flush()
        session.add(
            ScrapeCandidate(
                scrape_source_url=source_one.source_url,
                source_url="https://www.youtube.com/watch?v=shared456",
                extractor="youtube",
                video_id="shared456",
                title="Shared candidate",
                channel_name="Clips One",
                source_id=source_one.id,
                account_id=account_one.id,
            )
        )
        session.commit()
        account_two_id = account_two.id
        source_two_id = source_two.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        source = next(
            item
            for item in window._load_sources_for_account(account_two_id)
            if item.id == source_two_id
        )

        created_count, refreshed_count, skipped_count = window._persist_scrape_candidates(
            account_id=account_two_id,
            source=source,
            scrape_run_id=1,
            candidates=[
                ScrapedVideoCandidate(
                    scrape_source_url=source.source_url,
                    source_url="https://www.youtube.com/watch?v=shared456",
                    extractor="youtube",
                    video_id="shared456",
                    title="Shared candidate duplicate",
                    channel_name="Clips Two",
                    published_at=None,
                )
            ],
        )

        assert created_count == 1
        assert refreshed_count == 0
        assert skipped_count == 0

        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == account_two_id)
                .all()
            )

        assert len(candidates) == 1
        assert candidates[0].video_id == "shared456"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_source_filter_and_summary_help_structure_source_management(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add_all(
            [
                Source(
                    account_id=account.id,
                    platform="youtube",
                    source_type="youtube_profile",
                    label="@enabled",
                    source_url="https://www.youtube.com/@enabled",
                    enabled=1,
                    priority=100,
                    last_run_status="completed",
                ),
                Source(
                    account_id=account.id,
                    platform="youtube",
                    source_type="youtube_profile",
                    label="@disabled",
                    source_url="https://www.youtube.com/@disabled",
                    enabled=0,
                    priority=200,
                ),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._run_table.isEnabled() is True
        assert window._source_table.rowCount() == 2
        assert "2 source(s): 1 enabled, 1 disabled" in window._source_summary_label.text()

        window._source_filter.setCurrentIndex(window._source_filter.findData("enabled"))
        qt_app.processEvents()

        assert window._source_table.rowCount() == 1
        assert window._source_table.item(0, 1).text() == "@enabled"

        window._source_table.selectRow(0)
        qt_app.processEvents()

        assert "Selected source: @enabled" in window._source_summary_label.text()
        assert "Last status: completed." in window._source_summary_label.text()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_scraping_page_uses_tabs_and_source_enabled_dropdown_updates_state(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@enabled",
            source_url="https://www.youtube.com/@enabled",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.commit()
        source_id = source.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._scrape_tabs.count() == 3
        assert window._scrape_tabs.tabText(0) == "Candidates"
        assert window._scrape_tabs.tabText(1) == "Sources"
        assert window._scrape_tabs.tabText(2) == "Activity"
        assert window._scrape_tabs.isTabVisible(1) is True
        assert window._scrape_tabs.isTabVisible(2) is False

        enabled_combo = window._source_table.cellWidget(0, 0)
        assert enabled_combo is not None
        enabled_combo.setCurrentIndex(1)
        qt_app.processEvents()

        with get_session() as session:
            source_row = session.get(Source, source_id)

        assert source_row is not None
        assert source_row.enabled == 0
        assert window._status_label.text() == "Disabled source."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_instagram_profile_source_scrape_selected_uses_safe_scrape_job(qt_app, monkeypatch) -> None:
    init_db()

    account_id = 0
    with get_session() as session:
        account = Account(name="IG Main", platform="instagram")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="instagram",
            source_type="instagram_profile",
            label="@meme.ig",
            source_url="https://www.instagram.com/meme.ig/",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.flush()
        account_id = account.id
        source_id = source.id
        session.commit()

    captured: dict[str, object] = {}

    def fake_start(self, job) -> None:  # noqa: ANN001, ARG001
        captured["job"] = job

    monkeypatch.setattr(MainWindow, "_start_scrape_job", fake_start)
    monkeypatch.setattr(MainWindow, "_confirm_instagram_scrape", lambda self, **kwargs: True)

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("scraping")
        qt_app.processEvents()
        window._scrape_tabs.setCurrentIndex(1)
        window._source_table.selectRow(0)
        qt_app.processEvents()

        window._on_scrape_selected_clicked()

        job = captured["job"]
        assert job.account_id == account_id
        assert job.source_ids == [source_id]
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_removing_download_resets_linked_candidate_state(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        source = Source(
            account_id=account.id,
            platform="youtube",
            source_type="youtube_profile",
            label="@clips",
            source_url="https://www.youtube.com/@clips",
            enabled=1,
            priority=100,
        )
        session.add(source)
        session.flush()
        item = DownloadItem(
            source_url="https://www.youtube.com/watch?v=queue123",
            title="Queued clip",
            status="downloaded",
            account_id=account.id,
        )
        session.add(item)
        session.flush()
        run = ScrapeRun(account_id=account.id, source_id=source.id, status="completed")
        session.add(run)
        session.flush()
        candidate = ScrapeCandidate(
            scrape_source_url=source.source_url,
            source_url=item.source_url,
            extractor="youtube",
            video_id="queue123",
            title="Queued candidate",
            channel_name="Clips Channel",
            source_id=source.id,
            scrape_run_id=run.id,
            account_id=account.id,
            state="downloaded",
            queued_download_item_id=item.id,
        )
        session.add(candidate)
        session.commit()
        item_id = item.id
        candidate_id = candidate.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._on_remove_clicked(item_id)
        qt_app.processEvents()

        with get_session() as session:
            candidate_row = session.get(ScrapeCandidate, candidate_id)

        assert candidate_row is not None
        assert candidate_row.queued_download_item_id is None
        assert candidate_row.state == "candidate"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


_PASTED_DRAFT_BLOB = """Title Option 1:
That kind of **sacrifice** that makes a silent hero feel heavier than an army.

Recommended Style 1:
Editorial Italic

Caption Option 1:
The glowing Mind Stone... and hope becomes a burden.

Marvel Zombies (2025), directed by Bryan Andrews, is a zombie superhero series.

#MarvelZombies #BlackPanther

Title Option 2:
The moment that **reframes** the fight.

Recommended Style 2:
Bold Rounded

Caption Option 2:
That Infinity Gauntlet... and Wakanda becomes the last line.

#MarvelZombies #Wakanda

Title Option 3:
One stone. One king. The whole ending becomes a **farewell**.

Recommended Style 3:
Cinema Normal

Caption Option 3:
T'Challa's raised claws... and sacrifice answers the silence.

#MarvelZombies #TChalla

Recommended Pick:
Title Option 1 + Caption Option 1

Why:
This pair keeps the emotional focus on sacrifice.

Selection Notes:
Option 1: Best for a tragic, character-led post.
Option 2: Best if you want higher readability.
Option 3: Best for a quieter rewatch-style post.
"""


def test_parse_pasted_smart_draft_extracts_all_sections() -> None:
    draft = parse_pasted_smart_draft(_PASTED_DRAFT_BLOB)

    assert len(draft.title_options) == 3
    assert draft.title_options[0] == (
        "That kind of **sacrifice** that makes a silent hero feel heavier than an army."
    )
    assert draft.title_options[2].endswith("**farewell**.")
    assert draft.recommended_styles == ["Editorial Italic", "Bold Rounded", "Cinema Normal"]
    assert draft.recommended_title_index == 0
    assert draft.recommended_caption_index == 0
    assert draft.reason == "This pair keeps the emotional focus on sacrifice."
    assert draft.option_notes[0] == "Best for a tragic, character-led post."
    assert draft.option_notes[2] == "Best for a quieter rewatch-style post."


def test_parse_pasted_smart_draft_preserves_caption_paragraphs() -> None:
    draft = parse_pasted_smart_draft(_PASTED_DRAFT_BLOB)

    caption_one = draft.caption_options[0]
    # The hook, the synopsis paragraph, and the hashtag line stay as separate
    # paragraphs (blank-line separated) and the hashtags survive at the end.
    assert "\n\n" in caption_one
    assert caption_one.startswith("The glowing Mind Stone")
    assert caption_one.rstrip().endswith("#MarvelZombies #BlackPanther")


def test_parse_pasted_smart_draft_handles_missing_footer() -> None:
    minimal = "Title Option 1:\nA simple title\n\nCaption Option 1:\nA simple caption"
    draft = parse_pasted_smart_draft(minimal)

    assert draft.title_options == ["A simple title"]
    assert draft.caption_options == ["A simple caption"]
    assert draft.recommended_title_index is None
    assert draft.reason is None


def test_parse_pasted_smart_draft_empty_text_is_safe() -> None:
    draft = parse_pasted_smart_draft("")

    assert draft.title_options == []
    assert draft.caption_options == []
    assert draft.recommended_title_index is None
