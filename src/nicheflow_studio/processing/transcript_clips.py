"""Turn a timestamped transcript into clean, postable clip windows.

This is the core of the Clip Studio "long video -> short clip" pipeline. It is
deliberately source-agnostic: feed it cues from a YouTube auto-subtitle SRT
(via :func:`parse_srt`) or, later, from whisper segments, and it produces
sentence-level segments plus helpers that snap a rough in/out selection to
whole-sentence boundaries so a clip never starts or ends mid-word.

No video or network is required here — it operates purely on transcript text and
timestamps, which is why it can be built and tested without downloading anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.processing.video import MIN_CLIP_SECONDS, SubtitleCue, write_clip_srt

_TIMESTAMP_RE = re.compile(
    r"(\d\d):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d\d):(\d\d):(\d\d)[,.](\d{1,3})"
)
# Bracketed sound/annotation cues like [music], [applause], (laughs).
_ANNOTATION_RE = re.compile(r"[\[(][^\])]*[\])]")
# YouTube auto-caption speaker-change marker (">>").
_SPEAKER_RE = re.compile(r">>+")
# A cue that ends a sentence (optionally followed by a closing quote).
_SENTENCE_END_RE = re.compile(r"[.!?][\"'”’]?$")

DEFAULT_TARGET_SECONDS = 25.0
DEFAULT_MAX_SECONDS = 45.0
# Campaign floor is "over 7 seconds"; keep a margin so a snapped clip is never
# borderline against that rule.
DEFAULT_MIN_SECONDS = max(MIN_CLIP_SECONDS + 1.0, 8.0)


@dataclass(frozen=True)
class TranscriptCue:
    """One raw subtitle cue (as parsed from an SRT), in absolute seconds."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptSentence:
    """A sentence-level segment, the unit clip bounds snap to."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class ClipWindow:
    """A selected clip span with the transcript text it covers."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


def _ts_to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_caption_text(text: str) -> str:
    """Strip sound annotations and speaker markers, collapse whitespace."""
    text = _ANNOTATION_RE.sub(" ", text)
    text = _SPEAKER_RE.sub(" ", text)
    return _normalize_ws(text)


def parse_srt(content: str) -> list[TranscriptCue]:
    """Parse SRT (or VTT-converted-to-SRT) text into ordered cues.

    Tolerant of both ``,``/``.`` millisecond separators and missing index lines.
    Cues with no text or a non-positive span are dropped.
    """
    cues: list[TranscriptCue] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        match = _TIMESTAMP_RE.search(block)
        if not match:
            continue
        start = _ts_to_seconds(*match.group(1, 2, 3, 4))
        end = _ts_to_seconds(*match.group(5, 6, 7, 8))
        text_lines = [
            line.strip()
            for line in block.splitlines()
            if not _TIMESTAMP_RE.search(line) and not line.strip().isdigit()
        ]
        text = _normalize_ws(" ".join(line for line in text_lines if line))
        if end > start and text:
            cues.append(TranscriptCue(start=start, end=end, text=text))
    return cues


def dedup_consecutive_cues(cues: list[TranscriptCue]) -> list[TranscriptCue]:
    """Collapse back-to-back identical cue text (rolling auto-caption artifact).

    The duplicate's end time extends the kept cue so no timing is lost.
    """
    deduped: list[TranscriptCue] = []
    for cue in cues:
        if deduped and cue.text == deduped[-1].text:
            prev = deduped[-1]
            deduped[-1] = TranscriptCue(prev.start, max(prev.end, cue.end), prev.text)
            continue
        deduped.append(cue)
    return deduped


def _sentence_fragments(text: str) -> list[tuple[str, bool]]:
    """Split cleaned cue text into ``(fragment, ends_sentence)`` pieces.

    Splits at every terminal punctuation mark, so a cue that spans a sentence
    boundary ("...phenomenon. It was really...") yields two fragments instead of
    one over-long merged sentence.
    """
    fragments: list[tuple[str, bool]] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in ".!?":
            piece = buffer.strip()
            if piece:
                fragments.append((piece, True))
            buffer = ""
    tail = buffer.strip()
    if tail:
        fragments.append((tail, False))
    return fragments


