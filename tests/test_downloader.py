from __future__ import annotations

from pathlib import Path

import pytest

from nicheflow_studio.downloader.instagram import download_instagram_url, _download_with_yt_dlp
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


def test_yt_dlp_options_do_not_attach_browser_cookies() -> None:
    options = _yt_dlp_options(Path.cwd() / "data" / "downloads")

    assert "cookiefile" not in options
    assert "cookiesfrombrowser" not in options


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


class _CapturingYoutubeDL:
    """Records the options it was constructed with and writes a dummy file."""

    calls: list[dict[str, object]] = []

    def __init__(self, options: dict[str, object]) -> None:
        type(self).calls.append(options)

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
        out = Path(self._out)
        out.write_bytes(b"video")
        return {"id": "X1", "title": "t", "extractor": "Instagram"}

    def prepare_filename(self, _info: dict[str, object]) -> str:
        return self._out


def test_instagram_downloader_threads_sourcing_cookies(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    _CapturingYoutubeDL.calls = []
    _CapturingYoutubeDL._out = str(tmp_path / "Instagram_X1_Video.mp4")
    monkeypatch.setattr("nicheflow_studio.downloader.instagram.YoutubeDL", _CapturingYoutubeDL)
    monkeypatch.setattr(
        "nicheflow_studio.downloader.instagram.instagram_yt_dlp_cookie_status",
        lambda: SimpleNamespace(cookiefile=str(tmp_path / "cookies.txt"), has_sessionid=True),
    )

    _download_with_yt_dlp(url="https://www.instagram.com/reel/X1/", output_dir=tmp_path)

    assert _CapturingYoutubeDL.calls
    assert _CapturingYoutubeDL.calls[0].get("cookiefile") == str(tmp_path / "cookies.txt")


def test_instagram_downloader_prefers_explicit_cookiefile(monkeypatch, tmp_path: Path) -> None:
    """An explicit per-account cookiefile must win over the shared export default."""
    from types import SimpleNamespace

    _CapturingYoutubeDL.calls = []
    _CapturingYoutubeDL._out = str(tmp_path / "Instagram_X1_Video.mp4")
    monkeypatch.setattr("nicheflow_studio.downloader.instagram.YoutubeDL", _CapturingYoutubeDL)
    # The shared-export default would return this; the explicit cookiefile should
    # override it and the default resolver should not even be needed.
    monkeypatch.setattr(
        "nicheflow_studio.downloader.instagram.instagram_yt_dlp_cookie_status",
        lambda: SimpleNamespace(cookiefile=str(tmp_path / "shared.txt"), has_sessionid=True),
    )

    _download_with_yt_dlp(
        url="https://www.instagram.com/reel/X1/",
        output_dir=tmp_path,
        cookiefile=str(tmp_path / "per-account.txt"),
    )

    assert _CapturingYoutubeDL.calls
    assert _CapturingYoutubeDL.calls[0].get("cookiefile") == str(tmp_path / "per-account.txt")


def test_instagram_downloader_omits_cookiefile_when_none(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    _CapturingYoutubeDL.calls = []
    _CapturingYoutubeDL._out = str(tmp_path / "Instagram_X1_Video.mp4")
    monkeypatch.setattr("nicheflow_studio.downloader.instagram.YoutubeDL", _CapturingYoutubeDL)
    monkeypatch.setattr(
        "nicheflow_studio.downloader.instagram.instagram_yt_dlp_cookie_status",
        lambda: SimpleNamespace(cookiefile=None, has_sessionid=False),
    )

    _download_with_yt_dlp(url="https://www.instagram.com/reel/X1/", output_dir=tmp_path)

    assert _CapturingYoutubeDL.calls
    assert "cookiefile" not in _CapturingYoutubeDL.calls[0]


def test_instagram_downloader_does_not_fall_back_to_instaloader(
    monkeypatch, tmp_path: Path
) -> None:
    class FailingYoutubeDL:
        def __init__(self, _options: dict[str, object]) -> None:
            pass

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
            assert download is True
            raise RuntimeError("yt-dlp extractor failed")

    monkeypatch.setattr("nicheflow_studio.downloader.instagram.YoutubeDL", FailingYoutubeDL)

    with pytest.raises(RuntimeError, match="yt-dlp extractor failed"):
        download_instagram_url(
            url="https://www.instagram.com/reel/DYdxGRpO7Am/",
            output_dir=tmp_path,
        )
