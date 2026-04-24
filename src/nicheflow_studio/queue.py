from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from nicheflow_studio.core.paths import downloads_dir
from nicheflow_studio.db.models import DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.downloader.youtube import download_youtube_url


@dataclass(frozen=True)
class QueueConfig:
    max_workers: int = 2


_logger = logging.getLogger(__name__)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _sanitize_error_message(exc: Exception) -> str:
    message = _ANSI_ESCAPE_RE.sub("", str(exc)).strip()
    if not message:
        return exc.__class__.__name__

    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if not first_line:
        first_line = exc.__class__.__name__

    normalized = first_line.removeprefix("ERROR:").strip()
    lowered = normalized.lower()

    if "video unavailable" in lowered:
        return "This video is unavailable on YouTube."
    if "private video" in lowered:
        return "This video is private and cannot be downloaded."
    if "sign in to confirm your age" in lowered or "confirm your age" in lowered:
        return "This video requires an age-confirmed YouTube session."
    if "members-only" in lowered or "join this channel" in lowered:
        return "This video is members-only and cannot be downloaded with the current setup."
    if "requested format is not available" in lowered:
        return "This video format is not available right now. Try updating yt-dlp."
    if "unable to download video data" in lowered or "http error 403" in lowered:
        return "YouTube blocked the download request. Try updating yt-dlp."
    if "ffmpeg is not installed" in lowered:
        return "ffmpeg is not installed."

    return normalized[:200]


class QueueManager:
    _executor = ThreadPoolExecutor(max_workers=QueueConfig().max_workers)

    @classmethod
    def enqueue_download(
        cls,
        url: str,
        callback: Callable[[DownloadItem], None] | None = None,
        account_id: int | None = None,
    ) -> int:
        with get_session() as session:
            item = DownloadItem(source_url=url, status="queued", account_id=account_id)
            session.add(item)
            session.commit()
            item_id = item.id

        cls._executor.submit(cls._run_download, item_id, url, callback)
        return item_id

    @classmethod
    def retry_item(cls, item_id: int) -> bool:
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is None:
                return False
            source_url = item.source_url
            item.status = "queued"
            item.file_path = None
            item.extractor = None
            item.video_id = None
            item.title = None
            item.error_message = None
            session.commit()

        cls._executor.submit(cls._run_download, item_id, source_url, None)
        return True

    @classmethod
    def _run_download(cls, item_id: int, url: str, callback: Callable[[DownloadItem], None] | None) -> None:
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is None:
                _logger.warning("download item %s disappeared before processing", item_id)
                return
            item.status = "downloading"
            session.commit()

        try:
            result = download_youtube_url(url=url, output_dir=downloads_dir())
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None:
                    _logger.warning("download item %s missing after download", item_id)
                    return
                item.extractor = result.extractor
                item.video_id = result.video_id
                item.title = result.title
                item.file_path = str(result.file_path)
                item.error_message = None
                item.status = "downloaded"
                session.commit()
        except Exception as exc:  # noqa: BLE001
            _logger.exception("download failed for %s", url)
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None:
                    return
                item.status = "failed"
                item.error_message = _sanitize_error_message(exc)
                session.commit()
        finally:
            if callback:
                with get_session() as session:
                    item = session.get(DownloadItem, item_id)
                    if item:
                        callback(item)
