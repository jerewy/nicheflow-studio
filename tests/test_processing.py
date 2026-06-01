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
    assert "[block]pad=1080:1920:(ow-iw)/2:0:color=black[vout]" in filter_chain
    assert "(oh-ih)/2" not in filter_chain
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


def test_export_cropped_video_preserves_dialogue_title_band_breaks(
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
        captured["filter_chain"] = filter_chain
        captured["title_lines"] = _drawtext_textfile_contents(filter_chain)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=CropSettings(),
        title_text='Group chat: "one more reel and you\'re muted"\n\nMe:',
        title_font_size=54,
        title_font_name="arial_bold",
        title_color="#FFFFFF",
        title_layout="top_band",
    )

    filter_chain = str(captured["filter_chain"])
    title_lines = captured["title_lines"]
    assert title_lines[-1] == "Me:"
    assert all(" Me:" not in line for line in title_lines)
    # The Group chat paragraph wraps across whichever lines the balanced
    # wrapper picks (it now produces a more balanced split than the old
    # greedy "fill line 1 to the brim" behaviour). Verify structurally:
    # every word from the first paragraph survives, in order, and none of
    # it bled into the "Me:" line.
    paragraph_one_text = " ".join(title_lines[:-1])
    assert paragraph_one_text == 'Group chat: "one more reel and you\'re muted"'
    assert "x=(w-text_w)/2" not in filter_chain
    import re as _re

    x_values = {int(match.group(1)) for match in _re.finditer(r"x=(\d+):", filter_chain)}
    assert len(x_values) == 1
    assert next(iter(x_values)) >= 72


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

    # The detector's edges are trusted as-is — no overshoot — so the
    # returned crop matches detect_content_rectangle's result exactly.
    assert result == CropSettings(left=80, top=776, right=88, bottom=600)


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


def test_fit_title_overlay_autofits_long_cinema_title() -> None:
    """Long overlay titles must auto-shrink + grow lines so no wrapped line
    clips the crop. Same root cause as the _fit_title_band fix:
    _wrap_overlay_text collapses extra lines onto line 2 without
    re-checking max_chars."""
    font_size, wrapped, box_height = video._fit_title_overlay(
        "That kind of twist that makes you rewatch every quiet room differently.",
        crop_width=1080,
        requested_font_size=64,
    )
    # Font shrinks from the initial 41px ceiling to make room:
    assert font_size < 41
    assert font_size >= 24  # auto-fit floor
    # Every wrapped line must stay within ~92% of the crop at the chosen font.
    # Approximate char width ≈ font_size * 0.55.
    char_width = max(1, int(font_size * 0.55))
    max_safe_chars = int((1080 * 0.92) / char_width)
    for line in wrapped.split("\n"):
        if line.strip():
            assert len(line) <= max_safe_chars, f"line {line!r} too wide"
    # The previously-overflowing collapsed tail must not survive as one line:
    assert "makes you rewatch every quiet room differently" not in wrapped
    # Box height scales with line_count; sanity bound:
    assert 96 <= box_height <= 400


def test_fit_title_overlay_short_title_unchanged_after_autofit() -> None:
    """Auto-fit must NOT touch overlay titles that already fit at the
    initial 2-line budget."""
    font_size_initial, wrapped_initial, box_initial = video._fit_title_overlay(
        "Focus Mode Activated",
        crop_width=1080,
        requested_font_size=40,
    )
    # 20-char single line fits at the initial sizing: font 40, max_chars 22.
    assert font_size_initial == 40
    assert wrapped_initial == "Focus Mode Activated"
    assert box_initial == max(96, (40 + 14) * 1 + 34)


def test_normalize_overlay_text_preserves_paragraph_breaks() -> None:
    normalized = video._normalize_overlay_text(
        '  Friend: can you please stop sending\r\nme reels? \r\n\r\n  Me:  '
    )

    assert normalized == "Friend: can you please stop sending me reels?\n\nMe:"


