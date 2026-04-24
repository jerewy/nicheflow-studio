from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtWidgets import QLabel

from nicheflow_studio.app.main_window import MainWindow, SuggestCropJobConfig
from nicheflow_studio.processing.video import CropSettings, VideoProbe
from nicheflow_studio.db.models import Account, DownloadItem, ScrapeCandidate, ScrapeRun, Source
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.scraper.youtube import ScrapedVideoCandidate


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
        created_count, refreshed_count, skipped_count, rejected_count = window._run_scrape_for_source(
            account_id=job.account_id,
            source=source,
            keywords=job.keywords,
            max_items=job.max_items,
            max_age_days=job.max_age_days,
            min_view_count=job.min_view_count,
            min_like_count=job.min_like_count,
            weights=job.weights,
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
            "reasons": ["removed detected border margins", "trimmed repeated OCR text near the frame edges"],
            "used_border_detection": True,
            "used_ocr": True,
        }
    )


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
            "caption_options": ["This elephant stole the whole clip", "Wait for the elephant reveal"],
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
            },
        }
    )


def _fail_draft_job_immediately(window: MainWindow, job) -> None:  # noqa: ANN001
    window._processing_in_progress = True
    window._on_transcript_draft_failed("No speech was detected in this video.")


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
        assert window._processing_generate_drafts_button.text() == "Generate Drafts"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window.close()


