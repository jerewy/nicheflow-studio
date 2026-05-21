from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nicheflow_studio.processing import video
from nicheflow_studio.processing.video import CropSettings, VideoProbe


def _drawtext_textfile_contents(filter_chain: str) -> list[str]:
    contents: list[str] = []
    for entry in filter_chain.split("textfile='")[1:]:
        raw_path = entry.split("'", 1)[0]
        text_file = Path(raw_path.replace(r"\:", ":"))
        contents.append(text_file.read_text(encoding="utf-8"))
    return contents


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
    monkeypatch.setattr(
        video,
        "windows_font_file",
        lambda _font_name=None: Path("C:/Windows/Fonts/arial.ttf"),
    )
    expected_binary = str(Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1920, height=1080, duration_seconds=15.0),
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        filter_chain = command[command.index("-vf") + 1]
        captured["title_lines"] = _drawtext_textfile_contents(filter_chain)
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
        title_layout="overlay",
    )

    assert result == output_path.resolve()
    command = captured["command"]
    assert command[0] == expected_binary
    filter_chain = command[command.index("-vf") + 1]
    assert "crop=1720:980:120:40" in filter_chain
    assert "drawbox=" in filter_chain
    assert "drawtext=" in filter_chain
    assert "fontcolor=0xFFFFFF" in filter_chain
    assert "fontcolor=#FFFFFF" not in filter_chain
    assert "textfile=" in filter_chain
    assert captured["title_lines"] == ["Hook Title"]
    assert command[-1] == str(output_path.resolve())


def test_export_cropped_video_can_render_title_band_layout(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    output_path = tmp_path / "processed" / "sample_cropped.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(
        video,
        "windows_font_file",
        lambda _font_name=None: Path("C:/Windows/Fonts/arial.ttf"),
    )
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=15.0),
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        filter_chain = command[command.index("-filter_complex") + 1]
        captured["title_lines"] = _drawtext_textfile_contents(filter_chain)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    result = video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=CropSettings(),
        title_text="They brought his childhood memories back to life with AI technology",
        title_font_size=54,
        title_font_name="arial_bold",
        title_color="#FFFFFF",
        title_layout="top_band",
    )

    assert result == output_path.resolve()
    command = captured["command"]
    filter_chain = command[command.index("-filter_complex") + 1]
    assert "crop=1080:1920:0:0" in filter_chain
    assert "pad=1080:" in filter_chain
    assert "[content]" in filter_chain
    assert "color=c=black:s=1080x" in filter_chain
    assert "[title][content]vstack=inputs=2[block]" in filter_chain
    assert "[block]pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black[vout]" in filter_chain
    assert "boxblur" not in filter_chain
    assert "overlay=" not in filter_chain
    assert "split=2" not in filter_chain
    assert "drawtext=" in filter_chain
    assert "fontcolor=0xFFFFFF" in filter_chain
    assert "fontcolor=#FFFFFF" not in filter_chain
    assert "textfile=" in filter_chain
    assert any("childhood" in line for line in captured["title_lines"])
    assert any("memories" in line for line in captured["title_lines"])
    assert command[command.index("-map") + 1] == "[vout]"
    assert "0:a?" in command


def test_export_cropped_video_escapes_apostrophe_title_for_title_band(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    output_path = tmp_path / "processed" / "sample_cropped.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(
        video,
        "windows_font_file",
        lambda _font_name=None: Path("C:/Windows/Fonts/arialbd.ttf"),
    )
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=15.0),
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        filter_chain = command[command.index("-filter_complex") + 1]
        captured["title_lines"] = _drawtext_textfile_contents(filter_chain)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=CropSettings(top=178),
        title_text="Weightlifter's Tunnel Vision",
        title_font_size=60,
        title_font_name="arial_bold",
        title_color="#FFFFFF",
        title_layout="top_band",
    )

    command = captured["command"]
    filter_chain = command[command.index("-filter_complex") + 1]
    assert "textfile=" in filter_chain
    assert "Weightlifter" not in filter_chain
    assert captured["title_lines"] == ["Weightlifter's Tunnel Vision"]
    assert command[command.index("-map") + 1] == "[vout]"


