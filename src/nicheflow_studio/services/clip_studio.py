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
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.media_tools import (
    ffmpeg_binary,
    ffprobe_binary,
    subprocess_run_kwargs,
)
from nicheflow_studio.downloader.youtube import download_youtube_url
from nicheflow_studio.downloader.yt_dlp_sidecar import yt_dlp_sidecar_path
from nicheflow_studio.processing import transcript_clips, transcription, virality
from nicheflow_studio.processing.video import (
    CropSettings,
    default_caption_style,
    escape_ffmpeg_path,
    export_cropped_video,
    probe_video,
    suggest_crop_settings,
)
from nicheflow_studio.services.processing_workflow import TEMPLATE_RENDER_CONFIG

# Campaign floor most clip campaigns use ("over 7 seconds").
DEFAULT_MIN_CLIP_SECONDS = 8.0

# The renderer outputs 1080 wide from a cropped region of the source, so every
# pixel above this is downloaded and then thrown away. Measured on an 87-minute
# source: 2160p is 2.5 GB, 1080p is 561 MB, 720p is 257 MB.
DEFAULT_SOURCE_MAX_HEIGHT = 1080

# How many candidates to cut for review in one go. The point is not to pick the
# single best moment automatically but to put the good ones somewhere in a batch
# small enough to watch in about two minutes.
DEFAULT_PREVIEW_COUNT = 8


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
    # Entry-point quality: how much dead opening was already cut, and whether
    # the moment still starts mid-conversation. The operator adjusts the in-point
    # in the Trim step, so this says where to look rather than deciding anything.
    opening_trimmed: float = 0.0
    opens_mid_thought: bool = False
    # The out-point equivalent: the clip stops before the speaker finishes, so it
    # plays as if it were cut off. Nudge the end in the Trim step.
    ends_mid_thought: bool = False
    # Silence held after the final word so the clip lands instead of snapping
    # shut. Part of ``end``; surfaced so the UI can say why the clip runs on.
    tail_hold: float = 0.0


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


def cut_moment(
    source_path: Path,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
    *,
    transcript_path: Path | None = None,
    burn_captions: bool = False,
) -> Path:
    """Cut a chosen moment out of the source, untemplated.

    The handoff into the library (``clip_intake.register_moment``) uses this
    rather than :func:`render_clip`: a sourced clip should reach Processing as
    raw footage and pick up its crop, title and export there, the same way every
    other reel does.

    ``burn_captions`` is the one exception to "untemplated", and deliberately so:
    subtitles are part of the footage, not of the account's styling. A clip whose
    audio the viewer cannot follow is unusable no matter what Processing does to
    it later, and Processing has no access to the source transcript to add them.
    """
    resolved_source = source_path.expanduser().resolve()
    if not burn_captions:
        return _cut_segment(resolved_source, start_seconds, end_seconds, output_path)

    with tempfile.TemporaryDirectory(prefix="clipstudio-captions-") as temp_dir:
        raw = _cut_segment(
            resolved_source, start_seconds, end_seconds, Path(temp_dir) / "raw.mp4"
        )
        subtitles = _window_subtitles(
            transcript_path, start_seconds, end_seconds, Path(temp_dir) / "captions.srt"
        )
        if subtitles is None:
            # Nothing was said in this window; the plain cut is the right output.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(raw, output_path)
            return output_path
        return _burn_subtitles(raw, subtitles, output_path)


def _burn_subtitles(video_path: Path, subtitles_path: Path, output_path: Path) -> Path:
    """Render ``subtitles_path`` into ``video_path`` at its own resolution.

    Sized against the clip's real height rather than the 1080x1920 export canvas,
    because this runs on the raw cut: Processing scales the footage afterwards
    and the captions scale with it.
    """
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")
    probe = probe_video(video_path)
    # The raw cut is all picture, so the footage rectangle is the whole frame.
    style = default_caption_style(probe.width, probe.height, probe.width, probe.height)
    escaped = escape_ffmpeg_path(subtitles_path.resolve())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg_path), "-y", "-i", str(video_path),
        "-vf", f"subtitles='{escaped}':force_style='{style}'",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, **subprocess_run_kwargs())
    return output_path


