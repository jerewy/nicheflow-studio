"""Bulk-download pool clips at the PARENT (MediaAsset) level — for footage dedup.

Candidate-first accept pools most clips as ``pending`` ($0, never downloaded).
Footage dedup (Phase B) needs the real file on disk, so before you can footage-
dedup the *whole* pool you must download it. This script does exactly that and
NOTHING else:

  * It downloads each accepted pool clip whose asset is still ``pending``,
    computes the perceptual fingerprint, and marks the MediaAsset downloaded.
  * It works at the shared parent (MediaAsset) level only. It never creates or
    touches Assignments, and never changes any Account's state — so the
    Past Moments Daily (or any account) "video status" is left exactly as-is.
  * It does NOT flag duplicates. Download first, then review with
    ``pool_admin.py fingerprint`` (the "check everything" step), then distribute.

Resume-safe: re-running only fetches what's still missing, so a crash or Ctrl-C
mid-run just means you re-run it.

    # Dry-run: show how many clips would be downloaded (writes nothing)
    .venv/Scripts/python.exe scripts/download_pool.py --niche history --dry-run

    # Download every pending history pool clip
    .venv/Scripts/python.exe scripts/download_pool.py --niche history

    # Cap a first batch to sanity-check the pipeline
    .venv/Scripts/python.exe scripts/download_pool.py --niche history --limit 25
"""
from __future__ import annotations

import argparse
import pathlib
import random
import sys
import time

# Force UTF-8 so this runs on a stock Windows (cp1252) console.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

import os

os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

from nicheflow_studio.core.paths import downloads_dir  # noqa: E402
from nicheflow_studio.db.media_library import mark_media_asset_downloaded  # noqa: E402
from nicheflow_studio.db.models import MediaAsset, PoolItem, ScrapeCandidate  # noqa: E402
from nicheflow_studio.db.session import get_session, init_db  # noqa: E402
from nicheflow_studio.downloader.instagram import (  # noqa: E402
    download_instagram_url,
    instagram_shortcode_from_url,
)
from nicheflow_studio.processing.dedup import compute_video_fingerprint  # noqa: E402

NICHES = ("history", "movie")


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _file_size_bytes(path: pathlib.Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _already_on_disk(asset: MediaAsset) -> bool:
    return (
        asset.download_status == "downloaded"
        and bool(asset.original_download_path)
        and pathlib.Path(asset.original_download_path).exists()
    )


# Instagram throttles anonymous requests per-IP. yt-dlp surfaces it with these
# phrases. A run of these in a row means "this IP is rate-limited" — the fix is
# to switch IP (VPN / phone hotspot) and resume, not to keep hammering.
_RATE_LIMIT_MARKERS = (
    "rate-limit",
    "login required",
    "not available",
    "please wait a few minutes",
    "too many requests",
)


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


# Connectivity loss (wifi dropped, DNS down). A run of these means "no internet
# right now" — pointless to keep trying, so it should auto-stop just like a
# rate-limit, but with a different remedy (reconnect, don't switch IP).
_NETWORK_ERROR_MARKERS = (
    "getaddrinfo failed",
    "failed to resolve",
    "name or service not known",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset",
    "connection aborted",
    "timed out",
    "network is unreachable",
    "no route to host",
)


def _is_network_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _NETWORK_ERROR_MARKERS)


def _pending_pool_asset_ids(
    session,
    niche: str | None,
    *,
    source: str | None = None,
    randomize: bool = False,
) -> list[int]:
    """IDs of accepted-pool MediaAssets that still need a download.

    Filters at the parent (MediaAsset) level; PoolItem only scopes *which* assets
    are in-scope for the niche. No Assignment/Account rows are read or touched.

    ``source`` limits to one uploader (ScrapeCandidate.channel_name, e.g.
    "crazyfactscorner"). ``randomize`` shuffles instead of oldest-id-first — use
    it for unbiased measurement batches, since id order clusters by source (the
    artifact that earlier made one source look like 99.7% of the pool).
    """
    query = (
        session.query(MediaAsset)
        .join(PoolItem, PoolItem.media_asset_id == MediaAsset.id)
        .filter(PoolItem.acceptance_status == "accepted")
    )
    if niche is not None:
        query = query.filter(PoolItem.niche == niche)
    # Distinct: a clip can be pooled under more than one accepted PoolItem.
    assets = query.order_by(MediaAsset.id.asc()).all()

    # Source filter by uploader: map shortcode -> channel_name in one query.
    channel_by_shortcode: dict[str, str | None] = {}
    if source is not None:
        shortcodes = [a.source_shortcode for a in assets if a.source_shortcode]
        if shortcodes:
            rows = (
                session.query(ScrapeCandidate.video_id, ScrapeCandidate.channel_name)
                .filter(ScrapeCandidate.video_id.in_(set(shortcodes)))
                .all()
            )
            channel_by_shortcode = {video_id: channel for video_id, channel in rows}

    seen: set[int] = set()
    out: list[int] = []
    for asset in assets:
        if asset.id in seen:
            continue
        seen.add(asset.id)
        if _already_on_disk(asset):
            continue
        if source is not None and channel_by_shortcode.get(asset.source_shortcode) != source:
            continue
        out.append(asset.id)
    if randomize:
        random.shuffle(out)
    return out