def test_fit_title_band_reserves_clean_top_space() -> None:
    font_size, wrapped, band_height = video._fit_title_band(
        "Focus Mode Activated",
        canvas_width=1080,
        requested_font_size=54,
    )

    assert font_size == 54
    assert wrapped == "Focus Mode Activated"
    assert band_height == 172


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


def test_parse_tesseract_text_boxes_keeps_text_and_confidence() -> None:
    tsv_output = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t10\t20\t50\t18\t87.4\tHello",
            "5\t1\t1\t1\t1\t2\t66\t20\t60\t18\t74.2\tWorld",
        ]
    )

    result = video._parse_tesseract_text_boxes(tsv_output)

    assert [box.text for box in result] == ["Hello", "World"]
    assert [box.confidence for box in result] == [87.4, 74.2]


def test_ocr_snippets_filter_symbol_heavy_noise() -> None:
    boxes = [
        video.OcrTextBox(text="C]", confidence=80.0, left=10, top=10, width=20, height=12),
        video.OcrTextBox(text="(>", confidence=76.0, left=40, top=10, width=20, height=12),
        video.OcrTextBox(text="tal", confidence=52.0, left=70, top=10, width=30, height=12),
        video.OcrTextBox(text="A", confidence=49.0, left=110, top=10, width=16, height=12),
    ]

    assert video._ocr_snippets_from_boxes(boxes) == []


def test_ocr_snippets_keep_caption_like_text() -> None:
    boxes = [
        video.OcrTextBox(text="the", confidence=80.0, left=10, top=10, width=36, height=18),
        video.OcrTextBox(text="cool", confidence=82.0, left=52, top=10, width=48, height=18),
        video.OcrTextBox(text="thing", confidence=76.0, left=106, top=10, width=54, height=18),
        video.OcrTextBox(text='say"', confidence=72.0, left=166, top=10, width=42, height=18),
    ]

    assert video._ocr_snippets_from_boxes(boxes) == ["the cool thing say"]


def test_confirmed_region_snippets_reject_single_sample_noise() -> None:
    signals = [
        video.OcrRegionSignal(
            region="bottom",
            timestamp=1.0,
            text_detected=True,
            snippets=("TLL WEE stall A",),
            average_confidence=74.0,
        ),
        video.OcrRegionSignal(
            region="bottom",
            timestamp=5.0,
            text_detected=False,
            snippets=(),
            average_confidence=None,
        ),
        video.OcrRegionSignal(
            region="bottom",
            timestamp=9.0,
            text_detected=False,
            snippets=(),
            average_confidence=None,
        ),
    ]

    assert video._confirmed_region_snippets(signals, "bottom", sample_count=3) == []


def test_confirmed_region_snippets_keep_repeated_caption_text() -> None:
    signals = [
        video.OcrRegionSignal(
            region="bottom",
            timestamp=1.0,
            text_detected=True,
            snippets=("the cool thing",),
            average_confidence=78.0,
        ),
        video.OcrRegionSignal(
            region="bottom",
            timestamp=5.0,
            text_detected=True,
            snippets=("the cool thing sa",),
            average_confidence=76.0,
        ),
    ]

    assert video._confirmed_region_snippets(signals, "bottom", sample_count=3) == [
        "the cool thing",
        "the cool thing sa",
    ]