# ---------------------------------------------------------------------------
# Emoji rendering: ffmpeg's drawtext has no font fallback, so emoji codepoints
# would render as missing-glyph boxes (□) in the bundled Arial/Impact fonts.
# When Segoe UI Emoji is available we render the title with Pillow to a PNG
# and composite via ffmpeg's `overlay` filter. _normalize_overlay_text now
# PRESERVES emoji so the renderer can decide based on font availability.
# ---------------------------------------------------------------------------


def test_normalize_overlay_text_preserves_emoji() -> None:
    # The screenshot case: emoji must NOT be stripped — the renderer routes
    # emoji-bearing titles through the PIL+overlay path so they render in
    # full color.
    text = "when you get there and instantly want to leave 😭"
    assert video._normalize_overlay_text(text) == text


def test_text_has_emoji_detects_common_codepoints() -> None:
    assert video._text_has_emoji("leave 😭")
    assert video._text_has_emoji("ok 💀")
    assert video._text_has_emoji("hot 🔥 take")
    assert video._text_has_emoji("sunny ☀ day")  # U+2600 block
    assert not video._text_has_emoji("clean ascii text")
    assert not video._text_has_emoji("Me: that's wild — really")
    assert not video._text_has_emoji("")


def test_split_emoji_runs_separates_text_and_emoji_segments() -> None:
    assert video._split_emoji_runs("leave 😭") == [("leave ", False), ("😭", True)]
    assert video._split_emoji_runs("😭 then text") == [("😭", True), (" then text", False)]
    assert video._split_emoji_runs("a 😭 b 💀 c") == [
        ("a ", False),
        ("😭", True),
        (" b ", False),
        ("💀", True),
        (" c", False),
    ]
    assert video._split_emoji_runs("plain") == [("plain", False)]
    assert video._split_emoji_runs("😭") == [("😭", True)]
    assert video._split_emoji_runs("") == []


def test_strip_overlay_emojis_cleans_whitespace_when_used_as_fallback() -> None:
    # The fallback path runs only when no emoji font is available. The helper
    # both removes emoji codepoints AND tidies the whitespace they left
    # behind so the trailing-emoji case does not ship a stray space.
    assert (
        video._strip_overlay_emojis("leave 😭")
        == "leave"
    )
    assert video._strip_overlay_emojis("hot 🔥 take") == "hot take"
    assert video._strip_overlay_emojis("clean text") == "clean text"
    # Paragraph break structure is preserved by the fallback.
    assert (
        video._strip_overlay_emojis("Them: chill 💀\n\nMe when I drive:")
        == "Them: chill\n\nMe when I drive:"
    )


def test_color_to_rgba_parses_hex_and_falls_back_to_white() -> None:
    assert video._color_to_rgba("#FFFFFF") == (255, 255, 255, 255)
    assert video._color_to_rgba("#000000") == (0, 0, 0, 255)
    assert video._color_to_rgba("#FFF2BF") == (255, 242, 191, 255)
    # Garbage input becomes white so the title is at least readable.
    assert video._color_to_rgba("not a color") == (255, 255, 255, 255)


def test_render_overlay_title_image_produces_rgba_png_with_color_emoji(
    tmp_path: Path,
) -> None:
    from PIL import Image

    emoji_font_path = Path("C:/Windows/Fonts/seguiemj.ttf")
    text_font_path = Path("C:/Windows/Fonts/arialbd.ttf")
    if not emoji_font_path.exists() or not text_font_path.exists():
        pytest.skip("Windows-only test: requires Segoe UI Emoji and Arial Bold.")

    output_path = tmp_path / "title.png"
    video._render_overlay_title_image(
        lines=["leave 😭"],
        canvas_width=400,
        canvas_height=120,
        font_path=text_font_path,
        emoji_font_path=emoji_font_path,
        font_size=48,
        line_spacing=10,
        color="#FFFFFF",
        start_y=30,
        align="center",
        outline_width=2,
        output_path=output_path,
    )
    assert output_path.exists()

    with Image.open(output_path) as image:
        assert image.size == (400, 120)
        assert image.mode == "RGBA"
        # The emoji glyph paints colored (non-greyscale) pixels — find at
        # least one pixel where R ≠ G or G ≠ B, which can only come from
        # the COLR/CPAL palette since the text portion is pure white.
        has_color_pixel = any(
            a > 0 and (r != g or g != b)
            for r, g, b, a in image.get_flattened_data()
        )
        assert has_color_pixel, "Expected at least one color emoji pixel"


