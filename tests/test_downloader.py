from __future__ import annotations

from pathlib import Path

from nicheflow_studio.downloader.instagram import _download_with_yt_dlp
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


def test_instagram_downloader_uses_yt_dlp_first(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            calls.append(options)

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            assert url == "https://www.instagram.com/p/DYdxGRpO7Am/"
            assert download is True
            output_file = tmp_path / "Instagram_DYdxGRpO7Am_Video_by_meme.ig.mp4"
            output_file.write_bytes(b"video")
            self.output_file = output_file
            return {
                "id": "DYdxGRpO7Am",
                "title": "Video by meme.ig",
                "extractor": "Instagram",
            }

        def prepare_filename(self, info: dict[str, object]) -> str:
            return str(self.output_file)

    monkeypatch.setattr("nicheflow_studio.downloader.instagram.YoutubeDL", FakeYoutubeDL)

    result = _download_with_yt_dlp(
        url="https://www.instagram.com/p/DYdxGRpO7Am/",
        output_dir=tmp_path,
    )

    assert calls
    assert result.extractor == "instagram"
    assert result.video_id == "DYdxGRpO7Am"
    assert result.title == "Video by meme.ig"
    assert result.file_path.read_bytes() == b"video"
