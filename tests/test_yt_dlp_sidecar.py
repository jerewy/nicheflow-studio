from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nicheflow_studio.downloader import yt_dlp_sidecar


def test_sidecar_path_uses_packaged_bundle_root(monkeypatch, tmp_path: Path) -> None:
    sidecar = tmp_path / "yt-dlp.exe"
    sidecar.write_bytes(b"exe")
    monkeypatch.setattr(yt_dlp_sidecar.sys, "frozen", True, raising=False)
    monkeypatch.setattr(yt_dlp_sidecar.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert yt_dlp_sidecar.yt_dlp_sidecar_path() == sidecar


def test_sidecar_download_returns_metadata_and_after_move_path(monkeypatch, tmp_path: Path) -> None:
    downloaded = tmp_path / "Instagram_abc_clip.mp4"
    downloaded.write_bytes(b"video")

    def fake_run(command, **_kwargs):  # noqa: ANN001, ANN202
        path_file = Path(command[command.index("--print-to-file") + 2])
        path_file.write_text(str(downloaded), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout='{"extractor": "Instagram", "id": "abc", "title": "clip"}\n',
            stderr="",
        )

    monkeypatch.setattr(yt_dlp_sidecar.subprocess, "run", fake_run)

    info, file_path = yt_dlp_sidecar.download_with_sidecar(
        sidecar=tmp_path / "yt-dlp.exe",
        url="https://www.instagram.com/reel/abc/",
        output_dir=tmp_path,
        format_selector="best[ext=mp4]/best",
    )

    assert info["id"] == "abc"
    assert file_path == downloaded
    assert not list(tmp_path.glob("nicheflow-yt-dlp-*.txt"))