def build_sentences(
    cues: list[TranscriptCue], *, max_gap_seconds: float = 1.5
) -> list[TranscriptSentence]:
    """Merge cues into sentence-level segments.

    Each cue is split on internal sentence punctuation and its time span
    apportioned across the pieces by character length, so sentence boundaries
    land mid-cue where they actually occur. A sentence also flushes on a speaker
    change (``>>``) or a silent gap wider than ``max_gap_seconds``.
    """
    cues = dedup_consecutive_cues(cues)
    sentences: list[TranscriptSentence] = []
    buffer: list[str] = []
    buffer_start: float | None = None
    buffer_end: float | None = None

    def flush() -> None:
        nonlocal buffer, buffer_start, buffer_end
        if buffer_start is not None and buffer_end is not None and buffer:
            text = _normalize_ws(" ".join(buffer))
            if text:
                sentences.append(TranscriptSentence(buffer_start, buffer_end, text))
        buffer = []
        buffer_start = None
        buffer_end = None

    for cue in cues:
        starts_new_speaker = bool(_SPEAKER_RE.match(cue.text.strip()))
        gap = (cue.start - buffer_end) if buffer_end is not None else 0.0
        if buffer_start is not None and (starts_new_speaker or gap > max_gap_seconds):
            flush()

        text = clean_caption_text(cue.text)
        fragments = _sentence_fragments(text)
        if not fragments:
            continue
        total_chars = sum(len(fragment) for fragment, _ in fragments) or 1
        span = max(cue.end - cue.start, 0.0)
        cursor = 0
        for fragment, ends_sentence in fragments:
            fragment_start = cue.start + (cursor / total_chars) * span
            cursor += len(fragment)
            fragment_end = cue.start + (cursor / total_chars) * span
            if buffer_start is None:
                buffer_start = fragment_start
            buffer.append(fragment)
            buffer_end = fragment_end
            if ends_sentence:
                flush()

    flush()
    return sentences


def sentences_from_srt(content: str, *, max_gap_seconds: float = 1.5) -> list[TranscriptSentence]:
    return build_sentences(parse_srt(content), max_gap_seconds=max_gap_seconds)


def sentences_from_srt_file(path: Path, *, max_gap_seconds: float = 1.5) -> list[TranscriptSentence]:
    content = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    return sentences_from_srt(content, max_gap_seconds=max_gap_seconds)


def snap_to_sentences(
    sentences: list[TranscriptSentence], start: float, end: float
) -> ClipWindow:
    """Expand a rough ``[start, end]`` selection out to whole-sentence bounds.

    ``start`` snaps back to the start of the sentence playing at that moment;
    ``end`` snaps forward to the end of the sentence covering it. The result
    always contains complete sentences, so the cut never clips a word.
    """
    if not sentences:
        raise ValueError("No sentences to snap to.")

    lo = 0
    for index, sentence in enumerate(sentences):
        if sentence.start <= start:
            lo = index
        else:
            break

    hi = lo
    for index in range(lo, len(sentences)):
        hi = index
        if sentences[index].end >= end:
            break

    text = " ".join(sentence.text for sentence in sentences[lo : hi + 1])
    return ClipWindow(sentences[lo].start, sentences[hi].end, text)


def _anchor_index(sentences: list[TranscriptSentence], at_seconds: float) -> int:
    anchor = 0
    for index, sentence in enumerate(sentences):
        if sentence.start <= at_seconds <= sentence.end:
            return index
        if sentence.start <= at_seconds:
            anchor = index
        else:
            break
    return anchor


