from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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


# --- Section download (speed fix: fetch only the chosen span) --------------- #


def test_ensure_local_source_caps_resolution_and_never_uses_ranges(monkeypatch, tmp_path) -> None:
    """download_ranges is what routes the job to ffmpeg's slow seek — assert it is absent."""
    captured: dict = {}

    class FakeYDL:
        def __init__(self, options):
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=True):
            captured["url"] = url
            (tmp_path / "source.mp4").write_bytes(b"video")
            return {"title": "CardBound"}

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        clip_studio,
        "probe_video",
        lambda _p: SimpleNamespace(width=1280, height=720, duration_seconds=5249.0),
    )

    result = clip_studio.ensure_local_source("https://youtu.be/abc", tmp_path, max_height=1080)

    assert "download_ranges" not in captured["options"]
    assert "height<=1080" in captured["options"]["format"]
    assert result["from_cache"] is False
    assert result["video_path"].endswith("source.mp4")


def test_ensure_local_source_reuses_the_cached_download(monkeypatch, tmp_path) -> None:
    """Several clips come out of one source, so the download is paid once."""
    (tmp_path / "source.mp4").write_bytes(b"video")

    def fail_download(*_args, **_kwargs):
        raise AssertionError("a cached source must not be downloaded again")

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", fail_download)
    monkeypatch.setattr(
        clip_studio,
        "probe_video",
        lambda _p: SimpleNamespace(width=1280, height=720, duration_seconds=5249.0),
    )

    result = clip_studio.ensure_local_source("https://youtu.be/abc", tmp_path)

    assert result["from_cache"] is True
    assert result["video_path"].endswith("source.mp4")


def test_render_previews_cuts_each_moment_and_carries_its_context(monkeypatch, tmp_path) -> None:
    """Previews exist to be judged by eye, and to ground the title/caption prompt."""
    calls: list[list[str]] = []
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    monkeypatch.setattr(clip_studio, "ffmpeg_binary", lambda: Path("ffmpeg"))

    def fake_run(command, **_kwargs):
        calls.append(command)
        # The real ffmpeg writes the output file; visual_activity stats it.
        Path(command[-1]).write_bytes(b"x" * 400_000)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clip_studio.subprocess, "run", fake_run)

    moments = [
        clip_studio.SourceMoment(
            start=float(start), end=float(start + 15), duration=15.0, score=9.0,
            range_label="00:00–00:15", length_note="ideal",
            reasons=("big money: $400,000",), context=f"context {start}",
        )
        for start in (100, 200, 300)
    ]

    previews = clip_studio.render_previews(source, moments, tmp_path / "out", count=2)

    assert len(previews) == 2, "count must cap the batch"
    # ffprobe now runs alongside each cut to measure activity; count only the cuts.
    cuts = [command for command in calls if "-ss" in command]
    assert len(cuts) == 2
    assert "-ss" in calls[0] and f"{100.0:.3f}" in calls[0]
    # The window's own words are what the title/caption prompt is built from.
    assert previews[0]["context"] == "context 100"
    assert previews[0]["reasons"] == ["big money: $400,000"]


def test_visual_activity_flags_a_locked_off_shot() -> None:
    """A talking head compresses to almost nothing; a cutting sequence does not.

    Calibrated on a real documentary. The original figures — 22.8 KB/s for a
    static interview shot, 67.2 KB/s for a segment showing the artifact — were
    whole-file rates that included the 128 kbps audio track. The measurement now
    reads video packets only, so the same two shots are 7 and 51 KB/s, and the
    threshold moved with them. Re-measured on that source: the interview shot
    that was 22.8 whole-file is 7.4 video-only.
    """
    packets_static = [(index * 0.5, int(7.0 * 1024 * 0.5)) for index in range(30)]
    packets_lively = [(index * 0.5, int(51.0 * 1024 * 0.5)) for index in range(30)]
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_size=0))

    assert clip_studio._visual_activity(path, 15.0, packets_static)["looks_static"] is True
    assert clip_studio._visual_activity(path, 15.0, packets_lively)["looks_static"] is False
    assert clip_studio._visual_activity(path, 15.0, packets_lively)["kb_per_second"] == 51.0
    # A zero-length preview must not divide by zero.
    assert clip_studio._visual_activity(path, 0.0, packets_static)["looks_static"] is True


