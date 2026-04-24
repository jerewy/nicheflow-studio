from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nicheflow_studio.processing import video
from nicheflow_studio.processing.video import CropSettings, VideoProbe


def test_output_dimensions_rejects_crop_that_removes_the_video() -> None:
    probe = VideoProbe(width=1920, height=1080, duration_seconds=10.0)

    with pytest.raises(ValueError, match="too aggressive"):
        video.output_dimensions(
            probe,
            CropSettings(left=1000, right=919, top=0, bottom=0),
        )


def test_processed_output_path_appends_cropped_suffix(tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    output_dir = tmp_path / "processed"

    result = video.processed_output_path(input_path, output_dir)

    assert result == output_dir / "sample_cropped.mp4"


def test_probe_video_parses_ffprobe_json(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffprobe_binary", lambda: Path("C:/tools/ffprobe.exe"))
    expected_binary = str(Path("C:/tools/ffprobe.exe"))
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert command[0] == expected_binary
        assert command[-1] == str(input_path.resolve())
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"streams":[{"width":1280,"height":720}],"format":{"duration":"9.5"}}',
            stderr="",
        )

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    result = video.probe_video(input_path)

    assert result == VideoProbe(width=1280, height=720, duration_seconds=9.5)
    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_subprocess_run_kwargs_hide_windows_console() -> None:
    assert video.subprocess_run_kwargs() == {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
    }


def test_probe_video_falls_back_to_pyav_when_ffprobe_fails(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffprobe_binary", lambda: Path("C:/tools/ffprobe.exe"))

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        raise OSError("ffprobe launch failed")

    class FakeStream:
        width = 640
        height = 360
        duration = None
        time_base = None

    class FakeContainer:
        streams = type("Streams", (), {"video": [FakeStream()]})()
        duration = 3_500_000

        def close(self) -> None:
            return None

    monkeypatch.setattr(video.subprocess, "run", fake_run)
    monkeypatch.setattr(video.av, "open", lambda path: FakeContainer())

    result = video.probe_video(input_path)

    assert result == VideoProbe(width=640, height=360, duration_seconds=3.5)


def test_export_cropped_video_builds_expected_ffmpeg_command(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    output_path = tmp_path / "processed" / "sample_cropped.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(video, "windows_font_file", lambda _font_name=None: Path("C:/Windows/Fonts/arial.ttf"))
    expected_binary = str(Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1920, height=1080, duration_seconds=15.0),
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    result = video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=CropSettings(left=120, top=40, right=80, bottom=60),
        title_text="Hook Title",
        title_font_size=54,
        title_font_name="impact",
        title_color="#FFFFFF",
        title_background="dark",
    )

    assert result == output_path.resolve()
    command = captured["command"]
    assert command[0] == expected_binary
    filter_chain = command[command.index("-vf") + 1]
    assert "crop=1720:980:120:40" in filter_chain
    assert "drawbox=" in filter_chain
    assert "drawtext=" in filter_chain
    assert "Hook Title" in filter_chain
    assert command[-1] == str(output_path.resolve())


def test_parse_tesseract_boxes_keeps_confident_text_rows() -> None:
    tsv_output = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t10\t20\t50\t18\t87.4\tHello",
            "5\t1\t1\t1\t1\t2\t300\t400\t8\t8\t20.0\tlow",
            "5\t1\t1\t1\t1\t3\t0\t0\t0\t0\t95.0\t",
        ]
    )

    result = video._parse_tesseract_boxes(tsv_output)

    assert result == [(10, 20, 50, 18)]


def test_suggest_crop_settings_combines_border_and_text_detection(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1920, height=1080, duration_seconds=8.0),
    )
    monkeypatch.setattr(
        video,
        "detect_border_crop",
        lambda path, probe: CropSettings(left=10, top=0, right=12, bottom=0),
    )
    monkeypatch.setattr(
        video,
        "detect_text_crop",
        lambda path, probe: CropSettings(left=0, top=40, right=0, bottom=120),
    )

    suggestion = video.suggest_crop_settings(input_path)

    assert suggestion.crop == CropSettings(left=10, top=40, right=12, bottom=120)
    assert suggestion.used_border_detection is True
    assert suggestion.used_ocr is True
    assert any("border" in reason for reason in suggestion.reasons)
    assert any("OCR" in reason or "text" in reason for reason in suggestion.reasons)


def test_sample_timestamps_spreads_visual_context_across_clip() -> None:
    probe = VideoProbe(width=1920, height=1080, duration_seconds=100.0)

    timestamps = video._sample_timestamps(probe, count=5)

    assert timestamps == pytest.approx([8.0, 29.0, 50.0, 71.0, 92.0])


def test_fit_title_overlay_reduces_font_size_and_wraps_for_narrow_video() -> None:
    font_size, wrapped, box_height = video._fit_title_overlay(
        "Ultimate Hoe Hoe Hoe Challenge",
        crop_width=720,
        requested_font_size=54,
    )

    assert font_size < 54
    assert "\n" in wrapped
    assert box_height >= 64


def test_dark_band_margin_detects_top_blank_bar() -> None:
    import numpy as np

    frame = np.full((100, 50, 3), 180, dtype=np.uint8)
    frame[:20, :, :] = 0
    frame[8:14, 10:40, :] = 255

    margin = video._dark_band_margin(frame, from_top=True)

    assert margin >= 20
