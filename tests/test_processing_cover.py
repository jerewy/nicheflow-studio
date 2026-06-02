"""Tests for the reel-cover override controls on the processing page.

The heavy frame extraction (extract_cover_at / generate_cover) is monkeypatched,
so these exercise the UI wiring: timestamp sourcing, the source-mode guard, and
state updates.
"""
from __future__ import annotations

from pathlib import Path

import nicheflow_studio.app.main_window as mw
from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.db.session import init_db
from nicheflow_studio.processing.thumbnail import (
    CoverCandidate,
    CoverResult,
    cover_path_for_output,
)
from nicheflow_studio.processing.video import CropSettings


class _SourceItem:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


def _arrange_processed_item(window, monkeypatch, tmp_path: Path) -> Path:
    output = tmp_path / "reel_001.mp4"
    output.write_bytes(b"video")
    window._processing_last_output_path = output
    monkeypatch.setattr(
        window, "_processing_selected_item", lambda: _SourceItem(str(tmp_path / "clip.mp4"))
    )
    monkeypatch.setattr(window, "_processing_crop_settings", lambda: CropSettings())
    return output


def _cleanup(window) -> None:
    window._refresh_timer.stop()
    window._due_check_timer.stop()
    window._toast_timer.stop()
    window._hide_toast()
    window.close()


def test_set_cover_uses_current_source_preview_timestamp(qt_app, tmp_path, monkeypatch) -> None:
    init_db()
    window = MainWindow()
    try:
        window.show()
        output = _arrange_processed_item(window, monkeypatch, tmp_path)
        window._processing_preview_mode = "source"
        window._processing_preview_position_ms = 4200  # 4.2s

        captured: dict = {}

        def fake_extract(*, source_path, cover_path, crop, timestamp_seconds, probe=None):
            captured["timestamp"] = timestamp_seconds
            captured["cover_path"] = Path(cover_path)
            Path(cover_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cover_path).write_bytes(b"jpeg")
            return True

        monkeypatch.setattr(mw, "extract_cover_at", fake_extract)
        # Avoid probing the fake clip; crop resolution is exercised elsewhere.
        monkeypatch.setattr(mw, "resolve_cover_crop", lambda *_a, **_k: CropSettings())

        window._on_set_cover_from_frame_clicked()

        assert captured["timestamp"] == 4.2
        assert window._processing_cover_path == cover_path_for_output(output)
    finally:
        _cleanup(window)


def test_set_cover_blocked_when_previewing_processed_output(qt_app, tmp_path, monkeypatch) -> None:
    init_db()
    window = MainWindow()
    try:
        window.show()
        _arrange_processed_item(window, monkeypatch, tmp_path)
        # Processed output has burned-in text on every frame -> must be blocked.
        window._processing_preview_mode = "output"

        called = {"extract": False}
        monkeypatch.setattr(
            mw, "extract_cover_at", lambda **_k: called.__setitem__("extract", True) or True
        )

        window._on_set_cover_from_frame_clicked()

        assert called["extract"] is False
        assert window._processing_cover_path is None
    finally:
        _cleanup(window)


def test_reset_cover_regenerates_auto_pick(qt_app, tmp_path, monkeypatch) -> None:
    init_db()
    window = MainWindow()
    try:
        window.show()
        _arrange_processed_item(window, monkeypatch, tmp_path)
        auto_cover = tmp_path / "reel_001_cover.jpg"
        auto_cover.write_bytes(b"jpeg")

        monkeypatch.setattr(
            mw,
            "generate_cover",
            lambda **_k: CoverResult(image_path=auto_cover, timestamp_seconds=2.5),
        )

        window._on_reset_cover_clicked()

        assert window._processing_cover_path == auto_cover
    finally:
        _cleanup(window)


def test_clicking_cover_candidate_sets_stable_cover_path(qt_app, tmp_path, monkeypatch) -> None:
    init_db()
    window = MainWindow()
    try:
        window.show()
        output = _arrange_processed_item(window, monkeypatch, tmp_path)
        candidate_path = tmp_path / "reel_001_cover_candidate_01.jpg"
        candidate_path.write_bytes(b"candidate-jpeg")

        window._set_processing_cover(
            None,
            candidates=[
                CoverCandidate(
                    image_path=candidate_path,
                    timestamp_seconds=6.5,
                    score=12.0,
                )
            ],
        )

        window._on_cover_candidate_clicked(0)

        cover_path = cover_path_for_output(output)
        assert window._processing_cover_path == cover_path
        assert cover_path.read_bytes() == b"candidate-jpeg"
    finally:
        _cleanup(window)