def test_visual_payoff_spots_a_cutaway_after_the_talking_stops() -> None:
    """The reported failure: the words land, then the card is finally shown.

    Figures taken from the measured source — a locked-off talking head at
    7.4 KB/s whose tail jumps to 30.9 KB/s once he stops speaking.
    """
    body = [(index * 0.5, int(7.4 * 1024 * 0.5)) for index in range(34)]
    tail = [(17.0 + index * 0.5, int(30.9 * 1024 * 0.5)) for index in range(8)]
    path = SimpleNamespace()

    payoff = clip_studio._visual_payoff(path, 17.0, body + tail)

    assert payoff["detected"] is True
    assert payoff["extra_seconds"] > 0


def test_visual_payoff_ignores_a_tail_that_is_merely_less_still() -> None:
    """A quiet tail after a still body must not read as a reveal."""
    body = [(index * 0.5, int(7.5 * 1024 * 0.5)) for index in range(34)]
    tail = [(17.0 + index * 0.5, int(10.6 * 1024 * 0.5)) for index in range(8)]

    payoff = clip_studio._visual_payoff(SimpleNamespace(), 17.0, body + tail)

    assert payoff["detected"] is False


def test_visual_payoff_is_absent_without_lookahead_footage() -> None:
    body = [(index * 0.5, int(20.0 * 1024 * 0.5)) for index in range(34)]

    assert clip_studio._visual_payoff(SimpleNamespace(), 17.0, body)["detected"] is False


def test_download_source_section_requests_only_the_padded_span(monkeypatch, tmp_path) -> None:
    """The whole point is not downloading the full source, so assert the range."""
    captured: dict = {}

    class FakeYDL:
        def __init__(self, options):
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=True):
            captured["url"] = url
            captured["download"] = download
            (tmp_path / "section.mp4").write_bytes(b"clip")
            return {"title": "CardBound"}

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        clip_studio, "probe_video", lambda _p: SimpleNamespace(width=1920, height=1080,
                                                               duration_seconds=24.0)
    )

    result = clip_studio.download_source_section(
        "https://youtu.be/abc", tmp_path, start=100.0, end=120.0, padding_seconds=2.0
    )

    ranges = captured["options"]["download_ranges"](None, None)
    assert ranges == [{"start_time": 98.0, "end_time": 122.0}]
    # Without this the section can open on grey frames — fatal for a hook.
    assert captured["options"]["force_keyframes_at_cuts"] is True
    assert result["section_start"] == 98.0
    assert result["clip_offset"] == 2.0
    assert result["video_path"].endswith("section.mp4")


def test_download_source_section_clamps_padding_at_zero(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class FakeYDL:
        def __init__(self, options):
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, _url, download=True):
            (tmp_path / "section.mp4").write_bytes(b"clip")
            return {"title": "t"}

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(
        clip_studio, "probe_video", lambda _p: SimpleNamespace(width=1080, height=1920,
                                                               duration_seconds=9.0)
    )

    result = clip_studio.download_source_section(
        "https://youtu.be/abc", tmp_path, start=1.0, end=8.0, padding_seconds=5.0
    )

    assert captured["options"]["download_ranges"](None, None)[0]["start_time"] == 0.0
    assert result["clip_offset"] == 1.0


def test_download_source_section_rejects_an_inverted_range(tmp_path) -> None:
    with pytest.raises(ValueError, match="must be after start"):
        clip_studio.download_source_section(
            "https://youtu.be/abc", tmp_path, start=50.0, end=50.0
        )


def test_plan_url_ranks_without_downloading_the_video(monkeypatch, tmp_path) -> None:
    """plan_url is analyze_url minus the expensive half."""
    called: dict = {}

    def fail_download(*_args, **_kwargs):
        called["downloaded"] = True
        raise AssertionError("plan_url must not download the source video")

    srt = tmp_path / "transcript.en-orig.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:04,000\nIt sold for $400,000.\n\n", encoding="utf-8")

    monkeypatch.setattr(clip_studio, "download_source", fail_download)
    monkeypatch.setattr(clip_studio, "fetch_transcript", lambda _u, _d: srt)

    result = clip_studio.plan_url("https://youtu.be/abc", tmp_path)

    assert "downloaded" not in called
    assert result["transcript_available"] is True
    assert result["url"] == "https://youtu.be/abc"
    assert isinstance(result["moments"], list)


