from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from nicheflow_studio.core.instagram_session import load_latest_instagram_session
from nicheflow_studio.downloader.youtube import _yt_dlp_options

from yt_dlp import YoutubeDL

try:
    import instaloader
except ImportError:  # pragma: no cover - exercised through runtime error path
    instaloader = None


_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class InstagramDownloadResult:
    extractor: str | None
    video_id: str | None
    title: str | None
    file_path: Path


def instagram_shortcode_from_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return None

    host = parsed.netloc.lower()
    if host not in _INSTAGRAM_HOSTS:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] not in {"reel", "p", "tv"}:
        return None

    shortcode = parts[1].strip()
    if not shortcode or _SHORTCODE_RE.fullmatch(shortcode) is None:
        return None
    return shortcode


def validate_instagram_media_url(url: str) -> str | None:
    if instagram_shortcode_from_url(url) is None:
        return "Use an Instagram Reel or post URL."
    return None


def _require_instaloader():
    if instaloader is None:
        raise RuntimeError("Instaloader is not installed. Run pip install -r requirements.txt.")
    return instaloader


def _make_loader() -> object:
    instaloader_module = _require_instaloader()
    loader = instaloader_module.Instaloader(
        download_pictures=False,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=True,
        compress_json=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=1,
        request_timeout=60.0,
        quiet=True,
        sanitize_paths=True,
    )
    load_latest_instagram_session(loader)
    return loader


def _downloaded_video_path(output_dir: Path, shortcode: str, before: set[Path]) -> Path | None:
    exact_candidates = sorted(output_dir.glob(f"*{shortcode}*.mp4"))
    for candidate in exact_candidates:
        if candidate.is_file() and candidate not in before:
            return candidate

    new_candidates = sorted(
        (
            candidate
            for candidate in output_dir.glob("*.mp4")
            if candidate.is_file() and candidate not in before
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if new_candidates:
        return new_candidates[0]
    return None


def _download_with_yt_dlp(*, url: str, output_dir: Path) -> InstagramDownloadResult:
    shortcode = instagram_shortcode_from_url(url)
    if shortcode is None:
        raise ValueError("Use an Instagram Reel or post URL.")

    output_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = _yt_dlp_options(output_dir)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = Path(ydl.prepare_filename(info))
        if not file_path.exists():
            mp4_candidate = file_path.with_suffix(".mp4")
            if mp4_candidate.exists():
                file_path = mp4_candidate

    return InstagramDownloadResult(
        extractor="instagram",
        video_id=str(info.get("id") or shortcode),
        title=info.get("title"),
        file_path=file_path,
    )


def _download_with_instaloader(*, url: str, output_dir: Path) -> InstagramDownloadResult:
    shortcode = instagram_shortcode_from_url(url)
    if shortcode is None:
        raise ValueError("Use an Instagram Reel or post URL.")

    instaloader_module = _require_instaloader()
    output_dir.mkdir(parents=True, exist_ok=True)
    before = {candidate for candidate in output_dir.glob("*.mp4") if candidate.is_file()}

    loader = _make_loader()
    post = instaloader_module.Post.from_shortcode(loader.context, shortcode)
    if not post.is_video:
        raise ValueError("Instagram URL is not a video post or Reel.")

    loader.download_post(post, target=str(output_dir))
    file_path = _downloaded_video_path(output_dir, shortcode, before)
    if file_path is None:
        raise RuntimeError("Instagram download finished but no MP4 file was found.")

    caption = post.caption or ""
    title = caption.splitlines()[0][:120] if caption else None
    return InstagramDownloadResult(
        extractor="instagram",
        video_id=post.shortcode,
        title=title,
        file_path=file_path,
    )


def download_instagram_url(*, url: str, output_dir: Path) -> InstagramDownloadResult:
    try:
        return _download_with_yt_dlp(url=url, output_dir=output_dir)
    except Exception:
        return _download_with_instaloader(url=url, output_dir=output_dir)