def test_diagnose_preprocessing_ocr_reports_missing_tools(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffmpeg_binary", lambda: None)
    monkeypatch.setattr(video, "tesseract_binary", lambda: None)

    diagnostics = video.diagnose_preprocessing_ocr(input_path)

    assert diagnostics.ffmpeg_available is False
    assert diagnostics.tesseract_available is False
    assert diagnostics.sample_count == 0
    assert diagnostics.top_text_detected is False
    assert diagnostics.bottom_text_detected is False
    assert "ffmpeg unavailable" in diagnostics.debug_messages[0]


def test_diagnose_preprocessing_ocr_samples_top_and_bottom_regions(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    extracted_paths: list[Path] = []

    monkeypatch.setattr(video, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(video, "tesseract_binary", lambda: Path("C:/tools/tesseract.exe"))
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=12.0),
    )

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command[2] == "stdout":
            frame_path = Path(command[1])
            if "top" in frame_path.stem:
                stdout = "\n".join(
                    [
                        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                        "5\t1\t1\t1\t1\t1\t20\t30\t120\t24\t88.0\tTop",
                        "5\t1\t1\t1\t1\t2\t150\t30\t140\t24\t82.0\tHook",
                    ]
                )
            else:
                stdout = "\n".join(
                    [
                        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                        "5\t1\t1\t1\t1\t1\t20\t30\t160\t24\t80.0\tSubscribe",
                    ]
                )
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

        extracted_paths.append(Path(command[-1]))
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    diagnostics = video.diagnose_preprocessing_ocr(input_path, sample_count=2)

    assert diagnostics.sample_count == 2
    assert diagnostics.top_text_detected is True
    assert diagnostics.bottom_text_detected is True
    assert diagnostics.top_snippets == ("Top Hook",)
    assert diagnostics.bottom_snippets == ("Subscribe",)
    assert diagnostics.average_confidence == pytest.approx(83.3)
    assert len(diagnostics.region_signals) == 4
    assert {path.stem.rsplit("-", 1)[-1] for path in extracted_paths} == {"top", "bottom"}


def test_suggest_crop_settings_ignores_raw_ocr_crop(monkeypatch, tmp_path: Path) -> None:
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
        "detect_dark_band_crop",
        lambda path, probe: CropSettings(),
    )
    monkeypatch.setattr(video, "detect_top_title_crop", lambda path, probe: None)

    suggestion = video.suggest_crop_settings(input_path)

    assert suggestion.crop == CropSettings(left=10, top=0, right=12, bottom=0)
    assert suggestion.used_border_detection is True
    assert suggestion.used_ocr is False
    assert any("border" in reason for reason in suggestion.reasons)
    assert not any("OCR" in reason or "text" in reason for reason in suggestion.reasons)


def test_suggest_crop_settings_uses_ocr_top_crop_to_replace_source_title(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=14.0),
    )
    monkeypatch.setattr(
        video,
        "detect_border_crop",
        lambda path, probe: CropSettings(top=154),
    )
    monkeypatch.setattr(
        video,
        "detect_dark_band_crop",
        lambda path, probe: CropSettings(top=485, bottom=224),
    )
    monkeypatch.setattr(
        video,
        "detect_top_title_crop",
        lambda path, probe: CropSettings(top=223),
    )
    monkeypatch.setattr(video, "detect_bottom_caption_crop", lambda path, probe: None)

    suggestion = video.suggest_crop_settings(input_path)

    assert suggestion.crop == CropSettings(top=223)
    assert suggestion.used_ocr is True
    assert any("new title can replace it" in reason for reason in suggestion.reasons)


def test_suggest_crop_settings_uses_repeated_bottom_caption_crop(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=14.0),
    )
    monkeypatch.setattr(video, "detect_border_crop", lambda path, probe: CropSettings())
    monkeypatch.setattr(video, "detect_dark_band_crop", lambda path, probe: CropSettings())
    monkeypatch.setattr(video, "detect_top_title_crop", lambda path, probe: None)
    monkeypatch.setattr(
        video,
        "detect_bottom_caption_crop",
        lambda path, probe: CropSettings(bottom=96),
    )

    suggestion = video.suggest_crop_settings(input_path)

    assert suggestion.crop == CropSettings(bottom=96)
    assert suggestion.used_ocr is True
    assert any("bottom caption" in reason for reason in suggestion.reasons)