def test_failed_item_shows_error_in_output_and_detail_panel(qt_app) -> None:
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

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._table.rowCount() == 1
        assert window._table.item(0, 5).text() == "yt-dlp could not fetch metadata"
        assert window._status_label.text() == "Last failure: yt-dlp could not fetch metadata"
        assert window._detail_panel.isVisible() is False

        window._table.selectRow(0)
        qt_app.processEvents()

        assert window._detail_panel.isVisible() is True
        assert window._detail_fields["status"].text() == "failed"
        assert window._detail_fields["review"].text() == "ready"
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
        assert window._candidate_table.item(0, 6).text() == "Intake clip"
        assert window._scrape_summary_label.text().startswith("1 of 1 source(s) enabled, 1 keyword(s)")
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
        assert window._scrape_summary_label.text().startswith("1 of 1 source(s) enabled, 0 keyword(s)")
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
        assert str(captured["output_path"]).endswith("clip_cropped.mp4")
        assert captured["crop"].top == 24
        assert captured["crop"].bottom == 96
        assert captured["title_text"] == "Hook Title"
        assert captured["title_font_size"] == window._processing_title_font_size.value()
        assert window._processing_progress_label.text() == "Processing complete."
        assert window._status_label.text() == "Processed video saved to clip_cropped.mp4."
        assert window._processing_latest_output_label.text() == "clip_cropped.mp4"
        assert window._processing_open_latest_output_button.isEnabled() is True
        assert window._processing_preview_mode_combo.findData("output") >= 0
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


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

        assert loaded_paths[-1].name == "clip_cropped.mp4"
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

        assert window._processing_crop_settings() == CropSettings(left=18, top=24, right=12, bottom=96)
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
        assert window._processing_caption_draft_input.toPlainText() == "This is a generated caption draft."
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
        window._on_save_text_drafts_clicked()
        qt_app.processEvents()

        with get_session() as session:
            saved = session.query(DownloadItem).one()

        assert saved.title_style_preset == "boxed_banner"
        assert "\"font_size\": 72" in (saved.title_style_config or "")
        assert "\"font_name\": \"impact\"" in (saved.title_style_config or "")
        assert "\"background\": \"light\"" in (saved.title_style_config or "")

        window._apply_refresh(force=True)
        window._set_current_page("processing")
        qt_app.processEvents()

        assert window._processing_title_style_combo.currentData() == "boxed_banner"
        assert window._processing_title_font_size.value() == 72
        assert window._processing_title_font_combo.currentData() == "impact"
        assert window._processing_title_color_input.text() == "#FFD700"
        assert window._processing_title_background_combo.currentData() == "light"
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
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert probe_calls == []

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
        assert window._processing_smart_option_caption_inputs[0].toPlainText() == "This elephant stole the whole clip"
        assert window._processing_title_draft_input.text() == "Elephant Chaos"
        assert window._processing_caption_draft_input.toPlainText() == "This elephant stole the whole clip"
        window._apply_refresh(force=True)
        window._set_current_page("processing")
        qt_app.processEvents()
        assert window._processing_title_draft_input.text() == "Elephant Chaos"
        assert window._processing_caption_draft_input.toPlainText() == "This elephant stole the whole clip"

        window._on_processing_smart_option_clicked(1)
        assert window._processing_title_draft_input.text() == "Zoo Hook"

        window._on_processing_smart_option_clicked(1)
        assert window._processing_caption_draft_input.toPlainText() == "Wait for the elephant reveal"

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
        assert "frame_count" in window._processing_eval_meta_input.toPlainText()
        assert "scene_summary" in window._processing_eval_vision_input.toPlainText()
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
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_generate_drafts_auto_chains_into_smart_drafts(
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
                source_url="https://youtube.com/watch?v=chain123",
                title="Zoo source",
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

        assert window._processing_title_draft_input.text() == "Elephant Chaos"
        assert "generated transcript" in window._processing_transcript_input.toPlainText()
        assert len(window._processing_smart_option_buttons) == 3
        assert "smart draft options" in window._processing_draft_status_label.text().lower()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_processing_generate_drafts_falls_back_to_metadata_only_smart_drafts(
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
    monkeypatch.setattr(MainWindow, "_start_transcript_draft_job", _fail_draft_job_immediately)
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
        assert window._processing_title_draft_input.text() == "Elephant Chaos"
        assert window._processing_caption_draft_input.toPlainText() == "This elephant stole the whole clip"
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

    def fake_enqueue_download(*, url: str, account_id: int | None, callback=None) -> int:  # noqa: ANN001
        captured["url"] = url
        captured["account_id"] = account_id
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

    def fake_enqueue_download(*, url: str, account_id: int | None, callback=None) -> int:  # noqa: ANN001
        captured["url"] = url
        captured["account_id"] = account_id
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

    def fake_enqueue_download(*, url: str, account_id: int | None, callback=None) -> int:  # noqa: ANN001
        captured["url"] = url
        captured["account_id"] = account_id
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
        assert window._candidate_table.item(0, 7).text() != "(none)"
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
            {"sources": 1, "created": 0, "refreshed": 0, "skipped": 0, "rejected": 0, "auto_queued": 0}
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


def test_scrape_progress_bar_tracks_source_level_progress(qt_app) -> None:
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
        qt_app.processEvents()

        job = window._build_scrape_job_for_all_enabled_sources()
        assert job is not None

        window._scrape_in_progress = True
        window._prepare_scrape_progress(total_sources=len(job.source_ids))
        window._scrape_progress_label.setText("Preparing scrape job...")
        qt_app.processEvents()

        assert window._scrape_progress_bar.isHidden() is False
        assert window._scrape_progress_bar.maximum() == 1
        assert window._scrape_progress_bar.value() == 0

        window._on_scrape_progress({"current": 1, "total": 1, "source_label": "@clips"})
        qt_app.processEvents()

        assert window._scrape_progress_label.text() == "Scraping 1/1: @clips"
        assert window._scrape_progress_bar.format() == "0/1 sources complete"

        window._on_scrape_source_completed(
            {"source_label": "@clips", "created": 1, "refreshed": 0, "skipped": 0, "rejected": 0}
        )
        qt_app.processEvents()

        assert window._scrape_progress_bar.value() == 1
        assert window._scrape_progress_bar.format() == "1/1 sources complete"

        window._on_scrape_completed(
            {"sources": 1, "created": 1, "refreshed": 0, "skipped": 0, "rejected": 0, "auto_queued": 0}
        )
        qt_app.processEvents()

        assert window._scrape_progress_bar.isHidden() is True
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
        assert window._status_label.text() == "Create and select an account target to use the library."
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

        assert window._status_label.text() == "Create and select an account target first."
        assert window._toast_label.text() == "Create and select an account target first."
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
        assert window._status_label.text() == "Only YouTube and YouTube Shorts URLs are supported right now."
        assert window._toast_label.text() == "Only YouTube and YouTube Shorts URLs are supported right now."
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

        def fake_enqueue_download(*, url: str, account_id: int | None, callback=None) -> int:  # noqa: ANN001
            captured["url"] = url
            captured["account_id"] = account_id
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
        assert window._status_label.text() == "Channel and profile URLs are not supported right now."
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
        assert window._status_label.text() == "This video is already in this account library. Use Redownload from history."
        assert window._toast_label.text() == "This video is already in this account library. Use Redownload from history."
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


def test_download_rejects_failed_duplicate_and_points_to_retry(monkeypatch, qt_app) -> None:
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

        window._url_input.setText("https://www.youtube.com/watch?v=fail123")
        window._on_download_clicked()
        qt_app.processEvents()

        assert enqueue_calls == []
        assert window._status_label.text() == "This video already failed for this account. Use Retry from history."
        assert window._toast_label.text() == "This video already failed for this account. Use Retry from history."
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
        assert window._table.item(0, 1).text() == "ignored"

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
        assert window._detail_panel.isVisible() is True

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
            item = session.query(DownloadItem).filter(DownloadItem.source_url.contains("assign")).one()
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
        assert window._account_main_new_button.text() == "New Account"
        assert window._account_main_actions.isVisible() is True
        assert window._account_form_panel.isVisible() is False
        assert window._account_delete_panel.isVisible() is False
        assert window._account_save_button.text() == "Create Account"
        assert window._account_delete_button.isEnabled() is False
        assert window._account_delete_button.text() == "Delete Selected Account"

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
        assert window._detail_assign_button.text() == "Save Account Assignment"
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

        assert window._account_picker.itemText(0) == "Select account to edit..."

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
        assert window._account_save_button.text() == "Save Account Changes"

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

        window._toggle_account_sidebar()
        qt_app.processEvents()

        assert window._account_panel.isVisible() is True
        assert window._sidebar_toggle_button.isChecked() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_sidebar_brand_is_display_only(qt_app) -> None:
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert isinstance(window._sidebar_brand, QLabel)
        assert window._sidebar_brand.objectName() == "sidebarBrand"
        assert window._sidebar_brand.text() == "NicheFlow"
        assert window._sidebar_brand.alignment() == (
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        assert window._sidebar_brand.minimumHeight() >= 16
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_sidebar_selected_state_and_compact_width(qt_app) -> None:
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

        assert window._sidebar_panel.width() >= 72
        assert window._sidebar_panel.width() <= 84
        assert window._module_buttons["downloads"].property("selected") is True
        assert window._module_buttons["scraping"].property("selected") is False
        assert window._sidebar_toggle_button.property("selected") is False
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
        workspace_rect = window._workspace_content.parentWidget().geometry()

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
            saved = session.query(DownloadItem).filter(DownloadItem.source_url.contains("reject")).one()

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

        assert window._table.item(0, 1).text() == "kept"
        assert window._detail_fields["review"].text() == "kept"
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
                .filter(DownloadItem.source_url.in_([
                    "https://youtube.com/watch?v=batch1",
                    "https://youtube.com/watch?v=batch2",
                ]))
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
        assert window._account_niche_input.text() == "draft niche"
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


def test_accounts_page_keeps_account_manager_visible(qt_app) -> None:
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

        assert window._current_page == "accounts"
        assert window._account_panel.isVisible() is True
        assert window._sidebar_toggle_button.isEnabled() is False
        assert window._runtime_fields["data_dir"].text().endswith("data")
        assert window._export_backup_button.text() == "Create Backup Zip"
        assert window._restore_backup_button.text() == "Restore Backup Zip"

        window._set_current_page("downloads")
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

        assert window._run_table.minimumHeight() >= 320
        assert window._run_table.maximumHeight() >= 1000
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_unassigned_download_remains_visible_in_library_workspace(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=unassigned1",
                title="Unassigned clip",
                status="downloaded",
                account_id=None,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        assert window._table.rowCount() == 1
        assert window._table.item(0, 3).text() == "Unassigned clip"
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
        assert window._candidate_table.item(0, 6).text() == "Downloaded clip"
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

        window._candidate_state_filter.setCurrentIndex(window._candidate_state_filter.findData("ignored"))
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
        source = next(item for item in window._load_sources_for_account(account_two_id) if item.id == source_id)

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
        source = next(item for item in window._load_sources_for_account(account_two_id) if item.id == source_two_id)

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
        assert window._scrape_tabs.tabText(0) == "Sources"
        assert window._scrape_tabs.tabText(1) == "Candidates"
        assert window._scrape_tabs.tabText(2) == "Runs"

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
