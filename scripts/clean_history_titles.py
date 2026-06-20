"""Deterministic, low-risk cleanup of locally-OCR'd on-screen titles.

Local OCR (RapidOCR) reads this account's font well structurally but leaves two
systematic artifacts: stray single letters duplicated at word edges
("f filmed", "Fire e Extinguisher") and missing punctuation spacing
("life.I fear", "1892&lived"). This pass fixes those safely without touching
word content. It never attempts to re-split merged words (that needs a
dictionary and risks breaking proper nouns) -- those rows stay flagged via
``needs_review`` for an optional Groq refine later.

The original transcription is preserved in ``*_raw`` fields and a
``titles.raw.json`` backup, so the step is fully reversible.

Usage:
    .venv\\Scripts\\python scripts\\clean_history_titles.py data\\title_analysis\\historytrails-ocr
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from extract_history_titles import _needs_review, _write_outputs  # noqa: E402

_SENTENCE_PUNCT = re.compile(r"([.,!?;:])(?=[A-Za-z(])")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,!?;:])")
_MULTISPACE = re.compile(r"\s{2,}")


def _is_stray_single(token: str, prev_word: str, next_word: str) -> bool:
    """A lone single letter that is an OCR artifact, not a real word."""
    if len(token) != 1 or not token.isalpha():
        return False
    if token in ("a", "A", "I", "i"):  # keep legitimate single-letter words
        return False
    if token.islower():  # lone lowercase b-h/j-z is essentially always stray in prose
        return True
    # Lone uppercase (not A/I): drop only when it duplicates an adjacent word's edge.
    edge = token.lower()
    return next_word[:1].lower() == edge or prev_word[:1].lower() == edge


def _strip_stray_letters(text: str) -> str:
    tokens = text.split()
    kept: list[str] = []
    for index, token in enumerate(tokens):
        prev_word = tokens[index - 1] if index > 0 else ""
        next_word = tokens[index + 1] if index + 1 < len(tokens) else ""
        if _is_stray_single(token, prev_word, next_word):
            continue
        kept.append(token)
    return " ".join(kept)


def _fix_spacing(text: str) -> str:
    text = text.replace("&", " & ")
    text = _SENTENCE_PUNCT.sub(r"\1 ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = re.sub(r",\s*,", ",", text)  # collapse doubled commas (", ,but")
    text = _MULTISPACE.sub(" ", text)
    return text.strip()


def _clean_line(text: str) -> str:
    return _fix_spacing(_strip_stray_letters(text))


def clean_row(row: dict[str, Any]) -> bool:
    """Clean one row in place. Returns True if the title text changed."""
    raw_title = str(row.get("on_screen_title") or "")
    raw_lines = list(row.get("on_screen_title_lines") or [])
    if not raw_title.strip():
        return False

    cleaned_lines = [c for c in (_clean_line(line) for line in raw_lines) if c]
    cleaned_title = _clean_line(" ".join(raw_lines)) if raw_lines else _clean_line(raw_title)

    if cleaned_title == raw_title and cleaned_lines == raw_lines:
        return False

    row["on_screen_title_raw"] = raw_title
    row["on_screen_title_lines_raw"] = raw_lines
    row["on_screen_title"] = cleaned_title
    row["on_screen_title_lines"] = cleaned_lines or ([cleaned_title] if cleaned_title else [])
    row["title_line_count"] = len(row["on_screen_title_lines"])
    row["needs_review"] = _needs_review(cleaned_title)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean locally-OCR'd title text.")
    parser.add_argument("path", type=Path, help="Extraction output directory.")
    args = parser.parse_args()

    out_dir = args.path
    json_path = out_dir / "titles.json"
    rows = json.loads(json_path.read_text(encoding="utf-8"))

    backup = out_dir / "titles.raw.json"
    if not backup.exists():
        shutil.copy2(json_path, backup)
        print(f"Backed up original -> {backup}")

    changed = sum(1 for row in rows if clean_row(row))
    flagged = sum(1 for row in rows if row.get("needs_review"))
    _write_outputs(out_dir, rows)

    print(f"Rows: {len(rows)}")
    print(f"Rows cleaned: {changed}")
    print(f"Rows still flagged (needs_review, mostly residual merges): {flagged}")
    print(f"Wrote: {json_path} and titles.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