def test_suggest_crop_settings_rejects_aggressive_dark_band_crop(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=14.0),
    )
    monkeypatch.setattr(
        video,
        "detect_border_crop",
        lambda path, probe: CropSettings(top=154),
    )
    monkeypatch.setattr(
        video,
        "detect_dark_band_crop",
        lambda path, probe: CropSettings(top=485, bottom=224),
    )
    monkeypatch.setattr(video, "detect_top_title_crop", lambda path, probe: None)
    monkeypatch.setattr(video, "detect_bottom_caption_crop", lambda path, probe: None)

    suggestion = video.suggest_crop_settings(input_path)

    assert suggestion.crop == CropSettings(top=154)
    assert suggestion.used_ocr is False
    assert not any("dark title bars" in reason for reason in suggestion.reasons)


def test_safe_dark_band_crop_keeps_small_top_margin_only() -> None:
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)

    result = video._safe_dark_band_crop(CropSettings(top=120, bottom=90), probe)

    assert result == CropSettings(top=120)


def test_safe_dark_band_crop_keeps_reposted_title_header_margin() -> None:
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)

    result = video._safe_dark_band_crop(CropSettings(top=240, bottom=0), probe)

    assert result == CropSettings(top=240)


def test_suggest_title_replacement_crop_preserves_detected_video_body(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)

    monkeypatch.setattr(video, "detect_content_rectangle", lambda path, probe: None)
    monkeypatch.setattr(video, "detect_visual_content_crop", lambda path, probe: CropSettings(top=660))
    monkeypatch.setattr(
        video,
        "detect_dark_band_crop",
        lambda path, probe: CropSettings(top=420, bottom=120),
    )
    monkeypatch.setattr(video, "detect_text_crop", lambda path, probe: CropSettings(top=360))

    result = video.suggest_title_replacement_crop(input_path, probe)

    assert result == CropSettings(top=632)


def test_suggest_title_replacement_crop_uses_text_signal_when_visual_missing(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)

    monkeypatch.setattr(video, "detect_content_rectangle", lambda path, probe: None)
    monkeypatch.setattr(video, "detect_visual_content_crop", lambda path, probe: None)
    monkeypatch.setattr(video, "detect_dark_band_crop", lambda path, probe: CropSettings(top=420))
    monkeypatch.setattr(video, "detect_text_crop", lambda path, probe: CropSettings(top=360))

    result = video.suggest_title_replacement_crop(input_path, probe)

    assert result == CropSettings(top=435)


def test_suggest_title_replacement_crop_rejects_aggressive_top_signal(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)

    monkeypatch.setattr(video, "detect_content_rectangle", lambda path, probe: None)
    monkeypatch.setattr(video, "detect_visual_content_crop", lambda path, probe: None)
    monkeypatch.setattr(video, "detect_dark_band_crop", lambda path, probe: CropSettings(top=980))
    monkeypatch.setattr(video, "detect_text_crop", lambda path, probe: CropSettings())

    result = video.suggest_title_replacement_crop(input_path, probe)

    assert result == CropSettings()


def test_suggest_title_replacement_crop_uses_content_rectangle(
    monkeypatch, tmp_path: Path
) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)

    monkeypatch.setattr(
        video,
        "detect_content_rectangle",
        lambda path, probe: CropSettings(left=80, top=776, right=88, bottom=600),
    )

    result = video.suggest_title_replacement_crop(input_path, probe)

    # 2% top overshoot: 776 + int(1920 * 0.02) = 776 + 38 = 814
    assert result == CropSettings(left=80, top=814, right=88, bottom=600)


def test_largest_coverage_band_finds_longest_run() -> None:
    import numpy as np

    coverage = np.array([0.0, 0.0, 0.5, 0.5, 0.5, 0.0, 0.9, 0.0])

    assert video._largest_coverage_band(coverage, 0.2, 2) == (2, 4)


def test_largest_coverage_band_rejects_short_runs() -> None:
    import numpy as np

    coverage = np.array([0.0, 0.9, 0.0, 0.9, 0.0])

    assert video._largest_coverage_band(coverage, 0.2, 2) is None