def test_plan_url_returns_no_moments_without_a_transcript(monkeypatch, tmp_path) -> None:
    """Wordless trailers fall back to manual timestamps rather than erroring."""
    monkeypatch.setattr(clip_studio, "fetch_transcript", lambda _u, _d: None)

    result = clip_studio.plan_url("https://youtu.be/trailer", tmp_path)

    assert result["transcript_available"] is False
    assert result["moments"] == []


def test_previews_carry_the_entry_point_flags(monkeypatch, tmp_path) -> None:
    """The review card needs to say where the in-point was already fixed.

    Without these two keys the operator cannot tell an opening that was cleaned
    up from one that still lands mid-sentence, which is the whole point of
    surfacing them instead of silently adjusting the timestamps.
    """
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(clip_studio, "ffmpeg_binary", lambda: Path("ffmpeg"))

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"x" * 400_000)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clip_studio.subprocess, "run", fake_run)

    moments = [
        clip_studio.SourceMoment(
            start=10.0, end=25.0, duration=15.0, score=9.0, range_label="00:10–00:25",
            length_note="ideal", reasons=(), context="words",
            opening_trimmed=2.4, opens_mid_thought=False,
        ),
        clip_studio.SourceMoment(
            start=40.0, end=55.0, duration=15.0, score=8.0, range_label="00:40–00:55",
            length_note="ideal", reasons=(), context="words",
            opening_trimmed=0.0, opens_mid_thought=True,
        ),
    ]
    previews = clip_studio.render_previews(source, moments, tmp_path / "out", count=2)

    assert previews[0]["opening_trimmed"] == 2.4
    assert previews[0]["opens_mid_thought"] is False
    assert previews[1]["opening_trimmed"] == 0.0
    assert previews[1]["opens_mid_thought"] is True


