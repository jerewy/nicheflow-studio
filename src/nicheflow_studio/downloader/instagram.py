from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from nicheflow_studio.core.instagram_session import instagram_yt_dlp_cookie_status
from nicheflow_studio.downloader.youtube import _yt_dlp_options
from nicheflow_studio.downloader.yt_dlp_sidecar import (
    download_with_sidecar,
    yt_dlp_sidecar_path,
)

from yt_dlp import YoutubeDL

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
    # Instagram share URLs use both /reel/ (singular, common from the app) and
    # /reels/ (plural, often from the web). Accept both — the shortcode itself
    # is identical, only the route segment differs.
    if len(parts) < 2 or parts[0] not in {"reel", "reels", "p", "tv"}:
        return None

    shortcode = parts[1].strip()
    if not shortcode or _SHORTCODE_RE.fullmatch(shortcode) is None:
        return None
    return shortcode


def validate_instagram_media_url(url: str) -> str | None:
    if instagram_shortcode_from_url(url) is None:
        return "Use an Instagram Reel or post URL."
    return None


def _download_with_yt_dlp(*, url: str, output_dir: Path) -> InstagramDownloadResult:
    shortcode = instagram_shortcode_from_url(url)
    if shortcode is None:
        raise ValueError("Use an Instagram Reel or post URL.")

    output_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = _yt_dlp_options(output_dir)
    # Instagram now refuses anonymous downloads ("empty media response"), so reuse
    # the same saved sourcing cookies the Apify/instaloader scrape path already
    # uses. Without this the download fails even though scraping works.
    cookie_status = instagram_yt_dlp_cookie_status()
    cookiefile = cookie_status.cookiefile
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    sidecar = yt_dlp_sidecar_path()

    if sidecar is not None:
        info, file_path = download_with_sidecar(
            sidecar=sidecar,
            url=url,
            output_dir=output_dir,
            format_selector=str(ydl_opts["format"]),
            merge_output_format=ydl_opts.get("merge_output_format"),
            cookiefile=cookiefile,
        )
    else:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not isinstance(info, dict):
                raise RuntimeError("Instagram download returned no metadata.")
            file_path = Path(ydl.prepare_filename(info))
            if not file_path.exists():
                mp4_candidate = file_path.with_suffix(".mp4")
                if mp4_candidate.exists():
                    file_path = mp4_candidate

    return InstagramDownloadResult(
        extractor="instagram",
        video_id=str(info.get("id") or shortcode),
        title=info.get("title") if isinstance(info.get("title"), str) else None,
        file_path=file_path,
    )


def download_instagram_url(*, url: str, output_dir: Path) -> InstagramDownloadResult:
    return _download_with_yt_dlp(url=url, output_dir=output_dir)
