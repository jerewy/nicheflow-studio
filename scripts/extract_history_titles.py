"""Extract on-screen title text from scraped Instagram thumbnails.

The script enriches existing ``scrape_candidates`` rows into an analysis-ready
sidecar dataset. It does not mutate the app database.

Usage:
    .venv\\Scripts\\python scripts\\extract_history_titles.py --handle historytrails --limit 10
    .venv\\Scripts\\python scripts\\extract_history_titles.py --handle historytrails --resume data\\title_analysis\\historytrails-latest
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import io
import itertools
import json
import mimetypes
import sys
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from nicheflow_studio.core.env import load_dotenv  # noqa: E402
from nicheflow_studio.db.models import ScrapeCandidate  # noqa: E402
from nicheflow_studio.db.session import get_session  # noqa: E402
from nicheflow_studio.processing.smart_drafts import (  # noqa: E402
    DEFAULT_GROQ_VISION_MODEL,
    GROQ_CHAT_COMPLETIONS_URL,
    _all_groq_keys,
    _extract_message_content,
    _normalize_whitespace,
    _parse_model_json,
    _perform_chat_completion_request,
)


OUTPUT_FIELDS = [
    "candidate_id",
    "video_id",
    "source_url",
    "channel_name",
    "published_at",
    "view_count",
    "like_count",
    "comment_count",
    "engagement_rate",
    "caption",
    "caption_first_line",
    "thumbnail_url",
    "on_screen_title",
    "on_screen_title_lines",
    "title_line_count",
    "all_visible_text",
    "title_position",
    "alignment",
    "font_style_notes",
    "text_color",
    "background_style",
    "confidence",
    "needs_review",
    "notes",
    "extracted_at",
    "provider_label",
    "extraction_version",
]

EXTRACTION_VERSION = 2


def _candidate_rows(handle: str, *, limit: int | None) -> list[dict[str, Any]]:
    handle_lower = handle.casefold()
    with get_session() as session:
        query = (
            session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.thumbnail_url.is_not(None))
            .filter(ScrapeCandidate.thumbnail_url != "")
            .filter(
                ScrapeCandidate.channel_name.ilike(handle_lower)
                | ScrapeCandidate.source_url.ilike(f"%{handle_lower}%")
                | ScrapeCandidate.scrape_source_url.ilike(f"%{handle_lower}%")
                | ScrapeCandidate.match_reason.ilike(f"%{handle_lower}%")
            )
            .order_by(
                ScrapeCandidate.published_at.desc().nullslast(),
                ScrapeCandidate.id.desc(),
            )
        )
        if limit is not None:
            query = query.limit(limit)
        rows = []
        for row in query.all():
            caption = row.description or ""
            likes = row.like_count or 0
            comments = row.comment_count or 0
            views = row.view_count or 0
            rows.append(
                {
                    "candidate_id": row.id,
                    "video_id": row.video_id,
                    "source_url": row.source_url,
                    "channel_name": row.channel_name,
                    "published_at": row.published_at.isoformat() if row.published_at else "",
                    "view_count": row.view_count,
                    "like_count": row.like_count,
                    "comment_count": row.comment_count,
                    "engagement_rate": round((likes + comments) / views, 6) if views else "",
                    "caption": caption,
                    "caption_first_line": caption.splitlines()[0] if caption else "",
                    "thumbnail_url": row.thumbnail_url,
                }
            )
        return rows


def _image_data_url(payload: bytes, content_type: str) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


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


def _split_title_by_widths(title: str, pixel_widths: list[int]) -> list[str]:
    words = title.split()
    line_count = len(pixel_widths)
    if line_count <= 1 or len(words) < line_count:
        return [title] if title else []

    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except OSError:
        font = ImageFont.load_default()

    best_score: float | None = None
    best_lines: list[str] = [title]
    for breaks in itertools.combinations(range(1, len(words)), line_count - 1):
        boundaries = (0, *breaks, len(words))
        lines = [
            " ".join(words[boundaries[index] : boundaries[index + 1]])
            for index in range(line_count)
        ]
        text_widths = [font.getlength(line) for line in lines]
        denominator = sum(width * width for width in text_widths)
        if not denominator:
            continue
        scale = sum(
            text_width * pixel_width
            for text_width, pixel_width in zip(text_widths, pixel_widths, strict=True)
        ) / denominator
        score = sum(
            ((scale * text_width - pixel_width) / max(pixel_width, 1)) ** 2
            for text_width, pixel_width in zip(text_widths, pixel_widths, strict=True)
        )
        if best_score is None or score < best_score:
            best_score = score
            best_lines = lines
    return best_lines


def _thumbnail_title_image(url: str) -> tuple[str, list[int]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NicheFlowStudio/1.0",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        content_type = response.headers.get_content_type()
    if not content_type or content_type == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(url.split("?", 1)[0])
        content_type = guessed or "image/jpeg"
    with Image.open(io.BytesIO(payload)) as source_image:
        image = source_image.convert("RGB")
        title_line_widths = [width for _start, _end, width in _bright_text_runs(image)]
        title_band = image.crop((0, 0, image.width, max(1, round(image.height * 0.42))))
        output = io.BytesIO()
        title_band.save(output, format="JPEG", quality=92)
    return _image_data_url(output.getvalue(), "image/jpeg"), title_line_widths


_THUMBNAIL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NicheFlowStudio/1.0",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

_LOCAL_OCR_ENGINE: Any = None


def _local_ocr_engine() -> Any:
    """Lazily build the RapidOCR engine (PP-OCRv4 ONNX, CPU, no quota)."""
    global _LOCAL_OCR_ENGINE
    if _LOCAL_OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _LOCAL_OCR_ENGINE = RapidOCR()
    return _LOCAL_OCR_ENGINE


def _download_thumbnail_image(url: str) -> Image.Image:
    request = urllib.request.Request(url, headers=_THUMBNAIL_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    with Image.open(io.BytesIO(payload)) as source_image:
        return source_image.convert("RGB")


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


def _score_to_confidence(mean_score: float, *, has_text: bool) -> str:
    if not has_text:
        return "none"
    if mean_score >= 0.85:
        return "high"
    if mean_score >= 0.60:
        return "medium"
    return "low"


# PP-OCRv4 recognition drops inter-word spaces on this account's tightly-kerned
# title font. A modest fixed upscale restores them; past ~1.6x the recognizer
# starts inventing spurious splits/characters, so 1.4x is the sweet spot
# (validated against candidate 4152 and the wider sample).
_OCR_SCALE = 1.4


def _upscale_for_ocr(crop: Image.Image) -> Image.Image:
    if crop.height <= 1 or _OCR_SCALE <= 1.01:
        return crop
    return crop.resize(
        (round(crop.width * _OCR_SCALE), round(crop.height * _OCR_SCALE)), Image.LANCZOS
    )


def _needs_review(title: str) -> str:
    """Advisory flag for rows likely to carry OCR merge/blank errors."""
    tokens = title.split()
    if not tokens:
        return "empty"
    longest = max(len(token) for token in tokens)
    mean = sum(len(token) for token in tokens) / len(tokens)
    if longest >= 15 or mean > 9:
        return "merge"
    return ""


def _run_local_ocr(crop: Image.Image) -> list[Any]:
    result, _elapsed = _local_ocr_engine()(np.asarray(crop))
    return result or []


def _extract_title_local(row: dict[str, Any]) -> dict[str, Any]:
    image = _download_thumbnail_image(str(row["thumbnail_url"]))
    runs = _bright_text_runs(image)
    crop = _upscale_for_ocr(_title_band_crop(image, runs))
    boxes = _run_local_ocr(crop)
    if not boxes and runs:
        # Dark / low-contrast title can defeat the bright-run crop; retry broader.
        broad = image.crop((0, round(image.height * 0.06), image.width, round(image.height * 0.50)))
        crop = _upscale_for_ocr(broad)
        boxes = _run_local_ocr(crop)

    lines, all_text, mean_score, alignment = _assemble_lines(boxes, crop.width)
    title = " ".join(lines)
    return {
        "on_screen_title": title,
        "on_screen_title_lines": lines,
        "title_line_count": len(lines),
        "all_visible_text": all_text,
        "title_position": "top" if title else "none",
        "alignment": alignment,
        "font_style_notes": "",
        "text_color": "",
        "background_style": "",
        "confidence": _score_to_confidence(mean_score, has_text=bool(title)),
        "needs_review": _needs_review(title),
        "notes": f"local ocr (mean_score={mean_score:.2f})" if title else "local ocr: no title text",
        "extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "provider_label": "RapidOCR PP-OCRv4",
        "extraction_version": EXTRACTION_VERSION,
    }


def _build_title_payload(*, model: str, row: dict[str, Any], image_url: str) -> dict[str, Any]:
    prompt = (
        "Read this Instagram Reel thumbnail and return only valid JSON. "
        "The main task is to transcribe the largest burned-in on-screen title or hook text exactly. "
        "For this dataset, a title/hook can be a short phrase or a full sentence block at the top of the image. "
        "If readable top text appears over or above the footage, put that full visible text in on_screen_title. "
        "Preserve its physical visual rows in on_screen_title_lines: one array item per horizontal "
        "line as rendered in the image, in top-to-bottom reading order. This is visual layout data, "
        "not sentence structure. Never merge wrapped rows into one array item. For example, if the "
        "image displays 'They never told you' on one row and 'the royal mistake' on the next row, "
        "return [\"They never told you\", \"the royal mistake\"]. "
        "Return an empty on_screen_title only when no readable title/hook text is visible in the image. "
        "Do not copy the Instagram caption metadata below unless that same text is visibly present in the image. "
        "Also capture coarse visual style fields for later analysis. "
        "Use this exact schema: "
        '{"on_screen_title":"","on_screen_title_lines":[],"title_line_count":0,"all_visible_text":[],"title_position":"top|middle|bottom|mixed|none",'
        '"alignment":"left|center|right|mixed|unknown","font_style_notes":"","text_color":"",'
        '"background_style":"","confidence":"high|medium|low|none","notes":""}.\n'
        f"Known source account: {row.get('channel_name') or '(unknown)'}\n"
        f"Known caption metadata, for context only: {row.get('caption_first_line') or '(none)'}"
    )
    return {
        "model": model,
        "temperature": 0,
        "max_completion_tokens": 350,
        "top_p": 1,
        "stream": False,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise OCR and visual-style extraction model. "
                    "Return JSON only and leave fields empty rather than guessing."
                ),
            },
            {
                "role": "user",
                    "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
    }


def _normalize_choice(value: object, *, allowed: set[str], default: str) -> str:
    cleaned = _normalize_whitespace(str(value or "")).casefold()
    return cleaned if cleaned in allowed else default


def _clean_text_list(value: object) -> list[str]:
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        values = value.splitlines()
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    cleaned = []
    for item in values:
        text = _normalize_whitespace(str(item or ""))
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _parse_title_response(response_payload: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_model_json(_extract_message_content(response_payload))
    title = _normalize_whitespace(str(parsed.get("on_screen_title") or ""))
    title_lines = _clean_text_list(parsed.get("on_screen_title_lines"))
    if title and not title_lines:
        title_lines = [title]
    if not title and title_lines:
        title = " ".join(title_lines)
    return {
        "on_screen_title": title,
        "on_screen_title_lines": title_lines,
        "title_line_count": len(title_lines),
        "all_visible_text": _clean_text_list(parsed.get("all_visible_text")),
        "title_position": _normalize_choice(
            parsed.get("title_position"),
            allowed={"top", "middle", "bottom", "mixed", "none"},
            default="unknown",
        ),
        "alignment": _normalize_choice(
            parsed.get("alignment"),
            allowed={"left", "center", "right", "mixed", "unknown"},
            default="unknown",
        ),
        "font_style_notes": _normalize_whitespace(str(parsed.get("font_style_notes") or "")),
        "text_color": _normalize_whitespace(str(parsed.get("text_color") or "")),
        "background_style": _normalize_whitespace(str(parsed.get("background_style") or "")),
        "confidence": _normalize_choice(
            parsed.get("confidence"),
            allowed={"high", "medium", "low", "none"},
            default="none",
        ),
        "notes": _normalize_whitespace(str(parsed.get("notes") or "")),
    }


def _extract_title(row: dict[str, Any], *, model: str, api_key: str) -> dict[str, Any]:
    image_url, title_line_widths = _thumbnail_title_image(str(row["thumbnail_url"]))
    response = _perform_chat_completion_request(
        endpoint=GROQ_CHAT_COMPLETIONS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "NicheFlowStudio/1.0",
        },
        payload=_build_title_payload(model=model, row=row, image_url=image_url),
        provider_name="Groq title vision",
    )
    extracted = _parse_title_response(response)
    if extracted["on_screen_title"] and title_line_widths:
        title_lines = _split_title_by_widths(
            str(extracted["on_screen_title"]),
            title_line_widths,
        )
        extracted["on_screen_title_lines"] = title_lines
        extracted["title_line_count"] = len(title_lines)
    extracted["extracted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    extracted["provider_label"] = f"Groq {model}"
    extracted["extraction_version"] = EXTRACTION_VERSION
    return extracted


def _output_paths(handle: str, resume: Path | None) -> Path:
    if resume is not None:
        return resume
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return ROOT / "data" / "title_analysis" / f"{handle}-{stamp}"


def _read_existing(path: Path) -> dict[int, dict[str, Any]]:
    json_path = path / "titles.json"
    if not json_path.exists():
        return {}
    rows = json.loads(json_path.read_text(encoding="utf-8"))
    return {int(row["candidate_id"]): row for row in rows if row.get("candidate_id") is not None}


def _write_outputs(path: Path, rows: list[dict[str, Any]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "titles.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (path / "titles.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["all_visible_text"] = " | ".join(row.get("all_visible_text") or [])
            csv_row["on_screen_title_lines"] = " | ".join(
                row.get("on_screen_title_lines") or []
            )
            writer.writerow({field: csv_row.get(field, "") for field in OUTPUT_FIELDS})


def _is_flagged(row: dict[str, Any]) -> bool:
    return bool(row.get("needs_review")) or not str(row.get("on_screen_title") or "").strip()


def _refine_flagged(
    out_dir: Path,
    existing: dict[int, dict[str, Any]],
    args: argparse.Namespace,
    api_keys: list[str],
) -> int:
    """Re-extract only the flagged rows in place, leaving clean rows untouched."""
    rows_by_id = dict(existing)
    ordered_ids = list(existing.keys())
    targets = [cid for cid in ordered_ids if _is_flagged(existing[cid])]
    print(f"Refining {len(targets)}/{len(existing)} flagged rows with engine={args.engine}")

    stopped_for_rate_limit = False
    for index, candidate_id in enumerate(targets, start=1):
        row = rows_by_id[candidate_id]
        if not str(row.get("thumbnail_url") or "").strip():
            continue
        print(f"[refine {index}/{len(targets)}] candidate {candidate_id}", flush=True)
        try:
            if args.engine == "groq":
                extracted = _extract_title(row, model=args.model, api_key=api_keys[0])
                extracted["needs_review"] = _needs_review(extracted["on_screen_title"])
            else:
                extracted = _extract_title_local(row)
        except Exception as exc:
            if args.engine == "groq" and ("429" in str(exc) or "Rate limit reached" in str(exc)):
                print(f"Stopped for Groq rate limit at candidate {candidate_id}: {exc}")
                stopped_for_rate_limit = True
                break
            print(f"  error on {candidate_id}: {exc}")
            continue
        rows_by_id[candidate_id] = {**row, **extracted}
        _write_outputs(out_dir, [rows_by_id[cid] for cid in ordered_ids])

    final_rows = [rows_by_id[cid] for cid in ordered_ids]
    _write_outputs(out_dir, final_rows)
    remaining = sum(1 for cid in ordered_ids if _is_flagged(rows_by_id[cid]))
    print(f"Output: {out_dir}")
    print(f"Rows: {len(final_rows)} | still flagged: {remaining}")
    return 3 if stopped_for_rate_limit else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract on-screen titles from scraped thumbnails.")
    parser.add_argument("--handle", default="historytrails", help="Instagram source handle.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to process.")
    parser.add_argument("--resume", type=Path, default=None, help="Existing output directory to resume.")
    parser.add_argument(
        "--engine",
        choices=["local", "groq"],
        default="local",
        help="Transcription engine: 'local' RapidOCR (no quota) or 'groq' vision.",
    )
    parser.add_argument("--model", default=DEFAULT_GROQ_VISION_MODEL, help="Groq vision model.")
    parser.add_argument("--dry-run", action="store_true", help="Write metadata rows without calling Groq.")
    parser.add_argument(
        "--refine",
        action="store_true",
        help="Re-extract only existing flagged rows (needs_review/empty) with --engine, in place.",
    )
    args = parser.parse_args()

    load_dotenv()
    out_dir = _output_paths(args.handle, args.resume)
    existing = _read_existing(out_dir)
    candidates = _candidate_rows(args.handle, limit=args.limit)
    api_keys = _all_groq_keys()
    if not args.dry_run and args.engine == "groq" and not api_keys:
        print("GROQ_API_KEY is not configured in .env.")
        return 2

    if args.refine:
        return _refine_flagged(out_dir, existing, args, api_keys)

    rows_by_id = dict(existing)
    stopped_for_rate_limit = False
    for index, candidate in enumerate(candidates, start=1):
        candidate_id = int(candidate["candidate_id"])
        if (
            candidate_id in existing
            and existing[candidate_id].get("extracted_at")
            and int(existing[candidate_id].get("extraction_version") or 0)
            >= EXTRACTION_VERSION
        ):
            continue
        print(f"[{index}/{len(candidates)}] candidate {candidate_id}", flush=True)
        if args.dry_run:
            extracted = {
                "on_screen_title": "",
                "on_screen_title_lines": [],
                "title_line_count": 0,
                "all_visible_text": [],
                "title_position": "",
                "alignment": "",
                "font_style_notes": "",
                "text_color": "",
                "background_style": "",
                "confidence": "",
                "notes": "dry run",
                "extracted_at": "",
                "provider_label": "",
                "extraction_version": EXTRACTION_VERSION,
            }
        elif args.engine == "local":
            try:
                extracted = _extract_title_local(candidate)
            except Exception as exc:
                extracted = {
                    "on_screen_title": "",
                    "on_screen_title_lines": [],
                    "title_line_count": 0,
                    "all_visible_text": [],
                    "title_position": "",
                    "alignment": "",
                    "font_style_notes": "",
                    "text_color": "",
                    "background_style": "",
                    "confidence": "none",
                    "notes": f"error: {exc}",
                    "extracted_at": "",
                    "provider_label": "RapidOCR PP-OCRv4",
                    "extraction_version": EXTRACTION_VERSION,
                }
        else:
            try:
                extracted = _extract_title(candidate, model=args.model, api_key=api_keys[0])
            except Exception as exc:
                if "429" in str(exc) or "Rate limit reached" in str(exc):
                    print(f"Stopped for Groq rate limit at candidate {candidate_id}: {exc}")
                    stopped_for_rate_limit = True
                    break
                extracted = {
                    "on_screen_title": "",
                    "on_screen_title_lines": [],
                    "title_line_count": 0,
                    "all_visible_text": [],
                    "title_position": "",
                    "alignment": "",
                    "font_style_notes": "",
                    "text_color": "",
                    "background_style": "",
                    "confidence": "none",
                    "notes": f"error: {exc}",
                    "extracted_at": "",
                    "provider_label": f"Groq {args.model}",
                    "extraction_version": EXTRACTION_VERSION,
                }
        rows_by_id[candidate_id] = {**candidate, **extracted}
        checkpoint_rows = [
            rows_by_id[int(row["candidate_id"])]
            for row in candidates
            if int(row["candidate_id"]) in rows_by_id
        ]
        _write_outputs(out_dir, checkpoint_rows)

    final_rows = [
        rows_by_id[int(row["candidate_id"])]
        for row in candidates
        if int(row["candidate_id"]) in rows_by_id
    ]
    _write_outputs(out_dir, final_rows)

    extracted_count = sum(1 for row in final_rows if row.get("on_screen_title"))
    print(f"Output: {out_dir}")
    print(f"Rows: {len(final_rows)}")
    print(f"Rows with on-screen title: {extracted_count}")
    return 3 if stopped_for_rate_limit else 0


if __name__ == "__main__":
    raise SystemExit(main())
