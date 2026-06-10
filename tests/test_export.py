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
    assert captured["title_font_name"] == "arial_bold"
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


def test_crop_from_override_converts_normalized_rect() -> None:
    probe = video.VideoProbe(width=1080, height=1920, duration_seconds=8.0)
    # Keep the middle half vertically, full width.
    crop = export_svc.crop_from_override({"x": 0.0, "y": 0.25, "w": 1.0, "h": 0.5}, probe)
    assert (crop.left, crop.top, crop.right, crop.bottom) == (0, 480, 0, 480)


def test_crop_from_override_keeps_at_least_two_pixels() -> None:
    probe = video.VideoProbe(width=10, height=10, duration_seconds=8.0)
    crop = export_svc.crop_from_override({"x": 0.9, "y": 0.9, "w": 0.1, "h": 0.1}, probe)
    assert video.output_dimensions(probe, crop) == (2, 2)


def test_save_get_clear_crop_override() -> None:
    item_id = _make_item(file_path="C:/x.mp4")
    assert export_svc.get_crop_override(item_id) is None

    rect = {"x": 0.1, "y": 0.2, "w": 0.8, "h": 0.6}
    saved = export_svc.save_crop_override(item_id, rect)
    assert saved["crop_override"] == rect
    assert export_svc.get_crop_override(item_id) == rect

    export_svc.clear_crop_override(item_id)
    assert export_svc.get_crop_override(item_id) is None


def test_crop_preview_frame_extracts_and_reuses_cached_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    calls: list[tuple[Path, Path]] = []

    def fake_extract(input_path: Path, output_path: Path) -> Path:
        calls.append((input_path, output_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"jpeg")
        return output_path

    monkeypatch.setattr(video, "extract_video_preview_frame", fake_extract)

    first = export_svc.crop_preview_frame(item_id)
    second = export_svc.crop_preview_frame(item_id)

    assert first == second
    assert first.read_bytes() == b"jpeg"
    assert calls == [(source.resolve(), first.resolve())]


def test_save_crop_override_rejects_invalid() -> None:
    item_id = _make_item(file_path="C:/x.mp4")
    with pytest.raises(ExportError):
        export_svc.save_crop_override(item_id, {"x": 0.5, "y": 0.0, "w": 0.8, "h": 0.5})  # x+w>1
    with pytest.raises(ExportError):
        export_svc.save_crop_override(item_id, {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.5})  # w=0


def test_export_uses_crop_override_over_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    rect = {"x": 0.0, "y": 0.25, "w": 1.0, "h": 0.5}
    export_svc.save_crop_override(item_id, rect)

    probe = video.VideoProbe(width=1080, height=1920, duration_seconds=8.0)
    captured: dict = {}
    monkeypatch.setattr(video, "probe_video", lambda _p: probe)
    # The auto-crop sentinel must NOT be used when an override exists.
    sentinel = video.CropSettings(left=999, top=999, right=999, bottom=999)
    monkeypatch.setattr(video, "suggest_title_replacement_crop", lambda _p, _probe: sentinel)

    def fake_export(**kwargs):
        captured.update(kwargs)
        return kwargs["output_path"]

    monkeypatch.setattr(video, "export_cropped_video", fake_export)

    export_svc.export_item(item_id)

    assert captured["crop"] == export_svc.crop_from_override(rect, probe)
    assert captured["crop"] != sentinel
