from __future__ import annotations

from pathlib import Path

from nicheflow_studio.db.models import DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.downloader.youtube import DownloadResult
from nicheflow_studio.queue import QueueManager


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN003
        fn(*args, **kwargs)
        return None


def test_enqueue_download_persists_success_and_callback(monkeypatch) -> None:
    output_file = Path.cwd() / "data" / "downloads" / "video.mp4"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.touch()
    callback_states: list[tuple[str, str | None]] = []

    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        assert url == "https://youtube.com/watch?v=abc123"
        assert output_dir.name == "downloads"
        return DownloadResult(
            extractor="youtube",
            video_id="abc123",
            title="Test title",
            file_path=output_file,
        )

    def callback(item: DownloadItem) -> None:
        callback_states.append((item.status, item.error_message))

    monkeypatch.setattr(QueueManager, "_executor", ImmediateExecutor())
    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    item_id = QueueManager.enqueue_download(
        url="https://youtube.com/watch?v=abc123",
        callback=callback,
    )

    with get_session() as session:
        item = session.get(DownloadItem, item_id)

    assert item is not None
    assert item.status == "downloaded"
    assert item.extractor == "youtube"
    assert item.video_id == "abc123"
    assert item.title == "Test title"
    assert item.file_path == str(output_file)
    assert item.error_message is None
    assert callback_states == [("downloaded", None)]


def test_enqueue_download_persists_failed_status_and_sanitized_error(monkeypatch) -> None:
    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        raise RuntimeError("network busted\nfull traceback should stay out of the UI")

    monkeypatch.setattr(QueueManager, "_executor", ImmediateExecutor())
    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    item_id = QueueManager.enqueue_download(url="https://youtube.com/watch?v=fail")

    with get_session() as session:
        item = session.get(DownloadItem, item_id)

    assert item is not None
    assert item.status == "failed"
    assert item.error_message == "network busted"
    assert item.file_path is None


def test_enqueue_download_strips_ansi_escape_codes_from_error(monkeypatch) -> None:
    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        raise RuntimeError("\x1b[0;31mERROR:\x1b[0m ffmpeg is not installed")

    monkeypatch.setattr(QueueManager, "_executor", ImmediateExecutor())
    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    item_id = QueueManager.enqueue_download(url="https://youtube.com/watch?v=ansi")

    with get_session() as session:
        item = session.get(DownloadItem, item_id)

    assert item is not None
    assert item.status == "failed"
    assert item.error_message == "ffmpeg is not installed."


def test_enqueue_download_maps_video_unavailable_error(monkeypatch) -> None:
    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        raise RuntimeError("ERROR: [youtube] gone123: Video unavailable")

    monkeypatch.setattr(QueueManager, "_executor", ImmediateExecutor())
    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    item_id = QueueManager.enqueue_download(url="https://youtube.com/watch?v=gone123")

    with get_session() as session:
        item = session.get(DownloadItem, item_id)

    assert item is not None
    assert item.status == "failed"
    assert item.error_message == "This video is unavailable on YouTube."


def test_enqueue_download_maps_requested_format_error(monkeypatch) -> None:
    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        raise RuntimeError(
            "ERROR: [youtube] abc123: Requested format is not available. Use --list-formats for a list of available formats"
        )

    monkeypatch.setattr(QueueManager, "_executor", ImmediateExecutor())
    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    item_id = QueueManager.enqueue_download(url="https://youtube.com/watch?v=abc123")

    with get_session() as session:
        item = session.get(DownloadItem, item_id)

    assert item is not None
    assert item.status == "failed"
    assert item.error_message == "This video format is not available right now. Try updating yt-dlp."


def test_enqueue_download_maps_http_403_error(monkeypatch) -> None:
    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        raise RuntimeError("ERROR: unable to download video data: HTTP Error 403: Forbidden")

    monkeypatch.setattr(QueueManager, "_executor", ImmediateExecutor())
    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    item_id = QueueManager.enqueue_download(url="https://youtube.com/watch?v=403abc")

    with get_session() as session:
        item = session.get(DownloadItem, item_id)

    assert item is not None
    assert item.status == "failed"
    assert item.error_message == "YouTube blocked the download request. Try updating yt-dlp."


def test_retry_item_clears_error_and_allows_success_after_failure(monkeypatch) -> None:
    output_file = Path.cwd() / "data" / "downloads" / "retry.mp4"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.touch()
    calls = {"count": 0}

    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary outage")
        return DownloadResult(
            extractor="youtube",
            video_id="retry123",
            title="Recovered",
            file_path=output_file,
        )

    monkeypatch.setattr(QueueManager, "_executor", ImmediateExecutor())
    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    item_id = QueueManager.enqueue_download(url="https://youtube.com/watch?v=retry123")

    with get_session() as session:
        failed_item = session.get(DownloadItem, item_id)
        assert failed_item is not None
        assert failed_item.status == "failed"
        assert failed_item.error_message == "temporary outage"

    assert QueueManager.retry_item(item_id) is True

    with get_session() as session:
        item = session.get(DownloadItem, item_id)

    assert item is not None
    assert item.status == "downloaded"
    assert item.extractor == "youtube"
    assert item.video_id == "retry123"
    assert item.title == "Recovered"
    assert item.file_path == str(output_file)
    assert item.error_message is None


def test_run_download_ignores_missing_item_without_crashing(monkeypatch) -> None:
    called = {"value": False}

    def fake_download(*, url: str, output_dir: Path) -> DownloadResult:
        called["value"] = True
        raise AssertionError("downloader should not be called for a missing row")

    monkeypatch.setattr("nicheflow_studio.queue.download_youtube_url", fake_download)

    QueueManager._run_download(item_id=999, url="https://youtube.com/watch?v=missing", callback=None)

    assert called["value"] is False
