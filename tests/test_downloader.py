from __future__ import annotations

from pathlib import Path

from nicheflow_studio.downloader.youtube import _yt_dlp_options


def test_yt_dlp_options_prefer_merged_best_quality_when_ffmpeg_available(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.downloader.youtube._ffmpeg_available", lambda: True)

    options = _yt_dlp_options(Path.cwd() / "data" / "downloads")

    assert options["format"] == (
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    )
    assert options["merge_output_format"] == "mp4"


def test_yt_dlp_options_fall_back_to_progressive_mp4_without_ffmpeg(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.downloader.youtube._ffmpeg_available", lambda: False)

    options = _yt_dlp_options(Path.cwd() / "data" / "downloads")

    assert options["format"] == "best[ext=mp4]/best"
    assert "merge_output_format" not in options
