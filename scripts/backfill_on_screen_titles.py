"""Copy OCR'd on-screen titles from the analysis sidecars into scrape_candidates.

``extract_history_titles.py`` writes ``data/title_analysis/<run>/titles.json``
and deliberately does not touch the app database, so the source account's own
on-screen hook has only ever been available for offline analysis. The drafter
never saw it and wrote from the scraped caption instead, which on these accounts
is loose descriptive prose ("Fierce pursuit unleashes absolute mayhem as a woman
clad in bright pink...") rather than the line that actually earned the views
("The Breed (2006) features a scene so accidentally hilarious it completely
steals the movie"). Titles came out describing the footage as a result.

Idempotent: re-running only fills rows that are still empty unless --overwrite
is passed. Reads every run directory, newest last, so a later re-extraction of
the same reel wins.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nicheflow_studio.db.models import ScrapeCandidate  # noqa: E402
from nicheflow_studio.db.session import get_session  # noqa: E402
from nicheflow_studio.core.paths import data_dir  # noqa: E402


def load_ocr_titles(root: pathlib.Path) -> dict[str, str]:
    """``{video_id: on_screen_title}`` across every extraction run."""
    titles: dict[str, str] = {}
    if not root.exists():
        return titles
    for run_dir in sorted(root.iterdir()):
        payload_path = run_dir / "titles.json"
        if not payload_path.is_file():
            continue
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            for key in ("titles", "items", "rows", "records"):
                if key in payload:
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            video_id = (row.get("video_id") or "").strip()
            text = " ".join(str(row.get("on_screen_title") or "").split())
            # Sub-3-word OCR output is noise, not a hook.
            if video_id and len(text.split()) >= 3:
                titles[video_id] = text
    return titles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--overwrite", action="store_true", help="replace existing values")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    titles = load_ocr_titles(data_dir() / "title_analysis")
    print(f"on-screen titles found in the sidecars: {len(titles)}")
    if not titles:
        return 0

    filled = skipped = 0
    with get_session() as session:
        candidates = (
            session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.video_id.is_not(None))
            .all()
        )
        for candidate in candidates:
            text = titles.get(candidate.video_id or "")
            if not text:
                continue
            if candidate.on_screen_title and not args.overwrite:
                skipped += 1
                continue
            if not args.dry_run:
                candidate.on_screen_title = text
            filled += 1
        if not args.dry_run:
            session.commit()
        total = len(candidates)

    verb = "would fill" if args.dry_run else "filled"
    print(f"{verb}={filled} already_set={skipped} candidates={total}")
    if total:
        print(f"coverage after this run: {(filled + skipped) / total:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
