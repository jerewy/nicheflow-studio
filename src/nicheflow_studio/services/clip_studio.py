"""Clip Studio service — turn a long source video into short, templated clips.

This ties together the pieces built for the clip-and-earn (campaign) workflow so
the whole thing runs from one place instead of ad-hoc scripts:

1. ``download_source`` — pull a source URL to disk (yt-dlp).
2. ``fetch_transcript`` — grab a timestamped transcript (yt-dlp auto-subs).
3. ``rank_moments`` — score the most clippable moments (virality analyzer).
4. ``render_clip`` — cut a chosen moment and render it through the app's *real*
   template renderer: content auto-crop + a top-band title (HistoryTrails etc.),
   matching what the Processing screen's "Export Reel" produces.

``render_clip`` is intentionally the same recipe the Processing pipeline uses
(``suggest_crop_settings`` + ``TEMPLATE_RENDER_CONFIG`` + ``export_cropped_video``)
so a clip looks identical to the account's normal reels. Analysis is heuristic
and transcript-based, so it only helps on talky long-form (docs, interviews),
not near-wordless trailers.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.media_tools import ffmpeg_binary, subprocess_run_kwargs
from nicheflow_studio.downloader.youtube import download_youtube_url
from nicheflow_studio.downloader.yt_dlp_sidecar import yt_dlp_sidecar_path
from nicheflow_studio.processing import transcript_clips, virality
from nicheflow_studio.processing.video import (
    CropSettings,
    export_cropped_video,
    probe_video,
    suggest_crop_settings,
)
from nicheflow_studio.services.processing_workflow import TEMPLATE_RENDER_CONFIG

# Campaign floor most clip campaigns use ("over 7 seconds").
DEFAULT_MIN_CLIP_SECONDS = 8.0


@dataclass(frozen=True)
class SourceMoment:
    """A ranked, clippable moment surfaced for review (UI/JSON friendly)."""

    start: float
    end: float
    duration: float
    score: float
    range_label: str
    length_note: str
    reasons: tuple[str, ...]
    context: str


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _cut_segment(
    source_path: Path,
    start_seconds: float,
    end_seconds: float,
    output_path: Path,
) -> Path:
    """Cut ``[start, end)`` out of ``source_path`` (re-encoded, frame-accurate)."""
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")
    duration = end_seconds - start_seconds
    if duration <= 0:
        raise ValueError("end_seconds must be greater than start_seconds.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg_path),
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source_path),
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, **subprocess_run_kwargs())
    return output_path


def render_clip(
    source_path: Path,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
    title: str,
    *,
    template: str = "historytrails_left",
    auto_crop: bool = True,
    audio_mode: str = "keep",
    cancel_event: "threading.Event | None" = None,
) -> Path:
    """Cut a moment and render it through the app's real template renderer.

    Same pipeline as Processing's Export Reel: cut the segment, run the content
    auto-crop (``suggest_crop_settings``) to isolate the footage from any bars,
    then ``export_cropped_video`` with the chosen template's font/layout so the
    title sits in a band directly above the video.
    """
    config = TEMPLATE_RENDER_CONFIG.get(template) or TEMPLATE_RENDER_CONFIG["gaming_meme_black"]
    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.exists():
        raise FileNotFoundError(f"Source video not found: {resolved_source}")

    with tempfile.TemporaryDirectory(prefix="clipstudio-cut-") as temp_dir:
        segment = _cut_segment(
            resolved_source, start_seconds, end_seconds, Path(temp_dir) / "segment.mp4"
        )
        crop = suggest_crop_settings(segment).crop if auto_crop else CropSettings()
        export_cropped_video(
            input_path=segment,
            output_path=output_path,
            crop=crop,
            title_text=title,
            title_layout=config["layout"],
            title_font_size=config["font_size"],
            title_font_name=config["font_name"],
            title_color=config["color"],
            title_align=config.get("align", "center"),
            title_line_gap_scale=config.get("line_gap_scale"),
            enable_bold_keywords=config.get("bold_keywords", False),
            audio_mode=audio_mode,
            cancel_event=cancel_event,
        )
    return output_path


def download_source(url: str, output_dir: Path) -> dict:
    """Download a source URL to ``output_dir``; return path + metadata."""
    result = download_youtube_url(url=url, output_dir=output_dir)
    probe = probe_video(result.file_path)
    return {
        "video_path": str(result.file_path),
        "title": result.title,
        "width": probe.width,
        "height": probe.height,
        "duration_seconds": probe.duration_seconds,
    }


def fetch_transcript(url: str, output_dir: Path) -> Path | None:
    """Download the original English auto-caption track as an SRT, if available.

    Prefers the genuine ``en-orig`` / ``en`` track over auto-translated tracks
    (which are garbage for an English-spoken source). Returns the SRT path, or
    ``None`` when no usable English captions exist (e.g. a music-only trailer).
    """
    from yt_dlp import YoutubeDL

    output_dir.mkdir(parents=True, exist_ok=True)
    template = str(output_dir / "transcript.%(ext)s")
    options: dict[str, object] = {
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en-orig", "en"],
        "subtitlesformat": "srt/vtt",
        "convertsubtitles": "srt",
        "outtmpl": template,
        "quiet": True,
        "no_warnings": True,
    }
    sidecar = yt_dlp_sidecar_path()
    if sidecar is None:
        with YoutubeDL(options) as ydl:
            ydl.download([url])
    else:
        # Keep the sidecar path consistent with the app's downloader, but the
        # subtitle-only download is light enough to run in-process either way.
        with YoutubeDL(options) as ydl:
            ydl.download([url])

    for candidate in sorted(output_dir.glob("transcript*.srt")):
        return candidate
    return None


def rank_moments(
    srt_path: Path,
    *,
    celebrity_names: tuple[str, ...] = (),
    top_n: int = 10,
    min_seconds: float = DEFAULT_MIN_CLIP_SECONDS,
) -> list[SourceMoment]:
    """Rank the most clippable moments from an SRT transcript for review."""
    sentences = transcript_clips.sentences_from_srt_file(srt_path)
    ranked = virality.rank_moments(
        sentences,
        celebrity_names=celebrity_names,
        top_n=top_n,
        min_seconds=min_seconds,
    )
    moments: list[SourceMoment] = []
    for moment in ranked:
        moments.append(
            SourceMoment(
                start=round(moment.start, 1),
                end=round(moment.end, 1),
                duration=round(moment.duration, 1),
                score=moment.score,
                range_label=f"{_mmss(moment.start)}–{_mmss(moment.end)}",
                length_note=moment.length_note,
                reasons=tuple(moment.reasons),
                context=moment.text,
            )
        )
    return moments


def analyze_url(
    url: str,
    output_dir: Path,
    *,
    celebrity_names: tuple[str, ...] = (),
    top_n: int = 10,
) -> dict:
    """One-shot: download the source, transcribe it, and rank its moments.

    Returns everything the Clip Studio screen needs to render the review view:
    the downloaded video path, its metadata, and the ranked moments. ``moments``
    is empty when the source has no usable transcript (e.g. a wordless trailer) —
    the UI then falls back to manual timestamp entry.
    """
    source = download_source(url, output_dir)
    srt = fetch_transcript(url, output_dir)
    moments = rank_moments(srt, celebrity_names=celebrity_names, top_n=top_n) if srt else []
    return {
        **source,
        "transcript_available": srt is not None,
        "moments": [moment.__dict__ for moment in moments],
    }
