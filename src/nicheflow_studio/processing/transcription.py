from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.core.media_tools import ffmpeg_binary, subprocess_run_kwargs


@dataclass(frozen=True)
class TranscriptDraft:
    transcript_text: str
    title_draft: str
    caption_draft: str


def generate_transcript_draft(
    input_path: Path,
    *,
    fallback_title: str | None = None,
    model_size: str = "base",
) -> TranscriptDraft:
    resolved_input = input_path.expanduser().resolve()
    if not resolved_input.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_input}")

    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Add it to the environment to generate transcript drafts."
        ) from exc

    with tempfile.TemporaryDirectory(prefix="nicheflow-transcribe-") as temp_dir:
        audio_path = Path(temp_dir) / "audio.wav"
        _extract_audio_for_transcription(ffmpeg_path, resolved_input, audio_path)
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(audio_path), vad_filter=True)
        transcript_text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())

    if not transcript_text:
        raise RuntimeError("No speech was detected in this video.")

    cleaned_transcript = _normalize_whitespace(transcript_text)
    return TranscriptDraft(
        transcript_text=cleaned_transcript,
        title_draft=_draft_title(cleaned_transcript, fallback_title=fallback_title),
        caption_draft=_draft_caption(cleaned_transcript),
    )


def generate_transcript_draft_in_subprocess(
    input_path: Path,
    *,
    fallback_title: str | None = None,
    model_size: str = "base",
    python_executable: str | None = None,
) -> TranscriptDraft:
    resolved_input = input_path.expanduser().resolve()
    command = [
        python_executable or sys.executable,
        "-m",
        "nicheflow_studio.processing.transcription",
        "--input",
        str(resolved_input),
        "--model-size",
        model_size,
    ]
    if fallback_title:
        command.extend(["--fallback-title", fallback_title])

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )
    if result.returncode != 0:
        error_message = (result.stderr or result.stdout or "").strip()
        if not error_message:
            error_message = f"transcription subprocess exited with code {result.returncode}"
        raise RuntimeError(error_message)

    payload = json.loads(result.stdout)
    return TranscriptDraft(
        transcript_text=payload["transcript_text"],
        title_draft=payload["title_draft"],
        caption_draft=payload["caption_draft"],
    )


def _srt_timestamp(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def transcribe_to_srt(
    input_path: Path,
    output_path: Path,
    *,
    model_size: str = "base",
    language: str | None = "en",
    vocabulary: tuple[str, ...] = (),
) -> Path:
    """Transcribe ``input_path`` to a timestamped SRT at ``output_path``.

    :func:`generate_transcript_draft` runs the same model but joins every
    segment into one string, discarding ``segment.start``/``segment.end``. Those
    timings are what the whole clip pipeline is built on, so this writes an SRT
    instead — the exact format :mod:`transcript_clips` already parses. A local
    file and a captioned YouTube URL then converge on one code path rather than
    two.

    ``language`` is pinned to English by default: campaign rules require English
    output, and letting the model auto-detect on a noisy intro occasionally
    picks the wrong language and returns translated text.

    ``vocabulary`` biases decoding toward names the model would otherwise
    mangle, and it matters more than it looks: the ranker matches celebrity
    names literally, so a mangled name is a signal that silently never fires.
    Measured on a real interview, "Noah Lyles" came out as "Noah Laos" on the
    base model and "Noah Lowes" on small — passing the roster fixed it on base
    with no extra runtime.
    """
    resolved_input = input_path.expanduser().resolve()
    if not resolved_input.exists():
        raise FileNotFoundError(f"Video file not found: {resolved_input}")

    ffmpeg_path = ffmpeg_binary()
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed.")

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Add it to the environment to transcribe local files."
        ) from exc

    with tempfile.TemporaryDirectory(prefix="nicheflow-transcribe-srt-") as temp_dir:
        audio_path = Path(temp_dir) / "audio.wav"
        _extract_audio_for_transcription(ffmpeg_path, resolved_input, audio_path)
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(
            str(audio_path),
            vad_filter=True,
            language=language,
            initial_prompt=", ".join(vocabulary) + "." if vocabulary else None,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with output_path.open("w", encoding="utf-8") as handle:
            for segment in segments:
                text = _normalize_whitespace(segment.text or "")
                if not text:
                    continue
                written += 1
                handle.write(
                    f"{written}\n"
                    f"{_srt_timestamp(segment.start)} --> {_srt_timestamp(segment.end)}\n"
                    f"{text}\n\n"
                )

    if written == 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("No speech was detected in this video.")
    return output_path


def _extract_audio_for_transcription(ffmpeg_path: Path, input_path: Path, output_path: Path) -> None:
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        **subprocess_run_kwargs(),
    )


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


def _draft_title(transcript_text: str, *, fallback_title: str | None) -> str:
    first_sentence = _split_sentences(transcript_text)[0] if _split_sentences(transcript_text) else transcript_text
    words = [word for word in re.split(r"\s+", first_sentence) if word]
    if len(words) > 10:
        candidate = " ".join(words[:10]).rstrip(".,!?") + "..."
    else:
        candidate = first_sentence.rstrip(".,!?")
    if fallback_title and len(candidate) < 12:
        return fallback_title.strip()
    return candidate


def _draft_caption(transcript_text: str) -> str:
    sentences = _split_sentences(transcript_text)
    if not sentences:
        return transcript_text
    joined = " ".join(sentences[:2]).strip()
    if len(joined) <= 220:
        return joined
    trimmed = joined[:217].rstrip()
    return f"{trimmed}..."


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate transcript, title, and caption drafts.")
    parser.add_argument("--input", required=True, help="Path to the source video file.")
    parser.add_argument("--fallback-title", default=None, help="Optional fallback title.")
    parser.add_argument("--model-size", default="base", help="Whisper model size.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        draft = generate_transcript_draft(
            Path(args.input),
            fallback_title=args.fallback_title,
            model_size=args.model_size,
        )
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(asdict(draft), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