def _window_subtitles(
    transcript_path: Path | None,
    start_seconds: float,
    end_seconds: float,
    output_path: Path,
) -> Path | None:
    """Write the transcript lines inside ``[start, end)`` as a 0-based SRT.

    The cut segment starts at t=0, so absolute source timestamps would place
    every caption minutes into a twenty-second clip. ``caption_srt_for_window``
    does the rebasing (and the overlap clamping that stops two lines stacking).
    """
    if transcript_path is None:
        raise ValueError("burn_captions needs a transcript_path to take lines from.")
    resolved = Path(transcript_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Transcript not found: {resolved}")
    cues = transcript_clips.parse_srt(resolved.read_text(encoding="utf-8", errors="replace"))
    window = transcript_clips.ClipWindow(start=start_seconds, end=end_seconds, text="")
    written = transcript_clips.caption_srt_for_window(cues, window, output_path)
    # A window with no speech in it produces an empty file; burning that in is a
    # no-op that still costs a filter pass, so treat it as "no captions".
    if written is None or not written.exists() or written.stat().st_size == 0:
        return None
    return written


def _post_header_for(account_id: int | None):
    """The account's header spec (avatar, name, seal), or ``None``.

    Built inside a live session because ``build_post_header`` reads lazy ORM
    attributes off the account.
    """
    if account_id is None:
        return None
    # Imported here: the export service pulls in the whole rendering stack, and
    # the ranking half of this module must stay importable without it.
    from nicheflow_studio.db.models import Account
    from nicheflow_studio.db.session import get_session
    from nicheflow_studio.services.export import build_post_header

    with get_session() as session:
        account = session.get(Account, int(account_id))
        return build_post_header(account)


def render_clip(
    source_path: Path,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
    title: str,
    *,
    # Matches what every history account is configured to export with; callers
    # that know the destination account should pass its own template instead.
    template: str = "historytrails_post_header",
    account_id: int | None = None,
    transcript_path: Path | None = None,
    burn_captions: bool = False,
    auto_crop: bool = True,
    audio_mode: str = "keep",
    cancel_event: "threading.Event | None" = None,
) -> Path:
    """Cut a moment and render it through the app's real template renderer.

    Same pipeline as Processing's Export Reel: cut the segment, run the content
    auto-crop (``suggest_crop_settings``) to isolate the footage from any bars,
    then ``export_cropped_video`` with the chosen template's font/layout so the
    title sits in a band directly above the video.

    ``account_id`` is what makes a post-header template render its header. The
    avatar, display name and verified seal all live on the account, so without
    it a ``*_post_header`` template silently produces a headerless clip that
    does not match what the same account exports from Processing.

    ``burn_captions`` writes the window's own transcript lines into the picture,
    which is the difference between usable and unusable on a source the viewer
    cannot follow by ear. Off by default: the pooled reels this app normally
    exports already arrive with the creator's own text burned in, and a third
    text layer under the title band competes with the hook. Requires
    ``transcript_path``.
    """
    config = TEMPLATE_RENDER_CONFIG.get(template) or TEMPLATE_RENDER_CONFIG["gaming_meme_black"]
    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.exists():
        raise FileNotFoundError(f"Source video not found: {resolved_source}")
    # Checked before the cut: re-encoding a segment takes seconds and would be
    # thrown away by a failure the caller could have been told about instantly.
    if burn_captions and transcript_path is None:
        raise ValueError("burn_captions needs a transcript_path to take lines from.")

    with tempfile.TemporaryDirectory(prefix="clipstudio-cut-") as temp_dir:
        segment = _cut_segment(
            resolved_source, start_seconds, end_seconds, Path(temp_dir) / "segment.mp4"
        )
        subtitles = (
            _window_subtitles(
                transcript_path, start_seconds, end_seconds, Path(temp_dir) / "captions.srt"
            )
            if burn_captions
            else None
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
            post_header=_post_header_for(account_id) if config.get("post_header") else None,
            subtitles_path=subtitles,
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


def ensure_local_source(
    url: str,
    workspace: Path,
    *,
    max_height: int = DEFAULT_SOURCE_MAX_HEIGHT,
) -> dict:
    """Download the whole source once, resolution-capped, and cache it.

    Supersedes :func:`download_source_section` for the review workflow. Fetching
    only the chosen span reads as the cheaper option and is not: ``download_ranges``
    routes yt-dlp to the *ffmpeg* downloader, which seeks sequentially through a
    throttled single-file stream (YouTube serves these as ``proto=https``, so
    there are no fragments to skip). Measured on an 87-minute source, a
    17-second span had written nothing after 45 minutes of ffmpeg holding at
    0.6% CPU, while the entire video at 720p took 91 seconds.

    Caching is what makes this cheap overall: the workflow cuts several clips
    from one source, so the download is paid once and every later cut is a local
    ffmpeg call of about a second.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    cached = _cached_source(workspace)
    if cached is not None:
        probe = probe_video(cached)
        return {
            "video_path": str(cached),
            "title": None,
            "width": probe.width,
            "height": probe.height,
            "duration_seconds": probe.duration_seconds,
            "from_cache": True,
        }

    from yt_dlp import YoutubeDL

    options: dict[str, object] = {
        "outtmpl": str(workspace / "source.%(ext)s"),
        "format": (
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/best[height<={max_height}][ext=mp4]"
            f"/best[height<={max_height}]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        # Native downloader with parallel connections. Never set download_ranges
        # here; that is the flag that hands the job to ffmpeg (see docstring).
        "concurrent_fragment_downloads": 4,
    }
    info = _download_with_fallbacks(YoutubeDL, url, workspace, options)

    downloaded = _cached_source(workspace)
    if downloaded is None:
        raise RuntimeError(f"yt-dlp produced no source file in {workspace}.")
    probe = probe_video(downloaded)
    return {
        "video_path": str(downloaded),
        "title": info.get("title"),
        "width": probe.width,
        "height": probe.height,
        "duration_seconds": probe.duration_seconds,
        "from_cache": False,
    }


# YouTube hands out signed media URLs and rejects them with a 403 when it decides
# a client is pulling too much — which is exactly what a campaign day looks like.
# Re-extracting mints fresh URLs, and a different player client often gets served
# when the current one will not be. Measured: a source that 403'd twice in a row
# on the default client downloaded on the first retry with android_vr.
_SOURCE_PLAYER_CLIENTS = (None, "android_vr", "android", None)
_SOURCE_RETRY_BACKOFF_SECONDS = 5.0


def _download_with_fallbacks(
    youtube_dl_cls,
    url: str,
    workspace: Path,
    options: dict,
) -> dict:
    """Download ``url``, retrying with fresh URLs and alternate player clients.

    A 403 partway through a large download is not a permanent failure and must
    not surface as one: the operator has no way to act on it, and the fix is
    simply to ask again.
    """
    import time

    last_error: Exception | None = None
    for attempt, client in enumerate(_SOURCE_PLAYER_CLIENTS):
        attempt_options = dict(options)
        if client is not None:
            attempt_options["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with youtube_dl_cls(attempt_options) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as error:  # noqa: BLE001 - retried below, re-raised at the end
            last_error = error
            # Half-written files would otherwise be mistaken for a cached source.
            for leftover in workspace.glob("source.*"):
                leftover.unlink(missing_ok=True)
            if attempt < len(_SOURCE_PLAYER_CLIENTS) - 1:
                time.sleep(_SOURCE_RETRY_BACKOFF_SECONDS)

    raise RuntimeError(
        f"Could not download {url} after {len(_SOURCE_PLAYER_CLIENTS)} attempts. "
        f"YouTube may be rate-limiting this machine — waiting a few minutes usually "
        f"clears it. Last error: {last_error}"
    ) from last_error


def _cached_source(workspace: Path) -> Path | None:
    """The already-downloaded source in ``workspace``, if there is one."""
    for candidate in sorted(workspace.glob("source.*")):
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            return candidate
    return None


def download_source_section(
    url: str,
    output_dir: Path,
    *,
    start: float,
    end: float,
    padding_seconds: float = 2.0,
) -> dict:
    """Download only ``[start, end]`` of a source instead of the whole video.

    Superseded by :func:`ensure_local_source`; kept for callers that genuinely
    want a single span and can tolerate the cost. The reasoning below turned out
    to be wrong when measured: ``download_ranges`` hands the download to ffmpeg,
    which seeks sequentially through a throttled stream, so fetching a 17-second
    span from an 87-minute source ran for 45 minutes without producing a file
    while the whole video at 720p took 91 seconds. Prefer downloading once.

    The original reasoning, left for context: campaigns are won in hours, and
    pulling a 70-minute episode to cut 20 seconds out of it looks like the
    slowest step in the pipeline by a wide margin, so yt-dlp's
    ``download_ranges`` fetches just the requested span.

    ``force_keyframes_at_cuts`` re-encodes the boundary so the section starts on
    a real frame; without it the cut can open on several hundred milliseconds of
    grey, which is fatal on a clip whose first second is the hook.

    ``padding_seconds`` widens the fetched span on both sides so the caller can
    still nudge the in/out points without re-downloading.
    """
    from yt_dlp import YoutubeDL

    if end <= start:
        raise ValueError(f"Section end ({end}) must be after start ({start}).")
    output_dir.mkdir(parents=True, exist_ok=True)
    span_start = max(0.0, start - padding_seconds)
    span_end = end + padding_seconds

    options: dict[str, object] = {
        "outtmpl": str(output_dir / "section.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "download_ranges": lambda _info, _ydl: [
            {"start_time": span_start, "end_time": span_end}
        ],
        "force_keyframes_at_cuts": True,
        "quiet": True,
        "no_warnings": True,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    downloaded = sorted(output_dir.glob("section.*"))
    playable = [path for path in downloaded if path.suffix.lower() in {".mp4", ".mkv", ".webm"}]
    if not playable:
        raise RuntimeError(f"yt-dlp produced no section file in {output_dir}.")
    file_path = playable[0]
    probe = probe_video(file_path)
    return {
        "video_path": str(file_path),
        "title": info.get("title"),
        "width": probe.width,
        "height": probe.height,
        "duration_seconds": probe.duration_seconds,
        # Where the fetched span sits in the ORIGINAL timeline, so the caller can
        # translate transcript timestamps into section-relative ones.
        "section_start": span_start,
        "section_end": span_end,
        "clip_offset": start - span_start,
    }


def fetch_transcript(url: str, output_dir: Path) -> Path | None:
    """Download an English subtitle track as an SRT, if one exists.

    Human-written subtitles are tried **first**, and the distinction is not
    cosmetic. On a foreign-language source YouTube will happily serve an "en"
    automatic track that is machine translation stacked on machine
    transcription; a Korean video measured here produced "a district where you
    can be dating a midle cafec방". The same video had uploader-written English
    subtitles that are clean. Ranking a transcript that bad is worse than having
    none, because the garbage still scores.

    Returns the SRT path, or ``None`` when no English track exists at all (e.g.
    a music-only trailer) — the caller then falls back to transcribing locally.
    """
    from yt_dlp import YoutubeDL

    output_dir.mkdir(parents=True, exist_ok=True)

    def _download(*, manual: bool) -> Path | None:
        # Separate folders so a manual and an automatic track cannot overwrite
        # each other — yt-dlp names both "transcript.en.srt".
        target = output_dir / ("subs_manual" if manual else "subs_auto")
        target.mkdir(parents=True, exist_ok=True)
        options: dict[str, object] = {
            "skip_download": True,
            "writesubtitles": manual,
            "writeautomaticsub": not manual,
            "subtitleslangs": ["en-orig", "en"],
            "subtitlesformat": "srt/vtt",
            "convertsubtitles": "srt",
            "outtmpl": str(target / "transcript.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }
        with YoutubeDL(options) as ydl:
            ydl.download([url])
        for candidate in sorted(target.glob("transcript*.srt")):
            return candidate
        return None

    for manual in (True, False):
        try:
            found = _download(manual=manual)
        except Exception:  # noqa: BLE001 - a missing track must not end the run
            found = None
        if found is not None:
            return found
    return None


def transcribe_source(
    video_path: Path,
    workspace: Path,
    *,
    model_size: str = "base",
    vocabulary: tuple[str, ...] = (),
) -> Path:
    """Transcribe a local video to an SRT in ``workspace``, cached.

    The fallback for everything :func:`fetch_transcript` cannot serve: a file
    off disk, or a URL whose captions are disabled. Whisper on CPU is minutes
    rather than the seconds a caption fetch takes, so the result is cached
    beside the source and reused — the same bargain :func:`ensure_local_source`
    makes for the video itself.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    srt_path = workspace / "transcript.whisper.srt"
    if srt_path.exists() and srt_path.stat().st_size > 0:
        return srt_path
    return transcription.transcribe_to_srt(
        video_path, srt_path, model_size=model_size, vocabulary=vocabulary
    )


def plan_local_file(
    video_path: Path,
    workspace: Path,
    *,
    celebrity_names: tuple[str, ...] = (),
    top_n: int = DEFAULT_PREVIEW_COUNT,
    model_size: str = "base",
) -> dict:
    """The review workflow for a file already on disk: transcribe, rank, cut.

    Mirrors :func:`plan_and_preview` with the two network steps removed. A clip
    pack downloaded by hand and a YouTube URL therefore reach the same review
    batch, with the same ranking, trimming and flags.
    """
    resolved = video_path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Source video not found: {resolved}")

    # The roster doubles as decoding vocabulary: a name the model mangles is a
    # celebrity signal that never fires.
    srt = transcribe_source(
        resolved, workspace, model_size=model_size, vocabulary=celebrity_names
    )
    moments = rank_moments(srt, celebrity_names=celebrity_names, top_n=top_n)
    probe = probe_video(resolved)
    source = {
        "video_path": str(resolved),
        "title": resolved.stem,
        "width": probe.width,
        "height": probe.height,
        "duration_seconds": probe.duration_seconds,
        "from_cache": True,
    }
    if not moments:
        return {
            "url": None,
            "transcript_path": str(srt),
            "transcript_available": True,
            "moments": [],
            "source": source,
            "previews": [],
        }
    previews = render_previews(
        resolved, moments, workspace / "previews", count=top_n
    )
    return {
        "url": None,
        "transcript_path": str(srt),
        "transcript_available": True,
        "moments": [moment.__dict__ for moment in moments],
        "source": source,
        "previews": previews,
    }


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
                opening_trimmed=moment.opening_trimmed,
                opens_mid_thought=moment.opens_mid_thought,
                ends_mid_thought=moment.ends_mid_thought,
                tail_hold=moment.tail_hold,
            )
        )
    return moments