def test_detect_content_rectangle_finds_moving_footage(
    monkeypatch, tmp_path: Path
) -> None:
    import numpy as np

    input_path = tmp_path / "clip.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=200, height=400, duration_seconds=20.0)
    rng = np.random.default_rng(0)

    def fake_load(path, timestamp):  # noqa: ANN001, ARG001
        # Black canvas with a bright, frame-to-frame-changing footage rectangle.
        frame = np.zeros((400, 200, 3), dtype=np.uint8)
        frame[100:300, 40:160, :] = rng.integers(
            60, 255, size=(200, 120, 3), dtype=np.uint8
        )
        return frame

    monkeypatch.setattr(video, "_load_video_frame_at", fake_load)

    rect = video.detect_content_rectangle(input_path, probe)

    assert rect is not None
    assert abs(rect.top - 100) <= 30
    assert abs(rect.bottom - 100) <= 30
    assert abs(rect.left - 40) <= 30
    assert abs(rect.right - 40) <= 30


def test_detect_content_rectangle_returns_none_for_full_frame_footage(
    monkeypatch, tmp_path: Path
) -> None:
    import numpy as np

    input_path = tmp_path / "clip.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=200, height=400, duration_seconds=20.0)
    rng = np.random.default_rng(1)

    def fake_load(path, timestamp):  # noqa: ANN001, ARG001
        # Footage fills the whole frame -> no canvas to crop away.
        return rng.integers(60, 255, size=(400, 200, 3), dtype=np.uint8)

    monkeypatch.setattr(video, "_load_video_frame_at", fake_load)

    rect = video.detect_content_rectangle(input_path, probe)

    # Whole frame is active -> the 8% remaining-dimension guard keeps crop near zero.
    assert rect is None or rect == CropSettings()


def test_visual_content_top_margin_finds_wide_video_block_below_text() -> None:
    import numpy as np

    frame = np.zeros((1000, 600, 3), dtype=np.uint8)
    frame[220:250, 40:500, :] = 255
    frame[360:820, 20:580, :] = 120

    assert video._visual_content_top_margin(frame) == 360


def test_detect_bottom_caption_crop_requires_bottom_ocr(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)
    diagnostics = video.PreprocessingOcrDiagnostics(
        top_text_detected=False,
        bottom_text_detected=True,
        top_snippets=(),
        bottom_snippets=("meow :)",),
        average_confidence=82.0,
        sample_count=3,
        ffmpeg_available=True,
        tesseract_available=True,
        region_signals=(),
        debug_messages=(),
    )

    monkeypatch.setattr(video, "diagnose_preprocessing_ocr", lambda path: diagnostics)
    monkeypatch.setattr(video, "detect_text_crop", lambda path, probe: CropSettings(bottom=84))

    assert video.detect_bottom_caption_crop(input_path, probe) == CropSettings(bottom=84)


def test_detect_bottom_caption_crop_rejects_aggressive_crop(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "sample.mp4"
    input_path.write_bytes(b"video")
    probe = VideoProbe(width=1080, height=1920, duration_seconds=14.0)
    diagnostics = video.PreprocessingOcrDiagnostics(
        top_text_detected=False,
        bottom_text_detected=True,
        top_snippets=(),
        bottom_snippets=("meow :)",),
        average_confidence=82.0,
        sample_count=3,
        ffmpeg_available=True,
        tesseract_available=True,
        region_signals=(),
        debug_messages=(),
    )

    monkeypatch.setattr(video, "diagnose_preprocessing_ocr", lambda path: diagnostics)
    monkeypatch.setattr(video, "detect_text_crop", lambda path, probe: CropSettings(bottom=420))

    assert video.detect_bottom_caption_crop(input_path, probe) is None


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

    frame = np.full((200, 50, 3), 180, dtype=np.uint8)
    frame[:56, :, :] = 0
    frame[10:44, 6:44, :] = 255

    margin = video._dark_band_margin(frame, from_top=True)

    assert margin >= 56


def test_dark_band_margin_detects_tall_source_title_header() -> None:
    import numpy as np

    frame = np.full((1000, 120, 3), 170, dtype=np.uint8)
    frame[:320, :, :] = 0
    frame[120:170, 15:105, :] = 255

    margin = video._dark_band_margin(frame, from_top=True)

    assert margin >= 320
