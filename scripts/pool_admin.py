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
from nicheflow_studio.db.models import Account, DownloadItem, PoolItem  # noqa: E402
from nicheflow_studio.db.pools import accept_into_pool, pool_size  # noqa: E402
from nicheflow_studio.db.session import get_session, init_db  # noqa: E402
from nicheflow_studio.downloader.instagram import instagram_shortcode_from_url  # noqa: E402

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


def main() -> None:
    init_db()
    parser = argparse.ArgumentParser(description="NicheFlow pool admin / UAT harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show accounts, pool sizes, assignment counts.")
    sub.add_parser("downloaded", help="List downloaded Instagram clips + pool status.")
    sub.add_parser("backfill", help="Register MediaAssets for existing IG downloads.")

    p_accept = sub.add_parser("accept", help="Accept downloaded clips into a niche pool.")
    p_accept.add_argument("--niche", required=True, choices=NICHES)
    group = p_accept.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Accept all unpooled downloaded clips.")
    group.add_argument("--item-id", type=int, help="Accept one DownloadItem by id.")

    p_dist = sub.add_parser("distribute", help="Distribute a niche pool across its accounts.")
    p_dist.add_argument("--niche", required=True, choices=NICHES)
    p_dist.add_argument("--max-per-account", type=int, default=None)

    args = parser.parse_args()
    {
        "status": cmd_status,
        "downloaded": cmd_downloaded,
        "backfill": cmd_backfill,
        "accept": cmd_accept,
        "distribute": cmd_distribute,
    }[args.command](args)


if __name__ == "__main__":
    main()
