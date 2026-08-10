from __future__ import annotations

import datetime as dt
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.processing import video
from nicheflow_studio.services import export as export_svc
from nicheflow_studio.services import publishing, ui_settings
from nicheflow_studio.services.export import ExportError


def _make_item(
    *,
    file_path: str | None,
    title_draft: str | None = "Chosen title",
    caption_draft: str | None = "A final caption.",
    auto_schedule_on_export: bool = False,
    upload_schedule_slots: str | None = None,
    instagram_handle: str | None = None,
) -> int:
    with get_session() as session:
        account = Account(
            name="Export Account",
            platform="instagram",
            auto_schedule_on_export=auto_schedule_on_export,
            upload_schedule_slots=upload_schedule_slots,
            instagram_handle=instagram_handle,
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


def test_export_translates_render_cancel_to_job_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import threading

    from nicheflow_studio.services.jobs import JobCanceled

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))

    monkeypatch.setattr(video, "probe_video", lambda _p: object())
    monkeypatch.setattr(
        video, "suggest_title_replacement_crop", lambda _p, _probe: video.CropSettings()
    )

    def canceled_render(**_kwargs):
        raise video.RenderCanceled("Export canceled during rendering.")

    monkeypatch.setattr(video, "export_cropped_video", canceled_render)

    # A killed render surfaces as JobCanceled (a clean cancel), not ExportError,
    # and never persists a processed_path for the half-rendered clip.
    with pytest.raises(JobCanceled):
        export_svc.export_item(item_id, cancel_event=threading.Event())

    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        assert item.processed_path is None


def test_export_aborts_before_rendering_when_already_canceled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import threading

    from nicheflow_studio.services.jobs import JobCanceled

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))

    def fail_if_called(**_kwargs):
        raise AssertionError("render must not start once cancellation is requested")

    monkeypatch.setattr(video, "export_cropped_video", fail_if_called)

    event = threading.Event()
    event.set()
    with pytest.raises(JobCanceled):
        export_svc.export_item(item_id, cancel_event=event)


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


def test_export_covers_watermark_and_reports_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), instagram_handle="@ourbrand")
    _mock_video(monkeypatch, {})

    captured: dict = {}

    def fake_replace(video_path, *, replacement_text, output_path, **_kwargs):
        captured["replacement_text"] = replacement_text
        Path(output_path).write_bytes(b"covered")  # produce a covered file to swap in
        return SimpleNamespace(
            output_path=output_path,
            region=SimpleNamespace(text="@foreignsource"),
            replacement_text=replacement_text,
            skipped_reason=None,
        )

    monkeypatch.setattr(export_svc, "replace_detected_watermark", fake_replace)

    result = export_svc.export_item(item_id)

    # The publishing account's own handle is what stamps over the foreign mark.
    assert captured["replacement_text"] == "@ourbrand"
    assert result["watermark_replaced"] is True
    assert result["watermark_detected_text"] == "@foreignsource"
    assert result["watermark_skipped_reason"] is None
    assert result["processed_path"]
    # The covered file replaced the rendered output at the same path.
    assert Path(result["processed_path"]).read_bytes() == b"covered"


def test_export_reports_skip_when_no_watermark_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), instagram_handle="@ourbrand")
    _mock_video(monkeypatch, {})

    def fake_replace(video_path, *, replacement_text, output_path, **_kwargs):
        return SimpleNamespace(
            output_path=None,
            region=None,
            replacement_text=replacement_text,
            skipped_reason="no watermark detected",
        )

    monkeypatch.setattr(export_svc, "replace_detected_watermark", fake_replace)

    result = export_svc.export_item(item_id)

    assert result["watermark_replaced"] is False
    assert result["watermark_detected_text"] is None
    assert result["watermark_skipped_reason"] == "no watermark detected"
    assert result["processed_path"]


def test_export_succeeds_when_watermark_step_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crash in the OCR/FFmpeg watermark pipeline must never fail the export."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), instagram_handle="@ourbrand")
    _mock_video(monkeypatch, {})

    def boom(*_args, **_kwargs):
        raise RuntimeError("tesseract exploded")

    monkeypatch.setattr(export_svc, "replace_detected_watermark", boom)

    result = export_svc.export_item(item_id)

    assert result["processed_path"]  # export still succeeded
    assert result["watermark_replaced"] is False
    assert result["watermark_skipped_reason"] == "watermark step failed"


def test_export_skips_watermark_when_account_has_no_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No account @handle → the (expensive) detection pass is skipped entirely."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))  # no instagram_handle
    _mock_video(monkeypatch, {})

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("watermark detection must not run without a publishing handle")

    monkeypatch.setattr(export_svc, "replace_detected_watermark", fail_if_called)

    result = export_svc.export_item(item_id)

    assert result["watermark_replaced"] is False
    assert result["watermark_skipped_reason"] == "no publishing handle set"


def test_export_skips_watermark_when_the_scan_is_turned_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scan is ~40s of a ~45s export and finds nothing on unwatermarked
    sources, so it is switchable — and switching it off must skip the detection
    pass itself, not just the cover step."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), instagram_handle="@ourbrand")
    _mock_video(monkeypatch, {})

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("watermark detection must not run when the scan is off")

    monkeypatch.setattr(export_svc, "replace_detected_watermark", fail_if_called)
    ui_settings.set_setting(export_svc.WATERMARK_SCAN_SETTING_KEY, False)

    result = export_svc.export_item(item_id)

    assert result["watermark_replaced"] is False
    assert result["watermark_skipped_reason"] == "watermark scan turned off"
    assert result["processed_path"]


