from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL

from nicheflow_studio.core.media_tools import ffmpeg_binary


@dataclass(frozen=True)
class DownloadResult:
    extractor: str | None
    video_id: str | None
    title: str | None
    file_path: Path


def _ffmpeg_available() -> bool:
    return ffmpeg_binary() is not None


def _yt_dlp_options(output_dir: Path) -> dict[str, object]:
    output_template = output_dir / "%(extractor)s_%(id)s_%(title).80s.%(ext)s"
    options: dict[str, object] = {
        "outtmpl": str(output_template),
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
    }
    if _ffmpeg_available():
        # Prefer the highest-quality separate streams when ffmpeg can merge them.
        options["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
        options["merge_output_format"] = "mp4"
    else:
        # Fall back to a single-file MP4 when ffmpeg is unavailable.
        options["format"] = "best[ext=mp4]/best"
    return options


def download_youtube_url(*, url: str, output_dir: Path) -> DownloadResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = _yt_dlp_options(output_dir)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = Path(ydl.prepare_filename(info))

        if not file_path.exists():
            mp4_candidate = file_path.with_suffix(".mp4")
            if mp4_candidate.exists():
                file_path = mp4_candidate

    return DownloadResult(
        extractor=info.get("extractor"),
        video_id=info.get("id"),
        title=info.get("title"),
        file_path=file_path,
    )
