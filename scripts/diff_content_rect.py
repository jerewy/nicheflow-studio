"""Old-vs-new diff harness for detect_content_rectangle.

Proves the "no side effects" claim for the bottom-caption-preserve change:
run it on the CURRENT code to snapshot a baseline, apply the change, then run
again with --baseline pointing at the snapshot. It prints the EXACT set of
clips whose detected crop rectangle changed — everything else is provably
untouched (identical reel + cover framing).

    # 1) Baseline on current code
    .venv/Scripts/python.exe scripts/diff_content_rect.py --out data/tmp/crop_old.json

    # 2) (apply the code change) then diff against the baseline
    .venv/Scripts/python.exe scripts/diff_content_rect.py --out data/tmp/crop_new.json \
        --baseline data/tmp/crop_old.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

import os

os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

from nicheflow_studio.processing.video import (  # noqa: E402
    detect_content_rectangle,
    probe_video,
)

DOWNLOADS = pathlib.Path("data/downloads/instagram")


def _crop_tuple(crop) -> list[int] | None:
    if crop is None:
        return None
    return [crop.left, crop.top, crop.right, crop.bottom]


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot/diff detect_content_rectangle output.")
    parser.add_argument("--out", required=True, help="Where to write this run's JSON snapshot.")
    parser.add_argument("--baseline", help="A prior snapshot to diff this run against.")
    parser.add_argument("--limit", type=int, help="Only process the first N clips (by name).")
    parser.add_argument("--glob", default="*.mp4", help="Filename glob under the downloads dir.")
    args = parser.parse_args()

    clips = sorted(DOWNLOADS.glob(args.glob))
    if args.limit:
        clips = clips[: args.limit]
    if not clips:
        print(f"No clips matched {DOWNLOADS / args.glob}")
        return

    results: dict[str, list[int] | None] = {}
    print(f"Computing content rectangle for {len(clips)} clip(s)...")
    for index, clip in enumerate(clips, start=1):
        try:
            probe = probe_video(clip)
            crop = detect_content_rectangle(clip, probe)
            results[clip.name] = _crop_tuple(crop)
        except Exception as exc:  # noqa: BLE001 - record failures, don't abort the sweep
            results[clip.name] = None
            print(f"  [{index}/{len(clips)}] ERROR {clip.name}: {exc}")
            continue
        print(f"  [{index}/{len(clips)}] {clip.name}: {results[clip.name]}")

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {len(results)} entries -> {out_path}")

    if not args.baseline:
        return

    baseline = json.loads(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
    changed: list[str] = []
    for name, new_val in results.items():
        old_val = baseline.get(name, "MISSING")
        if old_val != new_val:
            changed.append(name)

    common = [n for n in results if n in baseline]
    print("\n=== DIFF vs baseline ===")
    print(f"clips compared (in both runs): {len(common)}")
    print(f"unchanged (provably no side effect): {len(common) - len([c for c in changed if c in baseline])}")
    print(f"CHANGED: {len(changed)}")
    for name in changed:
        print(f"  ~ {name}")
        print(f"      old: {baseline.get(name, 'MISSING')}")
        print(f"      new: {results[name]}")
    if not changed:
        print("  (none — output identical to baseline)")


if __name__ == "__main__":
    main()
