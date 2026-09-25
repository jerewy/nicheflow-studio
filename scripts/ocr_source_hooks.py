"""OCR the source account's burned-in hook off clips we already downloaded.

``draft_handoff._on_screen_hook_lines`` calls that hook "the single most useful
line in the block", and two rules in the shipping prompt exist to use it: the
evidence block's "match the angle, never the wording", and ``title_lab``'s
rule that keeps the hook's angle while dropping any claim the evidence does
not support. Both are dead when the hook is missing, and
coverage is ~14% of scraped candidates, so for most reels they never fire.

The gap is not a hard one. The hook is text burned into the video, so reading it
is OCR, not vision: RapidOCR (PP-OCRv4 ONNX, CPU, no quota, already a
dependency) does it locally for free. ``extract_history_titles.py`` already owns
that pipeline but sources its images by downloading a THUMBNAIL, which is the
right call when building a research dataset over someone else's account. For our
own reels the video is already on disk at full resolution, so this reads a frame
instead and writes the result straight into the column the prompt reads.

    .venv\\Scripts\\python.exe scripts\\ocr_source_hooks.py --account "Vaulted History"
    .venv\\Scripts\\python.exe scripts\\ocr_source_hooks.py --item-ids 1918 --dry-run

Existing hooks are never overwritten unless --refresh is passed: the 719 rows
already populated came from the thumbnail path and some were hand-checked.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from nicheflow_studio.db.models import Account, DownloadItem, ScrapeCandidate  # noqa: E402
from nicheflow_studio.db.session import get_session  # noqa: E402

# One implementation, in the package, because the batch path imports it too and a
# packaged build has no scripts directory. This script is now just a bulk driver.
from nicheflow_studio.processing.on_screen_hook import (  # noqa: E402
    MIN_HOOK_SCORE,
    read_hook,
)


def _targets(args: argparse.Namespace) -> list[dict[str, Any]]:
    with get_session() as session:
        query = session.query(DownloadItem).filter(
            DownloadItem.file_path.is_not(None), DownloadItem.file_path != ""
        )
        if args.item_ids:
            query = query.filter(DownloadItem.id.in_([int(i) for i in args.item_ids]))
        elif args.account:
            accounts = [
                a.id
                for a in session.query(Account).all()
                if args.account.casefold() in (a.name or "").casefold()
                or str(a.id) == args.account
            ]
            if not accounts:
                raise SystemExit(f"No account matched {args.account!r}")
            query = query.filter(DownloadItem.account_id.in_(accounts))
        rows = []
        for item in query.order_by(DownloadItem.id).all():
            video_id = (item.video_id or "").strip()
            if not video_id:
                continue
            existing = (
                session.query(ScrapeCandidate.on_screen_title)
                .filter(ScrapeCandidate.video_id == video_id)
                .first()
            )
            if existing is None:
                # A clip on disk with no candidate row behind it (manual intake,
                # or a candidate since pruned). OCR would read it and the write
                # would land nowhere, so it is not a target at all.
                continue
            if (existing[0] or "").strip() and not args.refresh:
                continue
            rows.append(
                {"item_id": item.id, "video_id": video_id, "file_path": item.file_path}
            )
        if args.limit:
            rows = rows[: args.limit]
        return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="account name (or part of it), or its id")
    parser.add_argument("--item-ids", nargs="+", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--refresh", action="store_true", help="re-read hooks that already exist")
    parser.add_argument("--dry-run", action="store_true", help="read and print, write nothing")
    parser.add_argument(
        "--min-score",
        type=float,
        default=MIN_HOOK_SCORE,
        help="skip a reading below this mean OCR confidence rather than store noise",
    )
    args = parser.parse_args()

    # Source reels carry non-Latin burned-in text often enough that one CJK
    # character used to kill a 1,400-clip run: a Windows console is cp1252 and
    # print() raises rather than dropping the glyph.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    rows = _targets(args)
    if not rows:
        print("Nothing to do: every matching reel already has a hook.")
        return 0
    print(f"{len(rows)} reel(s) to read.\n")

    wrote = weak = failed = empty = 0
    for row in rows:
        path = Path(row["file_path"])
        if not path.exists():
            print(f"  #{row['item_id']}: file missing, skipped")
            failed += 1
            continue
        try:
            hook, score = read_hook(path)
        except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the run
            print(f"  #{row['item_id']}: FAILED {exc}")
            failed += 1
            continue
        if not hook:
            print(f"  #{row['item_id']}: no text found")
            empty += 1
            continue
        if score < args.min_score:
            print(f"  #{row['item_id']}: score {score:.2f} below floor, skipped: {hook[:70]}")
            weak += 1
            continue
        print(f"  #{row['item_id']}: [{score:.2f}] {hook[:90]}")
        if args.dry_run:
            continue
        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.video_id == row["video_id"])
                .all()
            )
            for candidate in candidates:
                candidate.on_screen_title = hook
            session.commit()
            wrote += len(candidates)

    verb = "would write" if args.dry_run else "wrote"
    print(f"\n{verb} {wrote} | below floor {weak} | no text {empty} | failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
