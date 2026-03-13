from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL


@dataclass(frozen=True)
class DownloadResult:
    extractor: str | None
    video_id: str | None
    title: str | None
    file_path: Path


def download_youtube_url(*, url: str, output_dir: Path) -> DownloadResult:
    output_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(output_dir / "%(extractor)s_%(id)s_%(title).80s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
    }

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