def test_render_clip_passes_the_account_header_for_a_post_header_template(
    monkeypatch, tmp_path
) -> None:
    """A post-header template without an account renders a headerless clip.

    That silently diverges from what the same account exports in Processing, so
    the account's header must reach export_cropped_video.
    """
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    captured: dict = {}

    monkeypatch.setattr(clip_studio, "ffmpeg_binary", lambda: Path("ffmpeg"))
    monkeypatch.setattr(clip_studio.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(
        clip_studio, "suggest_crop_settings", lambda _p: SimpleNamespace(crop=None)
    )
    monkeypatch.setattr(
        clip_studio, "export_cropped_video", lambda **kwargs: captured.update(kwargs)
    )
    sentinel = object()
    monkeypatch.setattr(clip_studio, "_post_header_for", lambda account_id: sentinel)

    clip_studio.render_clip(
        source, tmp_path / "out.mp4", 0.0, 10.0, "Title",
        template="historytrails_post_header", account_id=7,
    )
    assert captured["post_header"] is sentinel

    captured.clear()
    clip_studio.render_clip(
        source, tmp_path / "out2.mp4", 0.0, 10.0, "Title",
        template="historytrails_left", account_id=7,
    )
    # A template without post_header must not grow one just because an account
    # was supplied.
    assert captured["post_header"] is None


def test_transcribe_source_caches_the_srt(monkeypatch, tmp_path) -> None:
    """Whisper is minutes per source, so a second call must not re-run it."""
    calls: list[Path] = []

    def fake_transcribe(video, out, **_kwargs):
        calls.append(video)
        out.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello\n\n", encoding="utf-8")
        return out

    monkeypatch.setattr(clip_studio.transcription, "transcribe_to_srt", fake_transcribe)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    first = clip_studio.transcribe_source(video, tmp_path / "ws")
    second = clip_studio.transcribe_source(video, tmp_path / "ws")

    assert first == second
    assert len(calls) == 1, "the cached SRT must be reused"


def test_plan_local_file_ranks_without_a_url(monkeypatch, tmp_path) -> None:
    """A file off disk reaches the same review batch a URL does."""
    srt = (
        "1\n00:00:00,000 --> 00:00:09,000\n"
        "The buyer paid $400,000 for the rarest card ever graded.\n\n"
        "2\n00:00:09,000 --> 00:00:18,000\n"
        "It was the biggest sale the hobby had ever seen.\n\n"
    )

    def fake_transcribe(video, out, **_kwargs):
        out.write_text(srt, encoding="utf-8")
        return out

    monkeypatch.setattr(clip_studio.transcription, "transcribe_to_srt", fake_transcribe)
    monkeypatch.setattr(
        clip_studio, "probe_video",
        lambda _p: SimpleNamespace(width=1920, height=1080, duration_seconds=18.0),
    )
    monkeypatch.setattr(
        clip_studio, "render_previews",
        lambda source, moments, out_dir, count=8: [{"index": i} for i, _ in enumerate(moments)],
    )
    video = tmp_path / "local.mp4"
    video.write_bytes(b"x")

    result = clip_studio.plan_local_file(video, tmp_path / "ws")

    assert result["url"] is None, "a local file has no source URL"
    assert result["transcript_available"] is True
    assert result["moments"], "the transcript should yield at least one ranked moment"
    assert result["previews"], "ranked moments must come back with previews to watch"
    assert result["source"]["video_path"] == str(video.resolve())


def test_plan_local_file_rejects_a_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        clip_studio.plan_local_file(tmp_path / "nope.mp4", tmp_path / "ws")


def test_plan_local_file_feeds_the_roster_to_the_transcriber(monkeypatch, tmp_path) -> None:
    """A mangled name is a celebrity signal that never fires.

    Measured on a real interview: "Noah Lyles" decoded as "Noah Laos" without
    the roster and correctly with it, so the roster must reach whisper and not
    only the ranker.
    """
    seen: dict = {}

    def fake_transcribe(video, out, **kwargs):
        seen.update(kwargs)
        out.write_text(
            "1\n00:00:00,000 --> 00:00:09,000\nNoah Lyles paid $400,000 for it.\n\n",
            encoding="utf-8",
        )
        return out

    monkeypatch.setattr(clip_studio.transcription, "transcribe_to_srt", fake_transcribe)
    monkeypatch.setattr(
        clip_studio, "probe_video",
        lambda _p: SimpleNamespace(width=1920, height=1080, duration_seconds=9.0),
    )
    monkeypatch.setattr(clip_studio, "render_previews", lambda *a, **k: [])
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    clip_studio.plan_local_file(
        video, tmp_path / "ws", celebrity_names=("Noah Lyles", "Logan Paul")
    )
    assert seen["vocabulary"] == ("Noah Lyles", "Logan Paul")


def test_render_clip_burns_the_window_transcript(monkeypatch, tmp_path) -> None:
    """A source the viewer cannot follow by ear needs the words on screen."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    transcript = tmp_path / "t.srt"
    transcript.write_text(
        "1\n00:00:10,000 --> 00:00:13,000\nInside the window.\n\n"
        "2\n00:01:40,000 --> 00:01:43,000\nFar outside the window.\n\n",
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setattr(clip_studio, "ffmpeg_binary", lambda: Path("ffmpeg"))
    monkeypatch.setattr(clip_studio.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(clip_studio, "suggest_crop_settings", lambda _p: SimpleNamespace(crop=None))

    def fake_export(**kwargs):
        # The SRT lives in a temp dir that is gone once render_clip returns, so
        # read it here — where ffmpeg would.
        captured.update(kwargs)
        subtitles = kwargs.get("subtitles_path")
        captured["srt_body"] = subtitles.read_text(encoding="utf-8") if subtitles else None

    monkeypatch.setattr(clip_studio, "export_cropped_video", fake_export)

    clip_studio.render_clip(
        source, tmp_path / "out.mp4", 9.0, 20.0, "Title",
        transcript_path=transcript, burn_captions=True,
    )
    body = captured["srt_body"]
    assert body is not None
    assert "Inside the window." in body
    assert "Far outside" not in body, "lines outside the cut must not be burned in"
    # The cut segment starts at t=0, so captions must be rebased off the source clock.
    assert "00:00:0" in body


def test_render_clip_without_captions_passes_no_subtitles(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    captured: dict = {}
    monkeypatch.setattr(clip_studio, "ffmpeg_binary", lambda: Path("ffmpeg"))
    monkeypatch.setattr(clip_studio.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(clip_studio, "suggest_crop_settings", lambda _p: SimpleNamespace(crop=None))
    monkeypatch.setattr(clip_studio, "export_cropped_video", lambda **kw: captured.update(kw))

    clip_studio.render_clip(source, tmp_path / "out.mp4", 0.0, 10.0, "Title")
    assert captured["subtitles_path"] is None


def test_render_clip_requires_a_transcript_to_burn_captions(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    with pytest.raises(ValueError):
        clip_studio.render_clip(
            source, tmp_path / "out.mp4", 0.0, 10.0, "Title", burn_captions=True
        )
