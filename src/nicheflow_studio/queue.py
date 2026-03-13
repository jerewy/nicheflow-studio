from __future__ import annotations

import logging
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


class QueueManager:
    _executor = ThreadPoolExecutor(max_workers=QueueConfig().max_workers)

    @classmethod
    def enqueue_download(cls, url: str, callback: Callable[[DownloadItem], None] | None = None) -> int:
        with get_session() as session:
            item = DownloadItem(source_url=url, status="queued")
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
            item.status = "queued"
            item.file_path = None
            item.extractor = None
            item.video_id = None
            item.title = None
            session.commit()

        cls._executor.submit(cls._run_download, item_id, item.source_url, None)
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
                item.status = "downloaded"
                session.commit()
        except Exception as exc:  # noqa: BLE001
            _logger.exception("download failed for %s", url)
            with get_session() as session:
                item = session.get(DownloadItem, item_id)
                if item is None:
                    return
                item.status = "failed"
                session.commit()
        finally:
            if callback:
                with get_session() as session:
                    item = session.get(DownloadItem, item_id)
                    if item:
                        callback(item)