def test_render_overlay_bold_keyword_renders_larger_than_body_text(
    tmp_path: Path,
) -> None:
    """A ``**bold**`` keyword swaps to a heavier sibling font scaled up by
    ``_BOLD_KEYWORD_SCALE``, so the emphasised word must paint a taller and
    wider ink box than the same word rendered at body size. Guards the
    enlargement + baseline-alignment path against a silent metrics regression."""
    from PIL import Image

    emoji_font_path = Path("C:/Windows/Fonts/seguiemj.ttf")
    text_font_path = Path("C:/Windows/Fonts/georgia.ttf")
    bold_font_path = Path("C:/Windows/Fonts/georgiab.ttf")
    if not all(p.exists() for p in (emoji_font_path, text_font_path, bold_font_path)):
        pytest.skip("Windows-only test: requires Segoe UI Emoji, Georgia, Georgia Bold.")

    assert video._BOLD_KEYWORD_SCALE > 1.0

    def _ink_bbox(word: str, *, bold: bool) -> tuple[int, int, int, int]:
        markup = f"**{word}**" if bold else word
        visible, flags = video._parse_bold_markup(markup)
        masks = video._bold_masks_for_wrapped(visible, flags, visible)
        output_path = tmp_path / f"bold_{bold}.png"
        video._render_overlay_title_image(
            lines=[visible],
            canvas_width=600,
            canvas_height=200,
            font_path=text_font_path,
            emoji_font_path=emoji_font_path,
            font_size=60,
            line_spacing=10,
            color="#FFFFFF",
            start_y=70,
            align="center",
            outline_width=2,
            output_path=output_path,
            bold_font_path=bold_font_path,
            bold_masks=masks,
        )
        with Image.open(output_path) as image:
            bbox = image.getbbox()
        assert bbox is not None, "Expected the word to paint visible ink"
        return bbox

    plain = _ink_bbox("sacrifice", bold=False)
    emphasised = _ink_bbox("sacrifice", bold=True)
    # Enlarged bold paints a taller and wider ink box than body text...
    assert (emphasised[3] - emphasised[1]) > (plain[3] - plain[1])  # taller
    assert (emphasised[2] - emphasised[0]) > (plain[2] - plain[0])  # wider
    # ...while staying on the shared baseline (same ink bottom edge).
    assert emphasised[3] == plain[3]


def test_export_cropped_video_uses_png_overlay_when_title_has_emoji_top_band(
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
    # Force the renderer to take the emoji path regardless of host OS.
    monkeypatch.setattr(
        video,
        "windows_emoji_font_file",
        lambda: Path("C:/Windows/Fonts/seguiemj.ttf"),
    )
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1080, height=1920, duration_seconds=12.0),
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["filter_chain"] = command[command.index("-filter_complex") + 1]
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=CropSettings(),
        title_text="want to leave 😭",
        title_font_size=54,
        title_font_name="arial_bold",
        title_color="#FFFFFF",
        title_layout="top_band",
    )

    filter_chain = str(captured["filter_chain"])
    # PIL path emits a movie filter that loads the rendered PNG.
    assert "movie='" in filter_chain
    assert "title-band.png" in filter_chain
    assert "[titlebase][titletext]overlay=0:0" in filter_chain
    assert "overlay=0:0:format=auto,format=yuv420p[title]" in filter_chain
    # No drawtext fallback should sneak in when emoji rendering is taking over.
    assert "drawtext=" not in filter_chain
    # And the [vout] mapping survives so audio still passes through.
    command = list(captured["command"])
    assert command[command.index("-map") + 1] == "[vout]"
    assert "0:a?" in command
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