def render_previews(
    source_path: Path,
    moments: list[SourceMoment],
    output_dir: Path,
    *,
    count: int = DEFAULT_PREVIEW_COUNT,
    height: int = 480,
) -> list[dict]:
    """Cut the top moments out of a local source so they can be watched and judged.

    These are raw cuts, not templated renders: no crop, no title, no styling.
    The question a preview answers is "is this moment worth a clip", which the
    footage alone settles, and skipping the template keeps a batch of eight
    under about ten seconds. The chosen moment then goes through
    :func:`render_clip` for the real output.

    Judging by eye is the point. Ranking cannot tell whether a moment is a
    static talking head or whether the payoff actually lands, and eight
    fifteen-second previews take about two minutes to watch against the ninety
    minutes of watching the source they replace.
    """
    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.exists():
        raise FileNotFoundError(f"Source video not found: {resolved_source}")
    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")
    output_dir.mkdir(parents=True, exist_ok=True)

    previews: list[dict] = []
    for index, moment in enumerate(moments[:count]):
        output_path = output_dir / f"preview_{index:02d}_{int(moment.start)}.mp4"
        # Cut past the out-point on purpose. The extra footage is what
        # _visual_payoff measures against the spoken body, and watching a few
        # seconds beyond the proposed end is how the operator sees whether the
        # thing being talked about is finally shown.
        cut_duration = moment.duration + _PAYOFF_LOOKAHEAD_SECONDS
        command = [
            str(ffmpeg_path),
            "-y",
            "-ss",
            f"{moment.start:.3f}",
            "-t",
            f"{cut_duration:.3f}",
            "-i",
            str(resolved_source),
            "-vf",
            f"scale=-2:{height}",
            "-c:v",
            "libx264",
            "-crf",
            "28",  # preview quality only; the real render re-cuts from the source
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(
            command, check=True, capture_output=True, text=True, **subprocess_run_kwargs()
        )
        # One ffprobe for both measurements — it reads packet headers, not
        # pixels, so the cost is a few milliseconds either way.
        packets = _packet_activity(output_path)
        previews.append(
            {
                "index": index,
                "video_path": str(output_path),
                "start": moment.start,
                "end": moment.end,
                "duration": moment.duration,
                "score": moment.score,
                "range_label": moment.range_label,
                "reasons": list(moment.reasons),
                "visual_activity": _visual_activity(output_path, moment.duration, packets),
                "visual_payoff": _visual_payoff(output_path, moment.duration, packets),
                # The preview plays this much past `end`, so the player's clock
                # and the proposed out-point do not line up. Stated rather than
                # inferred: the UI marks where the clip would actually stop.
                "lookahead_seconds": _PAYOFF_LOOKAHEAD_SECONDS,
                "opening_trimmed": moment.opening_trimmed,
                "opens_mid_thought": moment.opens_mid_thought,
                "ends_mid_thought": moment.ends_mid_thought,
                "tail_hold": moment.tail_hold,
                # The exact words spoken in this window: the grounding the title
                # and caption prompt should be built from.
                "context": moment.context,
            }
        )
    return previews


# Below this, a preview is almost certainly one locked-off shot. Calibrated on a
# documentary where a verified talking head measured 23 KB/s and a segment
# showing the artifact being discussed measured 67 KB/s — both whole-file rates,
# which included the 128 kbps audio track (16 KB/s) and container overhead.
#
# The measurement is now video packets only, so those calibration points become
# 7 and 51 KB/s and the threshold moves with them. Confirmed against the source
# they were taken from: the talking head that measured 23 whole-file measures
# 7.4 video-only. Leaving this at 30 would have marked every clip static.
_STATIC_FOOTAGE_KB_PER_SECOND = 14.0


def _visual_activity(
    preview_path: Path, duration: float, packets: list[tuple[float, int]] | None = None
) -> dict:
    """How much the picture actually changes, for free, from the encoded size.

    A constant-quality encoder spends bits where the image moves, so at a fixed
    CRF the bytes-per-second of a preview is a direct measure of visual change.
    A locked-off interview shot compresses to almost nothing; a segment that cuts
    between hands, objects and faces does not.

    This matters because the ranker reads only the transcript and cannot tell
    that a moment with perfect audio is fifteen seconds of a man in a chair.
    Reported rather than folded into the score: the operator picks, and "audio
    is great but there is nothing to look at" is a judgement call, not a reject.

    ``packets`` restricts the measurement to the clip's own span. The preview
    file is cut with look-ahead footage past the out-point, so dividing the whole
    file's size by ``duration`` would count seconds the clip does not contain and
    read every moment as busier than it is — the threshold below is calibrated on
    the clip alone.
    """
    if duration <= 0:
        return {"kb_per_second": 0.0, "looks_static": True}
    if packets:
        kb_per_second = _span_kb_per_second(packets, 0.0, duration)
    else:
        kb_per_second = preview_path.stat().st_size / 1024 / duration
    return {
        "kb_per_second": round(kb_per_second, 1),
        "looks_static": kb_per_second < _STATIC_FOOTAGE_KB_PER_SECOND,
    }


# How far past a moment's out-point to look for a visual payoff, and how much
# busier the picture has to get before it counts as one. The ratio is deliberately
# well above 1.0: ordinary shot-to-shot variation moves this by a few percent, and
# a cutaway to the object being discussed measured 2-3x the talking-head baseline.
_PAYOFF_LOOKAHEAD_SECONDS = 4.0
_PAYOFF_ACTIVITY_RATIO = 1.6
# Below this the tail is a cut to black, a fade, or the credits — busier than the
# body in relative terms only because the body was nearly still.
_PAYOFF_MIN_TAIL_KB_PER_SECOND = 25.0


def _packet_activity(video_path: Path) -> list[tuple[float, int]]:
    """``(timestamp, byte size)`` per video frame, from packet headers alone.

    No decoding: ffprobe reads the container index, so this is milliseconds even
    on a long file. At the fixed CRF the previews are encoded with, a frame's
    byte size is a direct measure of how much the picture changed to reach it,
    which is what lets :func:`_visual_payoff` compare two spans of one file.
    """
    ffprobe_path = ffprobe_binary()
    if ffprobe_path is None:
        return []
    command = [
        str(ffprobe_path), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,size",
        "-of", "csv=p=0",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, **subprocess_run_kwargs()
        )
    except subprocess.CalledProcessError:
        return []
    packets: list[tuple[float, int]] = []
    # Activity is advisory, so anything unreadable degrades to "no measurement"
    # rather than failing a preview batch the operator is waiting on.
    for line in (getattr(result, "stdout", "") or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            packets.append((float(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return packets


def _span_kb_per_second(
    packets: list[tuple[float, int]], start: float, end: float
) -> float:
    span = end - start
    if span <= 0:
        return 0.0
    total = sum(size for timestamp, size in packets if start <= timestamp < end)
    return total / 1024 / span


def _visual_payoff(
    preview_path: Path,
    spoken_duration: float,
    packets: list[tuple[float, int]] | None = None,
) -> dict:
    """Whether the picture gets busier *after* the talking stops.

    The case this exists for: a clip lands "this card is $400,000", the words
    end, and a beat later the camera finally cuts to the card. The ranker reads
    only the transcript, so it ends the clip on the last syllable and throws the
    reveal away — the words arrive without the thing they are about.

    The preview is cut with :data:`_PAYOFF_LOOKAHEAD_SECONDS` of extra footage
    past the out-point precisely so this can be measured. Comparing the tail's
    bytes-per-second against the spoken body's finds the cutaway without
    decoding a single frame.

    Advisory, like every other flag here: it reports where to look and offers an
    out-point, and the operator decides. A busier tail can equally be the next
    scene starting.
    """
    packets = packets if packets is not None else _packet_activity(preview_path)
    if not packets or spoken_duration <= 0:
        return {"detected": False}
    total_duration = max(timestamp for timestamp, _ in packets)
    if total_duration <= spoken_duration + 0.2:
        return {"detected": False}

    body = _span_kb_per_second(packets, 0.0, spoken_duration)
    tail = _span_kb_per_second(packets, spoken_duration, total_duration)
    detected = (
        tail >= _PAYOFF_MIN_TAIL_KB_PER_SECOND
        and body > 0
        and tail / body >= _PAYOFF_ACTIVITY_RATIO
    )
    return {
        "detected": detected,
        "body_kb_per_second": round(body, 1),
        "tail_kb_per_second": round(tail, 1),
        # How much further the out-point should run to include the reveal. The
        # whole look-ahead, not a guess at where the shot ends: the measurement
        # says the tail is busy, not where it stops being busy.
        "extra_seconds": round(total_duration - spoken_duration, 1),
    }


def plan_url(
    url: str,
    output_dir: Path,
    *,
    celebrity_names: tuple[str, ...] = (),
    top_n: int = 10,
) -> dict:
    """Rank a source's moments WITHOUT downloading the video.

    This is :func:`analyze_url` minus the expensive half. Only the caption track
    is fetched (a few hundred KB, ~4s on a 70-minute source), so the review
    screen can show a ranked list almost immediately; the footage for the chosen
    moment is then pulled with :func:`download_source_section`.

    Prefer this over :func:`analyze_url` in the UI — ``analyze_url`` downloads
    the entire source before ranking, which is minutes of waiting to produce a
    twenty-second clip.
    """
    srt = fetch_transcript(url, output_dir)
    moments = rank_moments(srt, celebrity_names=celebrity_names, top_n=top_n) if srt else []
    metadata = source_metadata(url)
    language = metadata.get("language")
    return {
        "url": url,
        "transcript_path": str(srt) if srt else None,
        "transcript_available": srt is not None,
        "moments": [moment.__dict__ for moment in moments],
        # Carried on the plan rather than read off the download: ensure_local_source
        # reports no title on a cache hit, which is every run after the first, so
        # the source's own subject was being thrown away exactly when it was cheap.
        "title": metadata.get("title"),
        "source_language": language,
        # A viewer who cannot follow the audio needs the words on screen, so the
        # UI defaults the burn-in toggle from this rather than making the
        # operator notice the source is in another language.
        "captions_recommended": bool(language) and not language.lower().startswith("en"),
    }


def source_metadata(url: str) -> dict:
    """What YouTube reports about ``url`` — ``{"title", "language"}``.

    Metadata only, no download, so it is cheap enough to call alongside the
    caption fetch. The title matters as much as the language: it is the only
    place the *subject* of a long source is written down, and a fifteen-second
    window rarely names it. Without it a title generator can only write "this
    card" where the source says "the PSA 10 Pikachu Illustrator card".
    """
    from yt_dlp import YoutubeDL

    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001 - advisory; never fail the plan for it
        return {"title": None, "language": None}
    return {"title": info.get("title") or None, "language": info.get("language") or None}


def source_language(url: str) -> str | None:
    """The language YouTube reports for ``url``, e.g. ``"ko"``; ``None`` if unknown."""
    return source_metadata(url).get("language")


def plan_and_preview(
    url: str,
    workspace: Path,
    *,
    celebrity_names: tuple[str, ...] = (),
    top_n: int = DEFAULT_PREVIEW_COUNT,
) -> dict:
    """The review workflow end to end: rank, fetch once, cut a batch to watch.

    Replaces "read the ranking, then fetch one span at a time". The source is
    downloaded once and cached, so cutting eight previews costs about ten
    seconds of ffmpeg rather than eight separate network fetches.

    A source with captions disabled is not a dead end: the video is fetched and
    transcribed locally instead (minutes rather than seconds, hence only as a
    fallback). ``moments`` stays empty only when there is genuinely no speech —
    a wordless trailer — and the caller should say so rather than showing an
    empty list, because "nothing said" and "nothing worth clipping" are very
    different problems.
    """
    plan = plan_url(url, workspace, celebrity_names=celebrity_names, top_n=top_n)
    if plan["transcript_available"]:
        source = ensure_local_source(url, workspace)
    else:
        # No captions to fetch, so the audio has to be read directly. The
        # download has to happen first either way.
        source = ensure_local_source(url, workspace)
        try:
            srt = transcribe_source(
                Path(source["video_path"]), workspace, vocabulary=celebrity_names
            )
        except RuntimeError as error:
            return {**plan, "previews": [], "source": source, "transcript_error": str(error)}
        plan = {
            **plan,
            "transcript_path": str(srt),
            "transcript_available": True,
            "transcribed_locally": True,
        }

    moments = rank_moments(
        Path(plan["transcript_path"]), celebrity_names=celebrity_names, top_n=top_n
    )
    if not moments:
        return {**plan, "previews": [], "source": source}

    previews = render_previews(
        Path(source["video_path"]), moments, workspace / "previews", count=top_n
    )
    return {**plan, "source": source, "previews": previews}


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
