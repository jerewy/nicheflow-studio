from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pytest

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import video
from nicheflow_studio.services import export as export_svc
from nicheflow_studio.services import publishing
from nicheflow_studio.services.export import ExportError


def _make_item(
    *,
    file_path: str | None,
    title_draft: str | None = "Chosen title",
    caption_draft: str | None = "A final caption.",
    auto_schedule_on_export: bool = False,
    upload_schedule_slots: str | None = None,
) -> int:
    with get_session() as session:
        account = Account(
            name="Export Account",
            platform="instagram",
            auto_schedule_on_export=auto_schedule_on_export,
            upload_schedule_slots=upload_schedule_slots,
        )
        session.add(account)
        session.flush()
        item = DownloadItem(
            source_url="https://instagram.com/reel/abc",
            title="Source title",
            title_draft=title_draft,
            # Scheduling validates finalized draft text; exports in these tests
            # should pass that gate unless a test clears it explicitly.
            caption_draft=caption_draft,
            file_path=file_path,
            status="completed",
            account_id=account.id,
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


def test_export_auto_schedules_next_open_slot_when_flag_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(
        file_path=str(source),
        auto_schedule_on_export=True,
        upload_schedule_slots="09:00",
    )
    _mock_video(monkeypatch, {})

    # Freeze "now" (and the jitter RNG) so the chosen slot never depends on the
    # wall-clock hour the test runs at. ``export_item`` reaches the scheduler via
    # ``publishing.auto_schedule_for_publish``, which exposes a ``now=``/``rng=``
    # seam; patch the call site to feed it deterministic values.
    #
    # At 06:00 UTC no 09:00 slot has passed today, so the scheduler takes the
    # "next open slot" path (not the near-now catch-up path). The already-booked
    # job sits exactly on today's 09:00 slot, so the new post must skip the
    # collision and land in the next open 09:00 slot (tomorrow).
    fixed_now = dt.datetime(2026, 6, 15, 6, 0, tzinfo=dt.timezone.utc)
    occupied = dt.datetime(2026, 6, 15, 9, 0, tzinfo=dt.timezone.utc)
    real_auto_schedule = publishing.auto_schedule_for_publish
    monkeypatch.setattr(
        publishing,
        "auto_schedule_for_publish",
        lambda iid, **_kw: real_auto_schedule(iid, now=fixed_now, rng=random.Random(0)),
    )

    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        session.add(
            UploadJob(
                account_id=item.account_id,
                processed_path="C:/processed/already-booked.mp4",
                scheduled_at=occupied,
                status="scheduled",
            )
        )
        session.commit()

    result = export_svc.export_item(item_id)

    assert "warning" not in result
    with get_session() as session:
        jobs = session.query(UploadJob).filter(UploadJob.download_item_id == item_id).all()
        assert len(jobs) == 1
        assert jobs[0].status == "scheduled"
        scheduled = jobs[0].scheduled_at.replace(tzinfo=dt.timezone.utc)
        # Landed in the next open slot AFTER the booked one, not stacked on it.
        assert scheduled > occupied
        assert (scheduled - occupied).total_seconds() > 20 * 60


def test_export_returns_cloud_status_from_auto_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(
        file_path=str(source),
        auto_schedule_on_export=True,
        upload_schedule_slots="09:00",
    )
    _mock_video(monkeypatch, {})
    monkeypatch.setattr(
        publishing,
        "auto_schedule_for_publish",
        lambda _item_id: {
            "job_id": 7,
            "status": "cloud",
            "scheduled_at": "2026-06-16T02:00:00+00:00",
            "created": True,
        },
    )

    result = export_svc.export_item(item_id)

    assert result["scheduled_publish"]["status"] == "cloud"


def test_export_does_not_schedule_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), upload_schedule_slots="09:00")
    _mock_video(monkeypatch, {})

    result = export_svc.export_item(item_id)

    assert "warning" not in result
    with get_session() as session:
        assert session.query(UploadJob).count() == 0


def test_export_with_auto_schedule_and_no_slots_returns_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), auto_schedule_on_export=True)
    _mock_video(monkeypatch, {})

    result = export_svc.export_item(item_id)

    assert "schedule slots" in result["warning"].lower()
    assert result["processed_path"]
    with get_session() as session:
        assert session.query(UploadJob).count() == 0


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