def clip_window_around(
    sentences: list[TranscriptSentence],
    at_seconds: float,
    *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_join_gap_seconds: float = 2.0,
) -> ClipWindow:
    """Build a clip window anchored on the sentence playing at ``at_seconds``.

    Grows whole sentences outward (forward first, then backward) until it reaches
    ``target_seconds`` or can't grow without exceeding ``max_seconds``. Growth
    stops at a silent gap wider than ``max_join_gap_seconds`` so a scene/topic
    break never pulls dead air into the clip. Used when you click a transcript
    line or a suggested moment.
    """
    if not sentences:
        raise ValueError("No sentences to build a window from.")

    lo = hi = _anchor_index(sentences, at_seconds)

    def duration() -> float:
        return sentences[hi].end - sentences[lo].start

    prefer_forward = True
    while duration() < target_seconds:
        can_forward = (
            hi + 1 < len(sentences)
            and sentences[hi + 1].end - sentences[lo].start <= max_seconds
            and sentences[hi + 1].start - sentences[hi].end <= max_join_gap_seconds
        )
        can_backward = (
            lo - 1 >= 0
            and sentences[hi].end - sentences[lo - 1].start <= max_seconds
            and sentences[lo].start - sentences[lo - 1].end <= max_join_gap_seconds
        )
        if not can_forward and not can_backward:
            break
        if can_forward and (prefer_forward or not can_backward):
            hi += 1
        else:
            lo -= 1
        prefer_forward = not prefer_forward

    text = " ".join(sentence.text for sentence in sentences[lo : hi + 1])
    return ClipWindow(sentences[lo].start, sentences[hi].end, text)


def find_moment(
    sentences: list[TranscriptSentence], needle: str
) -> TranscriptSentence | None:
    """Return the first sentence whose text contains ``needle`` (case-insensitive)."""
    lowered = needle.lower()
    for sentence in sentences:
        if lowered in sentence.text.lower():
            return sentence
    return None


def clip_window_for_text(
    sentences: list[TranscriptSentence],
    needle: str,
    *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_join_gap_seconds: float = 2.0,
) -> ClipWindow | None:
    """Locate a moment by transcript text and build a clip window around it."""
    moment = find_moment(sentences, needle)
    if moment is None:
        return None
    midpoint = (moment.start + moment.end) / 2
    return clip_window_around(
        sentences,
        midpoint,
        target_seconds=target_seconds,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        max_join_gap_seconds=max_join_gap_seconds,
    )


def caption_cues_for_window(
    cues: list[TranscriptCue], window: ClipWindow
) -> list[SubtitleCue]:
    """Build burn-in caption cues (absolute time) for a clip window.

    Uses the short raw cues — not the merged sentences — so captions roll a few
    words at a time, the right cadence for a short clip. Cues are deduped and
    cleaned, filtered to those overlapping the window, and each end is clamped to
    the next cue's start so burn-in captions never stack on top of each other.

    The result is in absolute source time; feed it straight to
    :func:`caption_srt_for_window` / ``write_clip_srt`` with the window bounds.
    """
    kept: list[SubtitleCue] = []
    for cue in dedup_consecutive_cues(cues):
        if cue.end <= window.start or cue.start >= window.end:
            continue
        text = clean_caption_text(cue.text)
        if text:
            kept.append(SubtitleCue(start=cue.start, end=cue.end, text=text))

    kept.sort(key=lambda cue: cue.start)
    clamped: list[SubtitleCue] = []
    for index, cue in enumerate(kept):
        end = cue.end
        if index + 1 < len(kept):
            end = min(end, kept[index + 1].start)
        if end > cue.start:
            clamped.append(SubtitleCue(start=cue.start, end=end, text=cue.text))
    return clamped


def caption_srt_for_window(
    cues: list[TranscriptCue], window: ClipWindow, output_path: Path
) -> Path:
    """Write a 0-based burn-in SRT for ``window`` — ready for ``render_vertical_clip``.

    Bridges the transcript to the renderer: pick the overlapping caption cues,
    then shift them to clip-relative time via ``write_clip_srt`` so they align
    with the seeked clip (whose first frame is t=0).
    """
    subtitle_cues = caption_cues_for_window(cues, window)
    return write_clip_srt(
        subtitle_cues, output_path, clip_start=window.start, clip_end=window.end
    )
