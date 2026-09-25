"""Read the source account's burned-in on-screen hook off a local clip.

``draft_handoff._on_screen_hook_lines`` calls that hook "the single most useful
line in the block", and two shipping rules depend on it: the evidence block's
"match the angle, never the wording", and ``title_lab``'s rule that keeps the
hook's angle while dropping any claim the evidence does not support. Both
sat dead on 161 of 185 reels because nothing populated the field.

The hook is text burned into the video, so reading it is OCR rather than vision:
RapidOCR (PP-OCRv4 ONNX, CPU) does it locally with no key and no quota. The band
detection and line assembly below were written for ``extract_history_titles.py``
and moved here verbatim so the batch path and the research extractor can never
disagree about what a title says - the reference set that script produced is
where the prompt's examples come from.

Lives in the package rather than in ``scripts/`` because the batch path imports
it, and a packaged build has no scripts directory.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from nicheflow_studio.processing.smart_drafts import _normalize_whitespace
from nicheflow_studio.processing.video import ffmpeg_binary

_LOCAL_OCR_ENGINE: Any = None

# Reels routinely open on a black or faded frame, and the banner is static by
# definition, so any later frame carries it just as well.
FRAME_SECONDS = 1.0

# Below this mean OCR confidence the reading is noise, and a wrong hook is worse
# than none: the prompt tells the model to MATCH THE ANGLE of whatever it is
# given, so a garbled line would steer every option on that reel.
MIN_HOOK_SCORE = 0.55

# A hook is a sentence burned across the top of a reel. Anything shorter than
# this is what OCR returns when it catches a stray glyph off the footage - the
# first pass over the library stored 30 hooks of one character ("H", "1", a star
# symbol), several of them above the confidence floor, because a single crisp
# letter reads with high confidence. Same principle as the floor: no hook beats
# a wrong one, since the prompt is told to match the angle of whatever it gets.
MIN_HOOK_CHARS = 10

# Upscale before OCR: PP-OCRv4 reads small overlay text noticeably better
# with a little headroom. Value carried over from the research extractor.
_OCR_SCALE = 1.4


def _bright_text_runs(image: Image.Image) -> list[tuple[int, int, int]]:
    grayscale = image.convert("L")
    width, height = grayscale.size
    x_start = round(width * 0.04)
    x_end = round(width * 0.96)
    minimum_bright_pixels = max(8, round(width * 0.012))
    raw_runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for y in range(round(height * 0.08), round(height * 0.42)):
        bright_pixels = sum(
            1 for x in range(x_start, x_end) if grayscale.getpixel((x, y)) >= 175
        )
        active = bright_pixels >= minimum_bright_pixels
        if active and run_start is None:
            run_start = y
        elif not active and run_start is not None:
            if y - run_start >= 3:
                raw_runs.append((run_start, y - 1))
            run_start = None

    merged: list[tuple[int, int]] = []
    for start, end in raw_runs:
        if merged and start - merged[-1][1] <= 3:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    if len(merged) < 2:
        return []

    section_gap = max(24, round(height * 0.025))
    title_start_index: int | None = None
    for index in range(1, len(merged)):
        if merged[index][0] - merged[index - 1][1] >= section_gap:
            title_start_index = index
            break
    if title_start_index is None:
        return []

    title_runs = [merged[title_start_index]]
    for run in merged[title_start_index + 1 :]:
        if run[0] - title_runs[-1][1] >= section_gap:
            break
        title_runs.append(run)

    measured: list[tuple[int, int, int]] = []
    for start, end in title_runs:
        bright_x = [
            x
            for y in range(start, end + 1)
            for x in range(x_start, x_end)
            if grayscale.getpixel((x, y)) >= 175
        ]
        if bright_x:
            measured.append((start, end, max(bright_x) - min(bright_x) + 1))
    return measured


def _local_ocr_engine() -> Any:
    """Lazily build the RapidOCR engine (PP-OCRv4 ONNX, CPU, no quota)."""
    global _LOCAL_OCR_ENGINE
    if _LOCAL_OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _LOCAL_OCR_ENGINE = RapidOCR()
    return _LOCAL_OCR_ENGINE


def _title_band_crop(image: Image.Image, runs: list[tuple[int, int, int]]) -> Image.Image:
    """Crop the vertical span that holds the title so OCR ignores the footage."""
    width, height = image.size
    if runs:
        pad = max(6, round(height * 0.012))
        y0 = max(0, min(start for start, _end, _w in runs) - pad)
        y1 = min(height, max(end for _start, end, _w in runs) + pad)
    else:
        y0 = round(height * 0.06)
        y1 = round(height * 0.46)
    return image.crop((0, y0, width, y1))


def _assemble_lines(
    boxes: list[Any], crop_width: int
) -> tuple[list[str], list[str], float, str]:
    """Group OCR boxes into physical rows, top-to-bottom then left-to-right."""
    items = []
    for box, text, score in boxes:
        cleaned = _normalize_whitespace(str(text or ""))
        if not cleaned:
            continue
        score = float(score)
        if score < 0.45:
            continue
        ys = [point[1] for point in box]
        xs = [point[0] for point in box]
        items.append(
            {
                "text": cleaned,
                "score": score,
                "cy": (min(ys) + max(ys)) / 2,
                "left": min(xs),
                "center": (min(xs) + max(xs)) / 2,
                "height": max(ys) - min(ys),
            }
        )
    if not items:
        return [], [], 0.0, "unknown"

    items.sort(key=lambda item: item["cy"])
    heights = sorted(item["height"] for item in items)
    line_gap = max(8.0, heights[len(heights) // 2] * 0.6)

    grouped: list[list[dict[str, Any]]] = []
    for item in items:
        if grouped:
            line_center = sum(member["cy"] for member in grouped[-1]) / len(grouped[-1])
            if abs(item["cy"] - line_center) <= line_gap:
                grouped[-1].append(item)
                continue
        grouped.append([item])

    lines: list[str] = []
    all_text: list[str] = []
    centers: list[float] = []
    for line in grouped:
        line.sort(key=lambda member: member["left"])
        lines.append(" ".join(member["text"] for member in line))
        all_text.extend(member["text"] for member in line)
        centers.append(sum(member["center"] for member in line) / len(line))

    mean_score = sum(item["score"] for item in items) / len(items)
    relative = (sum(centers) / len(centers)) / max(crop_width, 1)
    if relative < 0.42:
        alignment = "left"
    elif relative > 0.58:
        alignment = "right"
    else:
        alignment = "center"
    return lines, all_text, mean_score, alignment


def _upscale_for_ocr(crop: Image.Image) -> Image.Image:
    if crop.height <= 1 or _OCR_SCALE <= 1.01:
        return crop
    return crop.resize(
        (round(crop.width * _OCR_SCALE), round(crop.height * _OCR_SCALE)), Image.LANCZOS
    )


def _run_local_ocr(crop: Image.Image) -> list[Any]:
    result, _elapsed = _local_ocr_engine()(np.asarray(crop))
    return result or []


# A source account's brand mark sits in the same upper third as the hook, and the
# band crop is broad enough to catch it (the bright-run detector falls back to a
# fixed 6-46% band whenever the banner is low contrast). It then reads as the
# first or last line of the hook, so "InsideHistory The chilling stare of Ted
# Bundy ..." goes to the prompt as the source's claim. A hook line is a sentence
# fragment and always carries a space; a brand mark rarely does.
_MAX_WATERMARK_CHARS = 22


def _looks_like_watermark(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("@"):
        return True
    if " " in stripped or len(stripped) > _MAX_WATERMARK_CHARS:
        return False
    # OCR drops thin spaces often enough that a real hook fragment can also come
    # back space-less ("backin1968" is the closing line of a live reel), so the
    # spaceless test alone would truncate hooks. A brand mark is letters only;
    # the squashed fragments carry the digits and punctuation of a sentence.
    return stripped.replace(".", "").replace("_", "").isalpha()


# Words a line cannot end on when the sentence is finished. Banners wrap the
# last word onto its own line ("...before Michael Jackson's" / "death"), and that
# lone word passes the brand-mark test, so the line above it decides: a
# possessive or a function word means the next line is still the sentence.
_CONTINUES_ON = frozenset(
    "a an the of to for from in on at by with and or but as his her their its "
    "my your our before after during than was is were are".split()
)


def _sentence_continues(line: str) -> bool:
    words = line.strip().rstrip(",;:").split()
    if not words:
        return False
    last = words[-1].lower()
    return last.endswith(("'s", "’s")) or last in _CONTINUES_ON


def _drop_source_watermark(lines: list[str]) -> list[str]:
    """Trim a brand mark off either end, never out of the middle of a sentence."""
    trimmed = list(lines)
    while len(trimmed) > 1 and _looks_like_watermark(trimmed[0]):
        trimmed.pop(0)
    while (
        len(trimmed) > 1
        and _looks_like_watermark(trimmed[-1])
        and not _sentence_continues(trimmed[-2])
    ):
        trimmed.pop()
    return trimmed


def is_usable_hook(hook: str) -> bool:
    """Whether a reading is long enough to be a hook rather than a stray glyph."""
    return len(hook.strip()) >= MIN_HOOK_CHARS


def frame_image(video_path: Path) -> Image.Image:
    """One frame of a local clip as RGB, for OCR of its burned-in banner."""
    ffmpeg = ffmpeg_binary()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is not installed.")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "frame.jpg"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", str(FRAME_SECONDS), "-i", str(video_path),
                "-frames:v", "1", "-y", str(target),
            ],
            check=True,
            capture_output=True,
        )
        if not target.exists():
            raise RuntimeError(f"ffmpeg produced no frame for {video_path}")
        with Image.open(target) as image:
            return image.convert("RGB")


def read_hook(video_path: Path) -> tuple[str, float]:
    """The burned-in hook and its mean OCR confidence, or ("", 0.0)."""
    image = frame_image(video_path)
    runs = _bright_text_runs(image)
    crop = _upscale_for_ocr(_title_band_crop(image, runs))
    boxes = _run_local_ocr(crop)
    if not boxes and runs:
        # A dark or low-contrast banner defeats the bright-run crop; retry broader.
        broad = image.crop(
            (0, round(image.height * 0.06), image.width, round(image.height * 0.50))
        )
        crop = _upscale_for_ocr(broad)
        boxes = _run_local_ocr(crop)
    lines, _all_text, mean_score, _alignment = _assemble_lines(boxes, crop.width)
    hook = " ".join(_drop_source_watermark(lines)).strip()
    if not is_usable_hook(hook):
        return "", float(mean_score or 0.0)
    return hook, float(mean_score or 0.0)