def test_watermark_scan_is_on_until_it_is_explicitly_turned_off() -> None:
    # An unset preference must read as ON: defaulting a never-configured install
    # to "off" would silently ship foreign handles on real posts.
    assert export_svc.watermark_scan_enabled() is True
    ui_settings.set_setting(export_svc.WATERMARK_SCAN_SETTING_KEY, False)
    assert export_svc.watermark_scan_enabled() is False
    ui_settings.set_setting(export_svc.WATERMARK_SCAN_SETTING_KEY, True)
    assert export_svc.watermark_scan_enabled() is True


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

    def fake_extract(input_path: Path, output_path: Path, at_seconds=None) -> Path:
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


def test_crop_preview_frame_caches_each_scrubbed_timestamp_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    seen: list[float | None] = []

    def fake_extract(input_path: Path, output_path: Path, at_seconds=None) -> Path:
        seen.append(at_seconds)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"jpeg")
        return output_path

    monkeypatch.setattr(video, "extract_video_preview_frame", fake_extract)

    middle = export_svc.crop_preview_frame(item_id)
    scrubbed = export_svc.crop_preview_frame(item_id, at_seconds=3.0)

    # Distinct files per timestamp, and the timestamp reaches the extractor.
    assert middle != scrubbed
    assert scrubbed.name == f"item-{item_id}-t3000.jpg"
    assert seen == [None, 3.0]


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


# --- Burned-in post header ------------------------------------------------- #


def _use_post_header_template(item_id: int, **prefs: object) -> None:
    """Point the item's account at the post-header template, with header prefs."""
    import json

    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        account = session.get(Account, item.account_id)
        account.processing_preferences = json.dumps(
            {"template": "historytrails_post_header", **prefs}
        )
        session.commit()


def test_export_sends_no_post_header_for_a_plain_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    captured: dict = {}
    _mock_video(monkeypatch, captured)

    export_svc.export_item(item_id)

    assert captured["post_header"] is None


def test_export_sends_the_account_post_header_for_the_header_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), instagram_handle="pastmomentsdaily")
    _use_post_header_template(item_id, header_display_name="Past Moments Daily")

    headers_dir = tmp_path / "account_headers"
    headers_dir.mkdir()
    avatar = headers_dir / "pastmomentsdaily.png"
    avatar.write_bytes(b"not a real png, only the path is read here")
    monkeypatch.setattr(export_svc, "account_headers_dir", lambda: headers_dir)

    captured: dict = {}
    _mock_video(monkeypatch, captured)

    export_svc.export_item(item_id)

    header = captured["post_header"]
    assert header is not None
    assert header.display_name == "Past Moments Daily"
    assert header.avatar_path == avatar
    assert header.verified is True


def test_post_header_falls_back_to_the_account_name_without_an_avatar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source))
    _use_post_header_template(item_id, header_verified=False)
    monkeypatch.setattr(export_svc, "account_headers_dir", lambda: tmp_path / "missing")

    captured: dict = {}
    _mock_video(monkeypatch, captured)

    export_svc.export_item(item_id)

    header = captured["post_header"]
    assert header.display_name == "Export Account"
    assert header.avatar_path is None
    assert header.verified is False


def test_post_header_avatar_matches_a_versioned_variant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), instagram_handle="beneathhistory")
    _use_post_header_template(item_id)

    headers_dir = tmp_path / "account_headers"
    headers_dir.mkdir()
    # Real-world naming: no bare `<handle>.png`, mixed case, version suffixes,
    # and a different account whose handle starts with the same letters.
    (headers_dir / "BeneathHistory_pfp.png").write_bytes(b"older")
    newest = headers_dir / "beneathhistory2_pfp.png"
    newest.write_bytes(b"newer")
    (headers_dir / "beneathhistoryclub.png").write_bytes(b"different account")
    import os
    import time

    os.utime(headers_dir / "BeneathHistory_pfp.png", (time.time() - 600, time.time() - 600))
    os.utime(headers_dir / "beneathhistoryclub.png", (time.time() + 600, time.time() + 600))
    monkeypatch.setattr(export_svc, "account_headers_dir", lambda: headers_dir)

    captured: dict = {}
    _mock_video(monkeypatch, captured)

    export_svc.export_item(item_id)

    assert captured["post_header"].avatar_path == newest


def test_post_header_prefers_the_exact_handle_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake")
    item_id = _make_item(file_path=str(source), instagram_handle="vaultedhistory")
    _use_post_header_template(item_id)

    headers_dir = tmp_path / "account_headers"
    headers_dir.mkdir()
    exact = headers_dir / "vaultedhistory.png"
    exact.write_bytes(b"exact")
    (headers_dir / "vaultedhistory_pp4.png").write_bytes(b"variant")
    monkeypatch.setattr(export_svc, "account_headers_dir", lambda: headers_dir)

    captured: dict = {}
    _mock_video(monkeypatch, captured)

    export_svc.export_item(item_id)

    assert captured["post_header"].avatar_path == exact
