from __future__ import annotations

from pathlib import Path

from nicheflow_studio.processing.video import CropSettings, CropSuggestion
from nicheflow_studio.services import clip_studio


def test_render_clip_uses_template_config_and_autocrop(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    out = tmp_path / "clip.mp4"

    # Stub the heavy steps: cut writes a placeholder, crop is a known value,
    # export just captures the kwargs it was handed.
    monkeypatch.setattr(
        clip_studio, "_cut_segment", lambda s, a, b, o: (o.write_bytes(b"seg"), o)[1]
    )
    monkeypatch.setattr(
        clip_studio,
        "suggest_crop_settings",
        lambda _p: CropSuggestion(
            crop=CropSettings(top=616, bottom=616),
            reasons=(),
            used_border_detection=True,
            used_ocr=False,
        ),
    )
    captured: dict[str, object] = {}

    def fake_export(**kwargs):
        captured.update(kwargs)
        return kwargs["output_path"]

    monkeypatch.setattr(clip_studio, "export_cropped_video", fake_export)

    result = clip_studio.render_clip(
        source, out, 100.0, 118.0, "Hook line", template="historytrails_left"
    )

    assert result == out
    # Exact historytrails_left template config flows into the app's renderer.
    assert captured["title_layout"] == "top_band"
    assert captured["title_font_name"] == "arial"
    assert captured["title_font_size"] == 54
    assert captured["title_align"] == "left"
    assert captured["title_line_gap_scale"] == 0.20
    assert captured["title_text"] == "Hook line"
    # The content auto-crop is applied, not an empty crop.
    assert captured["crop"] == CropSettings(top=616, bottom=616)


def test_render_clip_falls_back_to_known_template(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(clip_studio, "_cut_segment", lambda s, a, b, o: (o.write_bytes(b"s"), o)[1])
    monkeypatch.setattr(
        clip_studio,
        "suggest_crop_settings",
        lambda _p: CropSuggestion(CropSettings(), (), False, False),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        clip_studio, "export_cropped_video", lambda **k: (captured.update(k), k["output_path"])[1]
    )

    clip_studio.render_clip(
        source, tmp_path / "o.mp4", 0.0, 10.0, "T", template="does_not_exist", auto_crop=False
    )
    # Unknown template resolves to the safe default instead of crashing.
    assert captured["title_layout"] == "top_band"
    assert captured["crop"] == CropSettings()  # auto_crop=False keeps it empty


def test_rank_moments_returns_ui_ready_moments(tmp_path: Path) -> None:
    srt = tmp_path / "t.srt"
    srt.write_text(
        "1\n00:00:04,000 --> 00:00:08,000\nJust some ordinary small talk about nothing here.\n\n"
        "2\n00:00:30,000 --> 00:00:35,000\nThis is the rarest card ever and it sold for $400,000.\n\n"
        "3\n00:00:35,000 --> 00:00:40,000\nHonestly the most insane discovery, nobody knew it existed.\n",
        encoding="utf-8",
    )
    moments = clip_studio.rank_moments(srt, top_n=3)

    assert moments, "expected at least one ranked moment"
    top = moments[0]
    assert "–" in top.range_label
    assert top.score > 0
    assert top.duration >= 0
    assert isinstance(top.reasons, tuple)
    assert top.length_note in {"ideal", "long", "short"}
    # The money/superlative beat outranks the small talk.
    assert "400,000" in top.context
