"""Analyze extracted on-screen title patterns."""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import statistics
from pathlib import Path
from typing import Any


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "but",
    "by",
    "for",
    "from",
    "he",
    "her",
    "his",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "she",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "when",
    "with",
    "you",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text.lower())


def _word_count(text: str) -> int:
    return len(_words(text))


def _bucket_word_count(count: int) -> str:
    if count <= 0:
        return "missing"
    if count <= 5:
        return "1-5"
    if count <= 9:
        return "6-9"
    if count <= 14:
        return "10-14"
    return "15+"


def _engagement(row: dict[str, Any]) -> float:
    raw = row.get("engagement_rate")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _views(row: dict[str, Any]) -> int:
    raw = row.get("view_count")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median_views(rows: list[dict[str, Any]]) -> int:
    return int(statistics.median([float(_views(row)) for row in rows])) if rows else 0


def _fine_word_bucket(count: int) -> str:
    if count <= 5:
        return "1-5"
    if count <= 9:
        return "6-9"
    if count <= 14:
        return "10-14"
    if count <= 19:
        return "15-19"
    return "20+"


def _title_format(title: str) -> str:
    """Bucket a title by its opening structure (the reusable template shape)."""
    text = title.strip()
    low = text.lower()
    if low.startswith("that time") or " that time " in low:
        return "That time <person did X>"
    if re.match(r"the (day|time|moment|way) ", low):
        return "The day/time/moment <subject>"
    if low.startswith("during "):
        return "During <event>, <twist>"
    if re.match(r"in (the )?\d", low):
        return "In <year>, <what happened>"
    if low.startswith("when "):
        return "When <person/event>"
    if re.match(r"^\d", text):
        return "<number> <subject> (age/count open)"
    if text.endswith("?") or re.match(r"(how|why|what|can|are|is|do|does) ", low):
        return "Question / How-Why"
    if "of all time" in low or re.search(
        r"\b(greatest|best|most|first|only|rarest|oldest|largest)\b", low
    ):
        return "Superlative / record claim"
    if low.startswith("this ") or low.startswith("these "):
        return "This/These <demonstrative>"
    if re.match(r"the [a-z]", low):
        return "The <noun> declarative"
    return "Other / descriptive sentence"


def _print_format_performance(rows: list[dict[str, Any]]) -> None:
    by_format: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_format[_title_format(str(row.get("on_screen_title") or ""))].append(row)

    print("\nFormat performance (ranked by median views)")
    print(f"  {'format':40} {'n':>4} {'med_views':>11} {'avg_words':>9} {'avg_eng':>8}")
    for name, group in sorted(by_format.items(), key=lambda kv: _median_views(kv[1]), reverse=True):
        avg_words = _mean([float(_word_count(str(r.get("on_screen_title") or ""))) for r in group])
        avg_eng = _mean([_engagement(r) for r in group])
        print(f"  {name:40} {len(group):>4} {_median_views(group):>11,} {avg_words:>9.1f} {avg_eng:>8.4f}")

    print("\nLine count vs median views")
    by_lines: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_lines[int(row.get("title_line_count") or 0)].append(row)
    for lines in sorted(by_lines):
        group = by_lines[lines]
        print(f"  {lines} lines: n={len(group):>3}  med_views={_median_views(group):,}")

    print("\nWord count vs median views")
    by_words: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_words[_fine_word_bucket(_word_count(str(row.get("on_screen_title") or "")))].append(row)
    for bucket in ["1-5", "6-9", "10-14", "15-19", "20+"]:
        if bucket in by_words:
            group = by_words[bucket]
            print(f"  {bucket:>6} words: n={len(group):>3}  med_views={_median_views(group):,}")


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        path = path / "titles.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _print_counter(title: str, counter: collections.Counter[str], *, limit: int = 12) -> None:
    print(f"\n{title}")
    for key, count in counter.most_common(limit):
        print(f"- {key}: {count}")


def _write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    out_path = path / "title_analysis_summary.csv"
    fields = [
        "candidate_id",
        "video_id",
        "on_screen_title",
        "word_count",
        "word_bucket",
        "title_line_count",
        "title_position",
        "alignment",
        "view_count",
        "like_count",
        "comment_count",
        "engagement_rate",
        "source_url",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            title = str(row.get("on_screen_title") or "")
            count = _word_count(title)
            writer.writerow(
                {
                    "candidate_id": row.get("candidate_id"),
                    "video_id": row.get("video_id"),
                    "on_screen_title": title,
                    "word_count": count,
                    "word_bucket": _bucket_word_count(count),
                    "title_line_count": row.get("title_line_count"),
                    "title_position": row.get("title_position"),
                    "alignment": row.get("alignment"),
                    "view_count": row.get("view_count"),
                    "like_count": row.get("like_count"),
                    "comment_count": row.get("comment_count"),
                    "engagement_rate": row.get("engagement_rate"),
                    "source_url": row.get("source_url"),
                }
            )
    print(f"\nWrote: {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze extracted history title patterns.")
    parser.add_argument("path", type=Path, help="Extraction output directory or titles.json.")
    parser.add_argument("--top", type=int, default=15, help="Top examples to print.")
    args = parser.parse_args()

    rows = _load_rows(args.path)
    with_titles = [row for row in rows if str(row.get("on_screen_title") or "").strip()]
    missing = len(rows) - len(with_titles)
    word_counts = [_word_count(str(row.get("on_screen_title") or "")) for row in with_titles]
    buckets = collections.Counter(_bucket_word_count(count) for count in word_counts)
    positions = collections.Counter(str(row.get("title_position") or "unknown") for row in rows)
    alignments = collections.Counter(str(row.get("alignment") or "unknown") for row in rows)
    line_counts = collections.Counter(
        str(row.get("title_line_count") or 0) for row in rows
    )
    first_words = collections.Counter(
        _words(str(row.get("on_screen_title") or ""))[0]
        for row in with_titles
        if _words(str(row.get("on_screen_title") or ""))
    )
    terms = collections.Counter(
        word
        for row in with_titles
        for word in _words(str(row.get("on_screen_title") or ""))
        if word not in STOPWORDS and len(word) > 2
    )

    print(f"Rows: {len(rows)}")
    print(f"Rows with on-screen title: {len(with_titles)}")
    print(f"Missing title: {missing}")
    print(f"Average title word count: {_mean([float(c) for c in word_counts]):.2f}")
    print(f"Average views with title: {_mean([float(_views(row)) for row in with_titles]):.0f}")
    print(f"Average engagement with title: {_mean([_engagement(row) for row in with_titles]):.4f}")
    _print_counter("Word-count buckets", buckets)
    _print_counter("Title line counts", line_counts)
    _print_counter("Title position", positions)
    _print_counter("Alignment", alignments)
    _print_counter("Common first words", first_words)
    _print_counter("Common non-stopword terms", terms)
    _print_format_performance(with_titles)

    print(f"\nTop {args.top} by views")
    for row in sorted(with_titles, key=_views, reverse=True)[: args.top]:
        title = str(row.get("on_screen_title") or "")
        print(f"- {row.get('view_count') or 0} views | {title} | {row.get('source_url')}")

    output_dir = args.path if args.path.is_dir() else args.path.parent
    _write_summary_csv(output_dir, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