def test_export_cropped_video_uses_png_overlay_when_title_has_emoji_overlay_layout(
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
        "windows_emoji_font_file",
        lambda: Path("C:/Windows/Fonts/seguiemj.ttf"),
    )
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1920, height=1080, duration_seconds=12.0),
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["filter_chain"] = command[command.index("-filter_complex") + 1]
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=CropSettings(left=120, top=40, right=80, bottom=60),
        title_text="hot 🔥 take",
        title_font_size=54,
        title_font_name="impact",
        title_color="#FFFFFF",
        title_background="dark",
        title_layout="overlay",
    )

    filter_chain = str(captured["filter_chain"])
    assert "movie='" in filter_chain
    assert "title-overlay.png" in filter_chain
    assert "[content][titletext]overlay=0:0" in filter_chain
    assert "overlay=0:0:format=auto,format=yuv420p[vout]" in filter_chain
    # Dark background tint still applied via drawbox on the cropped frame.
    assert "drawbox=" in filter_chain
    # No -vf when filter_complex is in use.
    command = list(captured["command"])
    assert "-vf" not in command
    assert command[command.index("-map") + 1] == "[vout]"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


def test_export_cropped_video_strips_emoji_when_emoji_font_missing(
    monkeypatch, tmp_path: Path
) -> None:
    # Non-Windows / missing seguiemj.ttf: fall back to stripping emoji so the
    # rendered video doesn't show the missing-glyph rectangle.
    input_path = tmp_path / "sample.mp4"
    output_path = tmp_path / "processed" / "sample_cropped.mp4"
    input_path.write_bytes(b"video")

    monkeypatch.setattr(video, "ffmpeg_binary", lambda: Path("C:/tools/ffmpeg.exe"))
    monkeypatch.setattr(
        video,
        "windows_font_file",
        lambda _font_name=None: Path("C:/Windows/Fonts/arial.ttf"),
    )
    monkeypatch.setattr(video, "windows_emoji_font_file", lambda: None)
    monkeypatch.setattr(
        video,
        "probe_video",
        lambda _: VideoProbe(width=1920, height=1080, duration_seconds=12.0),
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["filter_chain"] = command[command.index("-vf") + 1]
        captured["title_lines"] = _drawtext_textfile_contents(captured["filter_chain"])
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", fake_run)

    video.export_cropped_video(
        input_path=input_path,
        output_path=output_path,
        crop=CropSettings(),
        title_text="leave 😭",
        title_font_size=54,
        title_font_name="impact",
        title_color="#FFFFFF",
        title_layout="overlay",
    )

    # Drawtext still used (no emoji font available), and the emoji was
    # stripped + trailing space cleaned before reaching the text file.
    assert "drawtext=" in str(captured["filter_chain"])
    assert "movie='" not in str(captured["filter_chain"])
    assert captured["title_lines"] == ["leave"]


# ---------------------------------------------------------------------------
# Template B renderer support: \n\n paragraph breaks must survive wrapping
# and the title band must render two visually separated lines.
# ---------------------------------------------------------------------------


def test_wrap_overlay_text_preserves_paragraph_break() -> None:
    """A Template B meme title (``Them: X\\n\\nMe when Y:``) must keep its
    blank-line break through wrapping. Before the renderer fix, the global
    ``text.split()`` collapsed it into one block."""
    wrapped = video._wrap_overlay_text(
        'Them: "you\'re so sweet and kind!"\n\nMe when I drive:',
        max_chars=34,
        max_lines=2,
    )
    # The blank line between the two paragraphs survives:
    assert "\n\n" in wrapped
    # Both paragraphs still recognizable:
    assert "sweet and kind" in wrapped
    assert "Me when I drive" in wrapped


def test_wrap_overlay_text_wraps_each_paragraph_independently() -> None:
    """Each paragraph gets its own ``max_lines`` budget so a long line in
    paragraph 1 doesn't steal wrap room from paragraph 2."""
    text = (
        "Them: well actually this is a much longer expectation line than usual\n\n"
        "Me when I drive:"
    )
    wrapped = video._wrap_overlay_text(text, max_chars=22, max_lines=2)
    paragraphs = wrapped.split("\n\n")
    assert len(paragraphs) == 2
    # First paragraph gets wrapped to ≤2 lines:
    assert paragraphs[0].count("\n") + 1 <= 2
    # Second paragraph stays intact (it fits in one line):
    assert paragraphs[1] == "Me when I drive:"


def test_wrap_overlay_text_single_paragraph_unchanged() -> None:
    """Backward-compat: single-paragraph titles wrap exactly like before
    the paragraph-break refactor."""
    assert (
        video._wrap_overlay_text("Focus Mode Activated", max_chars=34, max_lines=2)
        == "Focus Mode Activated"
    )
    # Longer single paragraph wraps to 2 lines as it did before:
    long_wrap = video._wrap_overlay_text(
        "When you finally understand the joke 10 minutes later",
        max_chars=22,
        max_lines=2,
    )
    assert long_wrap.count("\n") == 1
    assert "\n\n" not in long_wrap  # no spurious paragraph break introduced


def test_wrap_overlay_text_drops_empty_paragraphs() -> None:
    """Sloppy model output with triple newlines collapses to a single
    paragraph break rather than emitting two consecutive blanks."""
    wrapped = video._wrap_overlay_text(
        "Them: hi\n\n\n\nMe:",
        max_chars=22,
        max_lines=2,
    )
    # Only one \n\n separator regardless of how many blanks the model emits:
    assert wrapped.count("\n\n") == 1


def test_balanced_wrap_keeps_short_text_on_one_line() -> None:
    """Balanced wrap must not fragment text that fits cleanly on one line.
    Naive min-max would split "Focus Mode Activated" (20 chars) into two
    10-char lines because that minimizes max-width — wrong tradeoff."""
    assert video._balanced_wrap_lines(
        ["Focus", "Mode", "Activated"], max_chars=34, max_lines=2
    ) == ["Focus Mode Activated"]


def test_balanced_wrap_balances_two_line_split() -> None:
    """Greedy fill-line-1 leaves an unbalanced second line. Balanced
    min-max picks an earlier split so line widths are close."""
    words = "When the cat finally jumps onto the table and breaks the vase".split()
    # At max_chars=35 the greedy result is ("...jumps onto the" 35, "table...vase" 25).
    # Balanced finds ("...jumps onto" 30, "the table...vase" 29).
    result = video._balanced_wrap_lines(words, max_chars=35, max_lines=2)
    assert result is not None
    assert len(result) == 2
    line_widths = [len(line) for line in result]
    assert max(line_widths) - min(line_widths) <= 2
    # The split must not lose words or reorder them:
    assert " ".join(result) == " ".join(words)


def test_balanced_wrap_returns_none_when_no_valid_split_fits() -> None:
    """When max_chars × max_lines can't hold the text, return None so
    callers can fall back to greedy-with-collapse and the auto-fit loop
    can detect the overflow."""
    words = "A reasonably long sentence that does not fit in twenty chars at all".split()
    assert video._balanced_wrap_lines(words, max_chars=20, max_lines=2) is None
    # Single huge word that exceeds max_chars by itself: also None.
    assert video._balanced_wrap_lines(
        ["Pneumonoultramicroscopicsilicovolcanoconiosis"], max_chars=20, max_lines=4
    ) is None


def test_fit_title_band_prefers_two_balanced_lines_over_three_uneven() -> None:
    """The cinema-style regression case: the long title used to produce
    33/24/12-char lines because Phase A bumped max_lines too eagerly.
    Prefer-fewer-lines + balanced wrap gives 33/37 instead."""
    font_size, wrapped, _band = video._fit_title_band(
        "That kind of twist that makes you rewatch every quiet room differently.",
        canvas_width=1080,
        requested_font_size=54,
    )
    lines = [line for line in wrapped.split("\n") if line.strip()]
    assert len(lines) == 2  # 2 balanced lines, not 3 uneven ones
    widths = [len(line) for line in lines]
    # Lines should be close in width — diff under 6 chars matches the
    # cinema.defined reference aesthetic.
    assert max(widths) - min(widths) <= 6
    # Font shrinks one step (54 -> 50) to clear the overflowing second line;
    # Past Moments Black keeps the title large rather than shrinking further.
    assert font_size <= 50


def test_fit_title_band_cinematic_soft_uses_greedy_two_line_wrap() -> None:
    """Cinema titles keep a compact body font (~46px at 1080px) and wrap
    greedily into a natural long-first-line pull quote rather than balancing
    into a justified block. Emphasis comes from the bold keyword, not a larger
    body font."""
    font_size, wrapped, band_height = video._fit_title_band(
        "The reveal that shook the internet and saved the MCU.",
        canvas_width=1080,
        requested_font_size=58,
        title_font_name="comic_italic",
    )

    assert font_size == 46
    assert wrapped == "The reveal that shook the internet\nand saved the MCU."
    # Cinema band uses the tall 168px vertical padding (the "large gap" rule),
    # so a 2-line title clears the default ~221px band height.
    assert band_height >= 271


def test_fit_title_band_cinema_bold_rounded_uses_same_large_gap_rule() -> None:
    font_size, wrapped, band_height = video._fit_title_band(
        "One of the most visually stunning transitions ever made.",
        canvas_width=1080,
        requested_font_size=56,
        title_font_name="arial_rounded_bold",
    )

    assert font_size == 46
    assert wrapped == "One of the most visually stunning\ntransitions ever made."
    assert band_height >= 271


def test_wrapped_overflows_detects_collapsed_line() -> None:
    """_wrapped_overflows is the signal the title-band auto-fit loop uses to
    notice that _wrap_single_paragraph had to collapse extra lines onto the
    last line. A clean wrap has no overflowing line; the collapsed case does."""
    clean_wrap = "That kind of twist that\nmakes you rewatch"
    assert video._wrapped_overflows(clean_wrap, max_chars=28) is False
    collapsed_wrap = (
        "That kind of twist that\nmakes you rewatch every quiet room different"
    )
    assert video._wrapped_overflows(collapsed_wrap, max_chars=28) is True
    # Blank lines (paragraph spacer) are ignored:
    with_blank = "Them: hi\n\nMe:"
    assert video._wrapped_overflows(with_blank, max_chars=10) is False


def test_fit_title_band_autofits_long_cinema_title() -> None:
    """Long cinema-style titles (10-20 words) must auto-shrink + grow lines
    so wrapped lines stay inside the canvas. Regression: the 12-word
    "That kind of twist that makes you rewatch every quiet room different"
    used to collapse 3 natural lines onto 2 and clip both canvas edges."""
    font_size, wrapped, band_height = video._fit_title_band(
        "That kind of twist that makes you rewatch every quiet room different",
        canvas_width=1080,
        requested_font_size=54,
    )
    # Past Moments Black intentionally renders a large title, and this 2-line
    # wrap already fits at the requested size — so the real regression guard is
    # the no-clip check below (plus the "tail doesn't survive" assertion), not
    # forced shrinking. Stay within the auto-fit floor and the requested ceiling.
    assert 38 <= font_size <= 54
    # No line clips the canvas: every wrapped line must stay within ~92% of
    # the canvas at the chosen font. Approximate char width ≈ font_size * 0.55.
    # The original bug produced a 44-char second line at font 54 that
    # extended ~1320px on a 1080px canvas.
    char_width = max(1, int(font_size * 0.55))
    canvas_width = 1080
    max_safe_chars = int((canvas_width * 0.92) / char_width)
    for line in wrapped.split("\n"):
        if line.strip():
            assert len(line) <= max_safe_chars, f"line {line!r} too wide"
    # Specifically, the previously-overflowing tail must not survive as a
    # single line — that's the regression marker.
    assert "makes you rewatch every quiet room different" not in wrapped
    # Band height stays sane (not blown out, not collapsed):
    assert 170 <= band_height <= 520


def test_fit_title_band_short_title_unchanged_after_autofit() -> None:
    """Auto-fit must NOT touch short titles — 1- and 2-line content should
    render at the original font size and band height."""
    font_size, wrapped, band_height = video._fit_title_band(
        "Focus Mode Activated",
        canvas_width=1080,
        requested_font_size=54,
    )
    assert font_size == 54
    assert wrapped == "Focus Mode Activated"
    assert band_height == 172


def test_fit_title_band_bumps_cap_for_multi_paragraph_titles() -> None:
    """Template B titles need more vertical room — the band cap must rise
    from 320 to 480 once line_count >= 3 so the second paragraph isn't
    truncated below the band."""
    _, _, single_paragraph_band = video._fit_title_band(
        "A nice short single line title",
        canvas_width=1080,
        requested_font_size=54,
    )
    _, wrapped_two_paragraph, two_paragraph_band = video._fit_title_band(
        'Them: "you\'re so sweet and kind!"\n\nMe when I drive:',
        canvas_width=1080,
        requested_font_size=54,
    )
    # Single-paragraph titles still respect the original 320 cap:
    assert single_paragraph_band <= 320
    # Two-paragraph titles now get up to 480 of headroom:
    assert "\n\n" in wrapped_two_paragraph
    assert two_paragraph_band <= 480
    # And the multi-paragraph band must actually be taller than the
    # single-line one — otherwise the cap bump did nothing:
    assert two_paragraph_band > single_paragraph_band


def test_title_band_filter_chain_skips_blank_lines_but_preserves_gap() -> None:
    """Sanity check the FFmpeg filter chain: blank lines reserve vertical
    space (so the visual gap shows up) but drawtext is only emitted for
    visible lines, so the [title0]/[title1]/[title]/... chain stays
    contiguous and doesn't reference a skipped index."""
    filter_string = video._title_band_filter_complex(
        'Them: hi\n\nMe:',
        crop="crop=1080:1920:0:0",
        crop_width=1080,
        crop_height=1920,
        font_path=Path("dummy.ttf"),
        requested_font_size=54,
        title_font_name=None,
        title_color="white",
        title_text_dir=Path("."),
        duration_seconds=2.0,
    )
    # Exactly two drawtext nodes (one per non-empty line):
    assert filter_string.count("drawtext=") == 2
    # Filter chain is contiguous — [titlebase] -> [title0] -> [title]
    # (the final node's output label is always [title], not [title1]):
    assert "[titlebase]drawtext" in filter_string
    assert "[title0]drawtext" in filter_string
    assert "[title]" in filter_string
    # The two drawtext y= values must differ by MORE than one line of
    # spacing (because the blank-line slot adds its own line of vertical
    # offset between them):
    import re as _re
    y_values = sorted(int(m.group(1)) for m in _re.finditer(r"y=(\d+)", filter_string))
    assert len(y_values) == 2
    # We pull line_spacing the same way _title_band_filter_complex does so
    # the threshold tracks the production formula exactly.
    font_size, _, _ = video._fit_title_band(
        'Them: hi\n\nMe:', canvas_width=1080, requested_font_size=54,
    )
    single_step = font_size + max(10, int(font_size * 0.24))
    gap = y_values[1] - y_values[0]
    # The two drawtext lines must be at least TWO line-steps apart (one
    # for the visible second line + one for the blank-line slot between).
    assert gap >= 2 * single_step - 4  # small tolerance for int rounding


def test_title_band_centers_video_content_not_combined_stack() -> None:
    """Wide meme footage should not be pushed low by the added title band.

    Regression: centering the combined title+content block puts the video
    center below the 9:16 canvas center by half the title-band height.
    """
    filter_string = video._title_band_filter_complex(
        "When you finally understand remote work\n\nMe:",
        crop="crop=964:418:58:828",
        crop_width=964,
        crop_height=418,
        font_path=Path("dummy.ttf"),
        requested_font_size=54,
        title_font_name=None,
        title_color="white",
        title_text_dir=Path("."),
        duration_seconds=2.0,
    )

    assert "scale=1080:468" in filter_string
    assert "color=c=black:s=1080x370" in filter_string
    assert "[block]pad=1080:1920:(ow-iw)/2:356:color=black[vout]" in filter_string


def test_title_band_insets_cinema_viral_bold_content_width() -> None:
    filter_string = video._title_band_filter_complex(
        "One of the most visually stunning\ntransitions ever made.",
        crop="crop=964:418:58:828",
        crop_width=964,
        crop_height=418,
        font_path=Path("dummy.ttf"),
        requested_font_size=56,
        title_font_name="arial_rounded_bold",
        title_color="white",
        title_text_dir=Path("."),
        duration_seconds=2.0,
    )

    # 56px explicit margin each side: cinema_w=968, left == right guaranteed
    assert "scale=968:" in filter_string
    assert ":56:0:color=black[content]" in filter_string


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


def test_parse_bold_markup_extracts_visible_text_and_flags() -> None:
    marked = '"An **animated** character **can\'t** have that much **aura**."'
    visible, flags = video._parse_bold_markup(marked)

    assert visible == '"An animated character can\'t have that much aura."'
    assert len(flags) == len(visible)
    # The three marked words are bold; surrounding text and quotes are not.
    for word in ("animated", "can't", "aura"):
        start = visible.index(word)
        assert all(flags[start : start + len(word)])
    assert flags[0] is False  # leading quote
    assert flags[visible.index("character")] is False


def test_parse_bold_markup_leaves_unmatched_marker_literal() -> None:
    # A lone, unmatched ** must not toggle bold for the rest of the string.
    visible, flags = video._parse_bold_markup("a **bold** and ** dangling")
    assert "bold" in visible
    assert not any(flags[visible.index("dangling") : visible.index("dangling") + 8])


def test_strip_bold_markers_removes_pairs_only() -> None:
    assert video._strip_bold_markers("a **b** c") == "a b c"
    assert video._strip_bold_markers("plain title") == "plain title"


def test_bold_masks_align_to_wrapped_lines() -> None:
    marked = '"An **animated** character **can\'t** have that much **aura**."'
    visible, flags = video._parse_bold_markup(marked)
    font_size, wrapped, _band = video._fit_title_band(
        visible,
        canvas_width=1080,
        requested_font_size=54,
        title_font_name="georgia",
    )
    masks = video._bold_masks_for_wrapped(visible, flags, wrapped)

    lines = wrapped.split("\n")
    assert len(masks) == len(lines)
    bold_text = ""
    for line, mask in zip(lines, masks):
        assert len(mask) == len(line)
        bold_text += "".join(ch for ch, is_bold in zip(line, mask) if is_bold)
    # Across both wrapped lines the bold characters spell exactly the marked
    # keywords, proving the projection survives the line break.
    assert bold_text == "animatedcan'taura"


def test_split_styled_runs_groups_bold_and_emoji() -> None:
    line = "an animated cat"
    mask = [False] * len(line)
    start = line.index("animated")
    for i in range(start, start + len("animated")):
        mask[i] = True

    runs = video._split_styled_runs(line, mask)

    assert "".join(text for text, _emoji, _bold in runs) == line
    bold_segments = [text for text, _emoji, is_bold in runs if is_bold]
    assert bold_segments == ["animated"]
    assert all(not is_emoji for _text, is_emoji, _bold in runs)


def test_split_styled_runs_without_mask_is_single_text_run() -> None:
    runs = video._split_styled_runs("plain words", None)
    assert runs == [("plain words", False, False)]


def test_bold_font_file_maps_known_fonts_and_skips_unknown() -> None:
    # On non-Windows hosts windows_font_file returns None, so only assert the
    # mapping contract: known fonts resolve through a bold sibling, unknown
    # fonts return None (bold gracefully disabled).
    assert "georgia" in video._BOLD_FONT_SIBLINGS
    assert video._BOLD_FONT_SIBLINGS["georgia"] == "georgia_bold"
    assert video._bold_font_file("totally_unknown_font") is None
