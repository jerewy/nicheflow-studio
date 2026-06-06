from __future__ import annotations

from pathlib import Path

import pytest

from nicheflow_studio.db.models import DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import video
from nicheflow_studio.services import export as export_svc
from nicheflow_studio.services.export import ExportError


def _make_item(*, file_path: str | None, title_draft: str | None = "Chosen title") -> int:
    with get_session() as session:
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Source title",
            title_draft=title_draft,
            file_path=file_path,
            status="completed",
        )
        session.add(item)
        session.commit()
        return item.id


def _mock_video(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    monkeypatch.setattr(video, "probe_video", lambda _p: object())
    monkeypatch.setattr(
        video, "suggest_title_replacement_crop", lambda _p, _probe: video.CropSettings()
    )

    def fake_export(**kwargs):
        captured.update(kwargs)
        return kwargs["output_path"]

    monkeypatch.setattr(video, "export_cropped_video", fake_export)


def test_export_sets_processed_path_and_reports_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    captured: dict = {}
    _mock_video(monkeypatch, captured)

    events: list[tuple[float, str]] = []
    result = export_svc.export_item(item_id, progress=lambda f, m="": events.append((f, m)))

    assert result["item_id"] == item_id
    assert result["processed_path"]
    # The applied title is burned in.
    assert captured["title_text"] == "Chosen title"
    assert captured["title_layout"] == "top_band"
    # processed_path persisted on the item.
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        assert item.processed_path == result["processed_path"]
    # Progress was reported and reached completion.
    assert events[0][0] <= 0.1
    assert events[-1][0] == 1.0


def test_export_missing_file_path_raises() -> None:
    item_id = _make_item(file_path=None)
    with pytest.raises(ExportError):
        export_svc.export_item(item_id)


def test_export_missing_file_on_disk_raises(tmp_path: Path) -> None:
    item_id = _make_item(file_path=str(tmp_path / "gone.mp4"))
    with pytest.raises(ExportError):
        export_svc.export_item(item_id)


def test_export_unknown_item_raises() -> None:
    with pytest.raises(ExportError):
        export_svc.export_item(99999)
