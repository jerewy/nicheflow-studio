"""Measure the footage-duplicate rate + source-account concentration of a pool.

Answers two questions before committing to a multi-day bulk download:
  1. How many downloaded clips are actually footage duplicates (is dedup paying off)?
  2. How concentrated is the pool by SOURCE account — clips from the same source
     owner are the most likely reposts and make the destination feed repetitive.

Read-only. Uses fingerprints already stored on downloaded assets.
"""
from __future__ import annotations

import collections
import os
import pathlib
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

from nicheflow_studio.db.models import MediaAsset, PoolItem, ScrapeCandidate  # noqa: E402
from nicheflow_studio.db.session import get_session  # noqa: E402
from nicheflow_studio.processing.dedup import fingerprints_match  # noqa: E402

NICHE = "history"
_SOURCE_RE = re.compile(r"_Video_by_(.+?)\.mp4", re.IGNORECASE)


def _source_of(session, asset: MediaAsset) -> str:
    shortcode = asset.source_shortcode
    if shortcode:
        candidate = (
            session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.video_id == shortcode)
            .first()
        )
        if candidate and candidate.channel_name:
            return candidate.channel_name
    path = asset.original_download_path or asset.canonical_source_url or ""
    match = _SOURCE_RE.search(path)
    return match.group(1) if match else "(unknown)"


def main() -> None:
    with get_session() as session:
        items = (
            session.query(PoolItem)
            .filter(PoolItem.niche == NICHE, PoolItem.acceptance_status == "accepted")
            .all()
        )
        downloaded = [
            i.media_asset
            for i in items
            if i.media_asset and i.media_asset.download_status == "downloaded"
        ]
        with_hash = [a for a in downloaded if a.content_hash]
        print(f"history accepted pool: {len(items)}")
        print(f"downloaded: {len(downloaded)}  |  with fingerprint: {len(with_hash)}")

        # --- Source-account concentration ---
        source_counts = collections.Counter(_source_of(session, a) for a in downloaded)
        print(f"\n=== source accounts ({len(source_counts)} distinct) — top 15 ===")
        for name, count in source_counts.most_common(15):
            share = count / max(1, len(downloaded)) * 100
            print(f"  {count:4}  ({share:4.1f}%)  {name}")

        # --- Footage duplicate clustering ---
        clusters: list[list[MediaAsset]] = []
        for asset in with_hash:
            for cluster in clusters:
                if fingerprints_match(asset.content_hash, cluster[0].content_hash):
                    cluster.append(asset)
                    break
            else:
                clusters.append([asset])
        dupes = [c for c in clusters if len(c) > 1]
        redundant = sum(len(c) - 1 for c in dupes)
        rate = redundant / max(1, len(with_hash)) * 100
        print("\n=== footage dedup ===")
        print(
            f"{len(with_hash)} fingerprinted -> {len(clusters)} unique clip(s), "
            f"{len(dupes)} duplicate group(s)"
        )
        print(f"redundant (removable) clips: {redundant}  ({rate:.1f}% of downloaded)")

        same_source = cross_source = 0
        for cluster in dupes:
            sources = {_source_of(session, a) for a in cluster}
            if len(sources) == 1:
                same_source += 1
            else:
                cross_source += 1
        if dupes:
            print(
                f"duplicate groups: {same_source} same-source (your hypothesis), "
                f"{cross_source} cross-source reposts"
            )
            for cluster in dupes[:20]:
                print("  dup group:")
                for asset in cluster:
                    print(f"     {asset.source_shortcode}  <- {_source_of(session, asset)}")


if __name__ == "__main__":
    main()