def main() -> None:
    init_db()
    parser = argparse.ArgumentParser(
        description="Bulk-download pending pool clips at the MediaAsset (parent) "
        "level so the pool can be footage-deduped. Never creates assignments or "
        "changes account status."
    )
    parser.add_argument("--niche", choices=NICHES, help="Limit to one niche pool (default: all).")
    parser.add_argument("--limit", type=int, help="Download at most N clips this run.")
    parser.add_argument(
        "--source",
        help="Only download clips from this source account (uploader/channel_name), "
        "e.g. crazyfactscorner or theanomalists. For source-balanced batches.",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="Shuffle order instead of oldest-first, so a --limit batch is an "
        "unbiased sample (id order clusters by source). Use for dedupe measurement.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Count what would download; write nothing."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Base seconds to pause between downloads (jittered +-40%%) to avoid "
        "Instagram IP rate-limiting. Use 0 to disable. Default: 3.0",
    )
    parser.add_argument(
        "--max-rate-errors",
        type=int,
        default=3,
        help="Stop after this many CONSECUTIVE rate-limit OR offline errors "
        "(throttled IP, or wifi dropped). Avoids grinding through failures when "
        "something's wrong. Keep low. 0 disables. Default: 3",
    )
    args = parser.parse_args()

    with get_session() as session:
        asset_ids = _pending_pool_asset_ids(
            session, args.niche, source=args.source, randomize=args.random
        )
    if args.limit:
        asset_ids = asset_ids[: args.limit]

    scope = args.niche or "all"
    if args.source:
        scope = f"{scope}/{args.source}"
    if args.random:
        scope = f"{scope} (random)"
    if not asset_ids:
        print(f"Nothing to download: every '{scope}' pool clip is already on disk.")
        return
    if args.dry_run:
        print(f"[dry-run] {len(asset_ids)} pending '{scope}' pool clip(s) would be downloaded.")
        return

    out_dir = downloads_dir() / "instagram"
    total = len(asset_ids)
    downloaded = skipped = failed = 0
    errors: list[str] = []
    started = time.monotonic()
    attempts = 0  # network attempts so far — drives the ETA, not free skips
    consecutive_stop_errors = 0  # a run of rate-limit OR offline errors => stop
    print(
        f"Downloading {total} pending '{scope}' pool clip(s) at the parent level "
        f"(delay ~{args.delay:g}s between clips)...",
        flush=True,
    )

    def _eta() -> str:
        """Rough time-remaining from the average per-attempt time so far."""
        if attempts < 1:
            return "?"
        per = (time.monotonic() - started) / attempts
        return _fmt_duration(per * (total - index))

    for index, asset_id in enumerate(asset_ids, start=1):
        # Re-read per asset so progress is committed incrementally (resume-safe).
        with get_session() as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                continue
            url = asset.canonical_source_url
            shortcode = asset.source_shortcode
            if _already_on_disk(asset):
                skipped += 1
                continue

        label = shortcode or url
        if instagram_shortcode_from_url(url) is None and not shortcode:
            failed += 1
            errors.append(f"{label}: not an Instagram URL — skipped")
            print(f"  [{index}/{total}] skip (non-Instagram): {label}", flush=True)
            continue

        try:
            result = download_instagram_url(url=url, output_dir=out_dir)
            attempts += 1
            file_path = pathlib.Path(str(result.file_path))
            content_hash = None
            try:
                content_hash = compute_video_fingerprint(file_path)
            except Exception:  # noqa: BLE001 - a bad fingerprint must not block the download
                content_hash = None
            with get_session() as session:
                asset = session.get(MediaAsset, asset_id)
                if asset is None:
                    continue
                mark_media_asset_downloaded(
                    asset,
                    original_download_path=str(file_path),
                    file_size_bytes=_file_size_bytes(file_path),
                    content_hash=content_hash,
                )
                session.commit()
            downloaded += 1
            consecutive_stop_errors = 0  # a real success clears the error streak
            print(f"  [{index}/{total}] ok: {label}  (~{_eta()} left)", flush=True)
        except KeyboardInterrupt:
            print("\nInterrupted — progress so far is saved. Re-run to resume.", flush=True)
            break
        except Exception as exc:  # noqa: BLE001
            attempts += 1
            failed += 1
            errors.append(f"{label}: {exc}")
            is_rate = _is_rate_limit_error(exc)
            is_network = _is_network_error(exc)
            if is_rate or is_network:
                consecutive_stop_errors += 1
                kind = "RATE-LIMITED" if is_rate else "OFFLINE"
                print(
                    f"  [{index}/{total}] {kind} ({consecutive_stop_errors}): {label}",
                    flush=True,
                )
                if args.max_rate_errors and consecutive_stop_errors >= args.max_rate_errors:
                    if is_network:
                        reason = (
                            "your internet/wifi looks down (can't resolve instagram.com). "
                            "Reconnect and re-run the same command to resume"
                        )
                    else:
                        reason = (
                            "this IP is throttled. Back off (or switch IP) and re-run the "
                            "same command later to resume"
                        )
                    print(
                        f"\nStopped: {consecutive_stop_errors} {kind.lower()} errors in a row — "
                        f"{reason} (progress is saved).",
                        flush=True,
                    )
                    break
            else:
                consecutive_stop_errors = 0
                print(f"  [{index}/{total}] FAILED: {label}: {exc}", flush=True)

        # Polite jittered pause after a real network attempt (not after skips/last).
        if args.delay > 0 and index < total:
            time.sleep(random.uniform(args.delay * 0.6, args.delay * 1.4))

    print(
        f"\nDone. downloaded={downloaded} skipped(already on disk)={skipped} failed={failed}. "
        f"No assignments created; account status unchanged."
    )
    if errors:
        print(f"{len(errors)} error(s):")
        for line in errors[:20]:
            print(f"  - {line}")
        if len(errors) > 20:
            print(f"  ...and {len(errors) - 20} more.")
    print("\nNext: review footage dupes with `pool_admin.py fingerprint`, then distribute.")


if __name__ == "__main__":
    main()
