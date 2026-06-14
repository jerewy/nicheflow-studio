"""Pool admin — a UAT harness for the Apify shared-pool distribution workflow.

Drives the backend loop on REAL data so the pool -> distribute flow can be
exercised before the in-app buttons exist:

    # See pool sizes + per-account assignment counts
    .venv/Scripts/python.exe scripts/pool_admin.py status

    # List downloaded Instagram clips and whether they're in a pool
    .venv/Scripts/python.exe scripts/pool_admin.py downloaded

    # Accept every downloaded-but-unpooled Instagram clip into a niche pool
    .venv/Scripts/python.exe scripts/pool_admin.py accept --niche history --all

    # Distribute the niche's pool across that niche's accounts
    .venv/Scripts/python.exe scripts/pool_admin.py distribute --niche history
    .venv/Scripts/python.exe scripts/pool_admin.py distribute --niche history --max-per-account 30

Nothing here posts to Instagram — it only plans and records assignments.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

# Generated/console-unsafe characters: force UTF-8 so this runs on a stock
# Windows (cp1252) console (same fix as scripts/test_generation.py).
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

import os

os.environ.setdefault("NICHEFLOW_DATA_DIR", "data")

from nicheflow_studio.db.assignments import (  # noqa: E402
    assignment_counts_by_account,
    distribute_niche,
)
from nicheflow_studio.db.media_library import (  # noqa: E402
    find_media_asset,
    find_or_register_media_asset,
    mark_media_asset_downloaded,
)
from nicheflow_studio.db.models import (  # noqa: E402
    Account,
    DownloadItem,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
)
from nicheflow_studio.db.pools import (  # noqa: E402
    CrossNicheError,
    DuplicateContentError,
    accept_candidate_into_pool,
    accept_into_pool,
    dedupe_pool_by_caption,
    pool_review_rows,
    pool_size,
    remove_pool_item,
    restore_pool_item,
)
from nicheflow_studio.db.session import get_session, init_db  # noqa: E402
from nicheflow_studio.services.pooling import (  # noqa: E402
    repair_pending_review_media_links,
    release_missing_media_assignments,
    release_unpublishable_assignments,
)
from nicheflow_studio.downloader.instagram import instagram_shortcode_from_url  # noqa: E402
from nicheflow_studio.processing.dedup import (  # noqa: E402
    compute_video_fingerprint,
    fingerprints_match,
)

NICHES = ("history", "movie")


def _account_name(session, account_id: int) -> str:
    account = session.get(Account, account_id)
    return account.name if account else f"#{account_id}"


def cmd_status(_args) -> None:
    with get_session() as session:
        print("=== Accounts ===")
        for account in session.query(Account).order_by(Account.id).all():
            print(f"  [{account.id}] {account.name:24} niche={account.niche or '-'}")
        print("\n=== Pools ===")
        for niche in NICHES:
            print(f"  {niche:8} accepted clips: {pool_size(session, niche)}")
        print("\n=== Assignments per account ===")
        for niche in NICHES:
            counts = assignment_counts_by_account(session, niche)
            if not counts:
                print(f"  {niche:8} (none)")
                continue
            for account_id, count in sorted(counts.items()):
                print(f"  {niche:8} {_account_name(session, account_id):24} {count}")


def _downloaded_instagram_items(session) -> list[DownloadItem]:
    items = (
        session.query(DownloadItem)
        .filter(DownloadItem.status == "downloaded")
        .filter(DownloadItem.file_path.isnot(None))
        .all()
    )
    return [i for i in items if instagram_shortcode_from_url(i.source_url) is not None]


def cmd_downloaded(_args) -> None:
    with get_session() as session:
        items = _downloaded_instagram_items(session)
        print(f"=== {len(items)} downloaded Instagram clip(s) ===")
        for item in items:
            asset = find_media_asset(session, source_url=item.source_url)
            pooled = ""
            if asset is not None:
                pools = session.query(PoolItem).filter(PoolItem.media_asset_id == asset.id).all()
                pooled = ",".join(p.niche for p in pools) or "(not pooled)"
            else:
                pooled = "(no media asset!)"
            title = (item.title or "")[:40]
            print(f"  item#{item.id:<4} asset={getattr(asset, 'id', '-'):<4} pool={pooled:18} {title}")


def cmd_backfill(_args) -> None:
    """Register MediaAssets for already-downloaded Instagram clips.

    Downloads made before the pantry wiring have no MediaAsset; this walks them
    and registers/links one from the existing file on disk, so existing
    inventory can enter pools without re-downloading.
    """
    import pathlib as _pathlib

    with get_session() as session:
        items = _downloaded_instagram_items(session)
        created = 0
        linked = 0
        for item in items:
            existing = find_media_asset(session, source_url=item.source_url)
            asset, was_created = find_or_register_media_asset(
                session, source_url=item.source_url, shortcode=item.video_id, platform="instagram"
            )
            if asset.download_status != "downloaded":
                size = None
                try:
                    size = _pathlib.Path(item.file_path).stat().st_size
                except OSError:
                    size = None
                mark_media_asset_downloaded(
                    asset, original_download_path=item.file_path, file_size_bytes=size
                )
            if was_created:
                created += 1
            elif existing is not None:
                linked += 1
        session.commit()
        print(f"Backfill complete: {created} new media asset(s) registered, "
              f"{linked} already existed. Pantry now reflects existing downloads.")


def cmd_fingerprint(_args) -> None:
    """Backfill perceptual fingerprints on downloaded assets + report duplicates.

    One-time pass so the existing library (downloaded before content dedup) gets
    fingerprints, then groups assets whose footage matches — the same clip
    reposted under different shortcodes.
    """
    import pathlib as _pathlib

    with get_session() as session:
        assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.download_status == "downloaded")
            .all()
        )
        computed = 0
        for asset in assets:
            if asset.content_hash or not asset.original_download_path:
                continue
            if not _pathlib.Path(asset.original_download_path).exists():
                continue
            fp = compute_video_fingerprint(_pathlib.Path(asset.original_download_path))
            if fp:
                asset.content_hash = fp
                computed += 1
        session.commit()
        print(f"Fingerprinted {computed} asset(s).")

        # Cluster assets by matching footage.
        fingerprinted = [a for a in assets if a.content_hash]
        clusters: list[list[MediaAsset]] = []
        for asset in fingerprinted:
            placed = False
            for cluster in clusters:
                if fingerprints_match(asset.content_hash, cluster[0].content_hash):
                    cluster.append(asset)
                    placed = True
                    break
            if not placed:
                clusters.append([asset])
        dupes = [c for c in clusters if len(c) > 1]
        print(
            f"{len(fingerprinted)} fingerprinted asset(s) -> {len(clusters)} unique clip(s), "
            f"{len(dupes)} duplicate group(s)."
        )
        for cluster in dupes:
            print("  duplicate footage:")
            for asset in cluster:
                print(f"    asset#{asset.id} {asset.source_shortcode or asset.canonical_source_url}")


def cmd_accept(args) -> None:
    niche = args.niche
    with get_session() as session:
        items = _downloaded_instagram_items(session)
        targets = items if args.all else [i for i in items if i.id == args.item_id]
        if not targets:
            print("No matching downloaded Instagram items. Use --all or --item-id N.")
            return
        accepted = 0
        skipped = 0
        for item in targets:
            asset = find_media_asset(session, source_url=item.source_url)
            if asset is None:
                print(f"  item#{item.id}: no media asset (download predates pantry wiring) — skip")
                skipped += 1
                continue
            already = session.query(PoolItem).filter(PoolItem.media_asset_id == asset.id).all()
            if any(p.niche == niche for p in already):
                skipped += 1
                continue
            accept_into_pool(
                session, media_asset=asset, niche=niche, accepted_reason="pool_admin accept"
            )
            accepted += 1
        session.commit()
        print(f"Accepted {accepted} clip(s) into '{niche}' pool ({skipped} skipped). "
              f"Pool size now {pool_size(session, niche)}.")


def cmd_accept_candidates(args) -> None:
    """Bulk candidate-first accept: pool raw (un-downloaded) candidates as PENDING.

    No download happens — each becomes a pending MediaAsset + PoolItem, so a whole
    trusted source can enter the pool for $0. URL/shortcode dedup is automatic.
    """
    niche = args.niche
    with get_session() as session:
        query = session.query(ScrapeCandidate).filter(
            ScrapeCandidate.state.in_(["candidate", "new"])
        )
        if args.source:
            query = query.filter(ScrapeCandidate.scrape_source_url.like(f"%{args.source}%"))
        candidates = query.order_by(ScrapeCandidate.id.asc()).all()
        if args.limit:
            candidates = candidates[: args.limit]

        if not candidates:
            print("No matching un-pooled candidates. Check --source / --niche.")
            return
        if args.dry_run:
            print(f"[dry-run] {len(candidates)} candidate(s) would be pooled into '{niche}'.")
            return

        accepted = skipped = failed = 0
        for candidate in candidates:
            try:
                accept_candidate_into_pool(
                    session,
                    candidate=candidate,
                    niche=niche,
                    accepted_reason="pool_admin bulk accept",
                )
                accepted += 1
            except (CrossNicheError, DuplicateContentError):
                skipped += 1
            except ValueError:
                failed += 1
        session.commit()
        print(
            f"Pooled {accepted} pending candidate(s) into '{niche}' "
            f"({skipped} skipped, {failed} unusable). Pool size now {pool_size(session, niche)}."
        )


def cmd_dedupe(args) -> None:
    """Pre-download caption dedup: flag pool items that repost the same caption."""
    niche = args.niche
    with get_session() as session:
        before = pool_size(session, niche)
        if args.dry_run:
            # Count without persisting by rolling back the flush.
            result = dedupe_pool_by_caption(session, niche)
            session.rollback()
            print(
                f"[dry-run] {result.flagged} duplicate caption(s) across "
                f"{result.groups} group(s) would be flagged in '{niche}' "
                f"(pool {before} -> {before - result.flagged})."
            )
            return
        result = dedupe_pool_by_caption(session, niche)
        session.commit()
        print(
            f"Flagged {result.flagged} duplicate(s) across {result.groups} caption "
            f"group(s) in '{niche}'. Clean pool size now {pool_size(session, niche)}."
        )


def cmd_pool_list(args) -> None:
    """List pool items for manual review (id, status, shortcode, caption)."""
    with get_session() as session:
        rows = pool_review_rows(session, args.niche, include_inactive=args.all)
        if not rows:
            print(f"No pool items in '{args.niche}'.")
            return
        print(f"=== {len(rows)} pool item(s) in '{args.niche}' "
              f"({'all statuses' if args.all else 'accepted only'}) ===")
        for row in rows:
            dist = f" -> {', '.join(row.distributed_to)}" if row.distributed_to else ""
            print(
                f"  #{row.pool_item_id:<5} {row.status:9} {row.shortcode or '-':14} "
                f"{(row.caption or '(no caption)')[:60]}{dist}"
            )


def cmd_pool_remove(args) -> None:
    """Manually remove a pool item (or all in a niche by shortcode) from the pool."""
    with get_session() as session:
        if args.item_id is not None:
            ok = remove_pool_item(session, pool_item_id=args.item_id, reason=args.reason)
            session.commit()
            print(f"Removed pool item #{args.item_id}." if ok else f"No pool item #{args.item_id}.")
            return
        # Remove by shortcode (within the niche).
        rows = [r for r in pool_review_rows(session, args.niche) if r.shortcode == args.shortcode]
        if not rows:
            print(f"No accepted pool item with shortcode '{args.shortcode}' in '{args.niche}'.")
            return
        for row in rows:
            remove_pool_item(session, pool_item_id=row.pool_item_id, reason=args.reason)
        session.commit()
        print(f"Removed {len(rows)} pool item(s) with shortcode '{args.shortcode}'.")


def cmd_pool_restore(args) -> None:
    with get_session() as session:
        ok = restore_pool_item(session, pool_item_id=args.item_id)
        session.commit()
        print(f"Restored pool item #{args.item_id}." if ok else f"No pool item #{args.item_id}.")


def cmd_distribute(args) -> None:
    niche = args.niche
    with get_session() as session:
        created = distribute_niche(
            session, niche, max_per_account=args.max_per_account
        )
        session.commit()
        if not created:
            accounts = session.query(Account).filter(Account.niche == niche).count()
            print(
                f"Nothing distributed for '{niche}'. "
                f"(accounts in niche: {accounts}, pool: {pool_size(session, niche)}, "
                f"all pool items may already be assigned.)"
            )
            return
        print(f"Distributed {len(created)} clip(s) across '{niche}' accounts:")
        for account_id, count in sorted(assignment_counts_by_account(session, niche).items()):
            print(f"  {_account_name(session, account_id):24} {count}")


def cmd_release_unpublishable(args) -> None:
    """Free pool clips locked as 'assigned' on accounts that can't publish.

    Cleans up after the pre-fix top-up tick that booked clips onto accounts
    with no Instagram profile or no recorded login: deletes those accounts'
    'assigned' rows and their untouched pending_review items, so the clips
    become distributable to publish-ready accounts again. posted /
    skipped_duplicate rows are never touched.
    """
    result = release_unpublishable_assignments(dry_run=args.dry_run)
    if not result["accounts"]:
        print("Nothing to release: no 'assigned' rows on unpublishable accounts.")
        return
    prefix = "[dry-run] " if args.dry_run else ""
    verb = "Would release" if args.dry_run else "Released"
    for row in result["accounts"]:
        print(
            f"  {row['niche']:8} {row['account_name']:24} "
            f"assignments={row['assignments']} pending_items={row['pending_items']}"
        )
    print(
        f"{prefix}{verb} {result['released_assignments']} assignment(s) and "
        f"{result['deleted_pending_items']} pending-review item(s); the clips are "
        f"distributable to publish-ready accounts again."
    )


def cmd_release_missing_media(args) -> None:
    """Free legacy assignments whose shared media was never downloaded."""
    result = release_missing_media_assignments(dry_run=args.dry_run)
    if not result["assignments"]:
        print("Nothing to release: every assigned clip has local media or progressed work.")
        return
    prefix = "[dry-run] " if args.dry_run else ""
    verb = "Would release" if args.dry_run else "Released"
    for row in result["assignments"]:
        print(
            f"  {row['niche']:8} {row['account_name']:24} "
            f"assignment=#{row['assignment_id']} pool=#{row['pool_item_id']} "
            f"shortcode={row['shortcode'] or '-'} pending_items={row['pending_items']}"
        )
    print(
        f"{prefix}{verb} {result['released_assignments']} assignment(s) and "
        f"{result['deleted_pending_items']} untouched pending-review item(s); "
        f"the pool clips can be retried by the guarded distribution flow."
    )


def cmd_repair_pending_media(args) -> None:
    """Backfill legacy pending-review rows from downloaded shared assets."""
    result = repair_pending_review_media_links(dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    verb = "Would repair" if args.dry_run else "Repaired"
    print(f"{prefix}{verb} {result['repaired_items']} pending-review media link(s).")


def main() -> None:
    init_db()
    parser = argparse.ArgumentParser(description="NicheFlow pool admin / UAT harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show accounts, pool sizes, assignment counts.")
    sub.add_parser("downloaded", help="List downloaded Instagram clips + pool status.")
    sub.add_parser("backfill", help="Register MediaAssets for existing IG downloads.")
    sub.add_parser("fingerprint", help="Backfill content fingerprints + report duplicate footage.")

    p_accept = sub.add_parser("accept", help="Accept downloaded clips into a niche pool.")
    p_accept.add_argument("--niche", required=True, choices=NICHES)
    group = p_accept.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Accept all unpooled downloaded clips.")
    group.add_argument("--item-id", type=int, help="Accept one DownloadItem by id.")

    p_accept_cand = sub.add_parser(
        "accept-candidates",
        help="Bulk-accept raw (un-downloaded) candidates into a niche pool as PENDING.",
    )
    p_accept_cand.add_argument("--niche", required=True, choices=NICHES)
    p_accept_cand.add_argument("--source", help="Only candidates whose scrape source URL contains this.")
    p_accept_cand.add_argument("--limit", type=int, default=None, help="Cap how many to pool.")
    p_accept_cand.add_argument("--dry-run", action="store_true", help="Count only; write nothing.")

    p_dedupe = sub.add_parser(
        "dedupe", help="Flag pool items that repost the same caption (pre-download)."
    )
    p_dedupe.add_argument("--niche", required=True, choices=NICHES)
    p_dedupe.add_argument("--dry-run", action="store_true", help="Count only; write nothing.")

    p_list = sub.add_parser("pool-list", help="List pool items for manual review.")
    p_list.add_argument("--niche", required=True, choices=NICHES)
    p_list.add_argument("--all", action="store_true", help="Include removed/duplicate items.")

    p_remove = sub.add_parser("pool-remove", help="Manually remove a clip from the pool.")
    p_remove.add_argument("--niche", required=True, choices=NICHES)
    grp = p_remove.add_mutually_exclusive_group(required=True)
    grp.add_argument("--item-id", type=int, help="Pool item id to remove.")
    grp.add_argument("--shortcode", help="Remove accepted item(s) with this shortcode.")
    p_remove.add_argument("--reason", default="manual removal", help="Why it's removed.")

    p_restore = sub.add_parser("pool-restore", help="Restore a removed/duplicate pool item.")
    p_restore.add_argument("--item-id", type=int, required=True)

    p_dist = sub.add_parser("distribute", help="Distribute a niche pool across its accounts.")
    p_dist.add_argument("--niche", required=True, choices=NICHES)
    p_dist.add_argument("--max-per-account", type=int, default=None)

    p_release = sub.add_parser(
        "release-unpublishable",
        help="Delete 'assigned' rows (+ pending-review items) on accounts that can't publish.",
    )
    p_release.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    p_release_missing = sub.add_parser(
        "release-missing-media",
        help="Release legacy assignments whose shared media is missing.",
    )
    p_release_missing.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    p_repair_pending = sub.add_parser(
        "repair-pending-media",
        help="Link legacy pending-review rows to downloaded shared media.",
    )
    p_repair_pending.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    args = parser.parse_args()
    {
        "status": cmd_status,
        "downloaded": cmd_downloaded,
        "backfill": cmd_backfill,
        "fingerprint": cmd_fingerprint,
        "accept": cmd_accept,
        "accept-candidates": cmd_accept_candidates,
        "dedupe": cmd_dedupe,
        "pool-list": cmd_pool_list,
        "pool-remove": cmd_pool_remove,
        "pool-restore": cmd_pool_restore,
        "distribute": cmd_distribute,
        "release-unpublishable": cmd_release_unpublishable,
        "release-missing-media": cmd_release_missing_media,
        "repair-pending-media": cmd_repair_pending_media,
    }[args.command](args)


if __name__ == "__main__":
    main()
