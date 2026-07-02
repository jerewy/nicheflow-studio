"""Pooling / Distribution read views (UI-independent).

A read-only overview of the shared niche pools and how their accepted clips are
distributed across accounts, backing the migrated Pool & Distribute screen.

Scope is deliberately read-only: pool intake, acceptance/rejection, dedup,
pruning, and distribution planning are complex, partly irreversible operations
that remain in the desktop app and the pool-admin scripts
(``scripts/pool_admin.py``). This screen surfaces state; it does not mutate pools.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Callable

from nicheflow_studio.core.account_health import HealthState, local_health
from nicheflow_studio.core.distribution import DEFAULT_DISTRIBUTE_DAILY_TARGET
from nicheflow_studio.core.instagram_profile_pool import ProfilePool
from nicheflow_studio.core.paths import downloads_dir
from nicheflow_studio.db import assignments as assignments_db, pools
from nicheflow_studio.db.media_library import mark_media_asset_downloaded
from nicheflow_studio.processing.dedup import safe_video_fingerprint
from nicheflow_studio.db.models import (
    Account,
    Assignment,
    DownloadItem,
    MediaAsset,
    PoolItem,
    ScrapeCandidate,
)
from nicheflow_studio.db.session import get_session
from nicheflow_studio.downloader.failures import looks_like_missing_source
from nicheflow_studio.downloader.instagram import download_instagram_url
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio import queue as queue_module

logger = logging.getLogger(__name__)

# The shared-pool niches (docs/SOURCING_POOLING_PLAN.md). Kept explicit so the
# overview is stable even before any pool rows exist.
NICHES = ("history", "movie")


class PoolingError(ServiceError):
    """Raised for invalid pooling queries (e.g. an unknown niche)."""


def _asset_is_downloaded(asset: MediaAsset) -> bool:
    return bool(
        asset.download_status == "downloaded"
        and asset.original_download_path
        and Path(asset.original_download_path).exists()
    )


def _download_error_message(exc: Exception) -> str:
    message = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
    return message.removeprefix("ERROR:").strip()[:200] or exc.__class__.__name__


def _ensure_pool_item_downloaded(pool_item_id: int) -> None:
    """Materialize a pool item's shared asset before it can be assigned."""
    with get_session() as session:
        pool_item = session.get(PoolItem, pool_item_id)
        if pool_item is None:
            raise PoolingError(f"No pool item with id {pool_item_id}.")
        asset = session.get(MediaAsset, pool_item.media_asset_id)
        if asset is None:
            raise PoolingError(f"Pool item {pool_item_id} has no media asset.")
        if _asset_is_downloaded(asset):
            return
        asset_id = asset.id
        source_url = asset.canonical_source_url

    try:
        result = download_instagram_url(
            url=source_url,
            output_dir=downloads_dir() / "instagram",
        )
        file_path = Path(str(result.file_path))
        if not file_path.exists():
            raise FileNotFoundError(f"Downloader returned a missing file: {file_path}")
    except Exception as exc:  # noqa: BLE001 - convert to a user-facing service error
        if looks_like_missing_source(str(exc)):
            # Permanent: retire the asset so the dead reel leaves the pool and
            # review queue instead of failing again on every approve/distribute.
            queue_module.retire_gone_source(
                media_asset_id=asset_id,
                source_url=source_url,
                shortcode=None,
                detail=_download_error_message(exc),
            )
            raise PoolingError(
                "The original reel is gone (deleted or made private). "
                "It was removed from the pool; re-run Distribute to refill the slot."
            ) from exc
        raise PoolingError(
            f"Could not download this clip before distribution: {_download_error_message(exc)}"
        ) from exc

    # Perceptual fingerprint for cross-repost footage dedup. Computed off the DB
    # session (it reads the file) and best-effort so it never blocks distribution.
    content_hash = safe_video_fingerprint(file_path)

    with get_session() as session:
        asset = session.get(MediaAsset, asset_id)
        if asset is None:
            raise PoolingError(f"Media asset {asset_id} disappeared during download.")
        mark_media_asset_downloaded(
            asset,
            original_download_path=str(file_path),
            file_size_bytes=file_path.stat().st_size,
            content_hash=content_hash,
        )
        session.commit()


def _unassigned_pool_item_ids(niche: str) -> list[int]:
    """Currently unassigned accepted pool item ids for a niche (no download).

    Distribution ranks and assigns from these immediately; the chosen clips'
    footage is fetched off the request path by :func:`_start_background_download`
    so the screen never blocks on downloads.
    """
    try:
        with get_session() as session:
            already = assignments_db.assigned_pool_item_ids(session, niche)
            return [
                item.id
                for item in pools.pool_items_for_niche(session, niche)
                if item.id not in already
            ]
    except ValueError as exc:
        raise PoolingError(str(exc)) from exc


# Heavy IG downloads are serialized so a burst of distributions (or overlapping
# background workers) never opens several browsers/yt-dlp runs at once.
_DOWNLOAD_LOCK = threading.Lock()


def _download_pool_assets(pool_item_ids: list[int]) -> None:
    """Download any not-yet-downloaded footage for these pool items, then link it
    onto the pending-review Processing rows. Synchronous; best-effort per clip."""
    downloaded_any = False
    for pool_item_id in pool_item_ids:
        with _DOWNLOAD_LOCK:
            try:
                _ensure_pool_item_downloaded(pool_item_id)
                downloaded_any = True
            except PoolingError as exc:
                logger.warning("Background pool download failed for %s: %s", pool_item_id, exc)
    if downloaded_any:
        try:
            repair_pending_review_media_links()
        except Exception:  # noqa: BLE001 - background backfill must not crash the worker
            logger.exception("Linking downloaded footage to pending-review items failed.")


def _start_background_download(pool_item_ids: list[int]) -> None:
    """Fetch the assigned clips' footage on a daemon thread so distribution
    returns instantly. Already-downloaded clips are skipped cheaply."""
    ids = [int(pool_item_id) for pool_item_id in pool_item_ids]
    if not ids:
        return
    threading.Thread(
        target=_download_pool_assets,
        args=(ids,),
        name="nicheflow-pool-download",
        daemon=True,
    ).start()


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def overview() -> dict:
    """Per-niche pool stats + per-account assignment counts."""
    with get_session() as session:
        names = {a.id: a.name for a in session.query(Account).all()}
        niches = []
        for niche in NICHES:
            stats = pools.niche_pool_stats(session, niche)
            counts = assignments_db.assignment_counts_by_account(session, niche)
            per_account = [
                {
                    "account_id": account_id,
                    "account_name": names.get(account_id, f"#{account_id}"),
                    "count": count,
                }
                for account_id, count in sorted(counts.items(), key=lambda kv: -kv[1])
            ]
            niches.append(
                {
                    "niche": niche,
                    "pooled": stats.pooled,
                    "assigned": stats.assigned,
                    "unused": stats.unused,
                    "rejected": stats.rejected,
                    "pending": stats.pending,
                    "assignments_by_account": per_account,
                }
            )
        return {"niches": niches}


def _clip_label(asset: MediaAsset | None, pool_item_id: int) -> str:
    if asset is not None:
        if asset.source_shortcode:
            return asset.source_shortcode
        if asset.original_download_path:
            return Path(asset.original_download_path).name
        if asset.canonical_source_url:
            return asset.canonical_source_url
    return f"item#{pool_item_id}"


def _candidate_for_asset(session, asset: MediaAsset | None) -> ScrapeCandidate | None:
    if asset is None:
        return None
    if asset.source_shortcode:
        candidate = (
            session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.video_id == asset.source_shortcode)
            .first()
        )
        if candidate is not None:
            return candidate
    if asset.canonical_source_url:
        return (
            session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.source_url == asset.canonical_source_url)
            .first()
        )
    return None


def review_queue(niche: str, source_label: str | None = None) -> list[dict]:
    """Pending pool clips ranked for owner review; never changes their status."""
    source_filter = (source_label or "").strip()
    with get_session() as session:
        try:
            items = pools.pool_items_for_niche(
                session, niche, status=pools.POOL_STATUS_PENDING_REVIEW
            )
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        signals = assignments_db._engagement_signals_for_pool_items(
            session, [item.id for item in items]
        )
        rows: list[dict] = []
        for item in items:
            asset = item.media_asset
            candidate = _candidate_for_asset(session, asset)
            source = candidate.channel_name if candidate and candidate.channel_name else "-"
            if source_filter and source != source_filter:
                continue
            signal = signals[item.id]
            rows.append(
                {
                    "pool_item_id": item.id,
                    "niche": item.niche,
                    "clip_label": (
                        candidate.title
                        if candidate is not None and candidate.title
                        else _clip_label(asset, item.id)
                    ),
                    "source_label": source,
                    "created_at": _iso(item.created_at),
                    "thumbnail_url": candidate.thumbnail_url if candidate else None,
                    # Source reel URL (for opening the original) and the local
                    # original file when footage has already been downloaded; the
                    # bridge turns the latter into an in-app ``preview_url``.
                    "source_url": (
                        candidate.source_url
                        if candidate is not None and candidate.source_url
                        else (asset.canonical_source_url if asset is not None else None)
                    ),
                    "original_download_path": (
                        asset.original_download_path if asset is not None else None
                    ),
                    "fit_score": round(signal.fit_score, 6),
                    "source_er": signal.source_er,
                    "topic_tier": signal.topic_tier,
                    "suggested_action": signal.suggested_action,
                    "view_count": candidate.view_count if candidate else None,
                    "like_count": candidate.like_count if candidate else None,
                    "comment_count": candidate.comment_count if candidate else None,
                    "duration_seconds": candidate.duration_seconds if candidate else None,
                    "description": candidate.description if candidate else None,
                    "channel_name": candidate.channel_name if candidate else None,
                    "published_at": _iso(candidate.published_at if candidate else None),
                }
            )
        session.commit()
        return sorted(
            rows,
            key=lambda row: signals[row["pool_item_id"]].fit_score,
            reverse=True,
        )


def pool_item_preview(pool_item_id: int) -> dict:
    """Best preview source for a single pool item, for in-app review.

    Scraped IG thumbnail URLs are signed and expire within days, so for an older
    item the remote thumbnail no longer loads. When the footage has been
    downloaded, return its local file for a real video preview; otherwise fall
    back to the (possibly expired) thumbnail plus the source URL to open. The
    bridge turns ``local_path`` into an in-app ``preview_url``. Never changes the
    item's acceptance status.
    """
    with get_session() as session:
        item = session.get(PoolItem, pool_item_id)
        if item is None:
            raise PoolingError(f"No pool item with id {pool_item_id}.")
        asset = item.media_asset
        candidate = _candidate_for_asset(session, asset)
        local_path = None
        if asset is not None and asset.original_download_path:
            path = Path(asset.original_download_path)
            if path.exists():
                local_path = str(path)
        return {
            "pool_item_id": pool_item_id,
            "local_path": local_path,
            "thumbnail_url": candidate.thumbnail_url if candidate is not None else None,
            "source_url": (
                candidate.source_url
                if candidate is not None and candidate.source_url
                else (asset.canonical_source_url if asset is not None else None)
            ),
        }


def download_pool_item_for_review(
    pool_item_id: int, *, progress: Callable[[float, str], None] | None = None
) -> dict:
    """Download a pending pool item's footage so it can be previewed in review.

    Reuses on-disk footage when present; otherwise downloads the reel (serialized
    with the distribution downloader via the shared lock). The original is
    registered on the shared asset so it's reused later at distribution, but this
    does NOT change the item's acceptance status or create a Processing row — it
    is review-only. Returns the same shape as :func:`pool_item_preview`.
    """
    if progress:
        progress(0.1, "Downloading clip…")
    with _DOWNLOAD_LOCK:
        _ensure_pool_item_downloaded(pool_item_id)
    if progress:
        progress(1.0, "Ready.")
    return pool_item_preview(pool_item_id)


def approve_pool_items(pool_item_ids: list[int]) -> dict:
    """Approve pending pool items so they become eligible for distribution."""
    ids = [int(pool_item_id) for pool_item_id in (pool_item_ids or [])]
    if not ids:
        return {"approved": 0}
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as session:
        items = (
            session.query(PoolItem)
            .filter(
                PoolItem.id.in_(ids),
                PoolItem.acceptance_status == pools.POOL_STATUS_PENDING_REVIEW,
            )
            .all()
        )
        for item in items:
            item.acceptance_status = pools.POOL_STATUS_ACCEPTED
            item.accepted_at = now
            item.accepted_reason = "approved in review"
        session.commit()
        return {"approved": len(items)}


def reject_pool_items(pool_item_ids: list[int], reason: str) -> dict:
    """Reject pending pool items by reusing the reversible pool removal flow."""
    ids = [int(pool_item_id) for pool_item_id in (pool_item_ids or [])]
    if not ids:
        return {"rejected": 0}
    note = (reason or "").strip() or "rejected in review"
    rejected = 0
    with get_session() as session:
        for pool_item_id in ids:
            item = session.get(PoolItem, pool_item_id)
            if item is None or item.acceptance_status != pools.POOL_STATUS_PENDING_REVIEW:
                continue
            if pools.remove_pool_item(session, pool_item_id=pool_item_id, reason=note):
                rejected += 1
        session.commit()
    return {"rejected": rejected}


def list_pool_items(niche: str) -> list[dict]:
    """Accepted clips in a niche pool with their source and distribution state."""
    with get_session() as session:
        try:
            rows = pools.pool_contents(session, niche)
        except ValueError as exc:  # _validate_niche rejects unknown niches
            raise PoolingError(str(exc)) from exc
        return [
            {
                "pool_item_id": row.pool_item_id,
                "clip_label": row.clip_label,
                "source_label": row.source_label,
                "accepted_at": _iso(row.accepted_at),
                "distributed_to": list(row.distributed_to),
                "is_distributed": row.is_distributed,
            }
            for row in rows
        ]


def list_sources(niche: str) -> list[dict]:
    """Source summary rows for the selected niche pool."""
    with get_session() as session:
        try:
            rows = pools.pool_source_summary(session, niche)
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        return [
            {
                "source_label": row.source_label,
                "clip_count": row.clip_count,
                "newest_post_at": _iso(row.newest_post_at),
            }
            for row in rows
        ]


def list_source_clips(
    niche: str, source_label: str, include_removed: bool = False
) -> list[dict]:
    """Detailed clip rows for one source in a niche pool.

    ``original_download_path`` is the local original file; the bridge turns it
    into an in-app ``preview_url``. With ``include_removed`` the reversibly
    removed clips are included too (so they can be restored).
    """
    with get_session() as session:
        try:
            rows = pools.pool_clips_for_source(
                session, niche, source_label, include_removed=include_removed
            )
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        return [
            {
                "pool_item_id": row.pool_item_id,
                "shortcode": row.shortcode,
                "source_url": row.source_url,
                "caption": row.caption,
                "like_count": row.like_count,
                "published_at": _iso(row.published_at),
                "download_status": row.download_status,
                "acceptance_status": row.acceptance_status,
                "original_download_path": row.original_download_path,
                "distributed_to": list(row.distributed_to),
                "score": round(row.score, 3),
            }
            for row in rows
        ]


def remove_pool_item(pool_item_id: int, reason: str = "manual removal") -> dict:
    """Reversibly remove a clip from its niche pool so it stops distributing.

    Existing assignments are left untouched; restore the clip to undo it. The
    manual post-pool review gate (SOURCING_POOLING_PLAN.md §2).
    """
    with get_session() as session:
        if not pools.remove_pool_item(session, pool_item_id=pool_item_id, reason=reason):
            raise PoolingError(f"No pool item with id {pool_item_id}.")
        released = 0
        for assignment in session.query(Assignment).filter(
            Assignment.pool_item_id == pool_item_id,
            Assignment.status == assignments_db.ASSIGNMENT_STATUS_ASSIGNED,
        ).all():
            assignment.status = assignments_db.ASSIGNMENT_STATUS_REJECTED
            released += 1
        session.commit()
        return {
            "pool_item_id": pool_item_id,
            "acceptance_status": "removed",
            "released_assignments": released,
        }


def restore_pool_item(pool_item_id: int) -> dict:
    """Undo a removal — return the clip to the active pool."""
    with get_session() as session:
        if not pools.restore_pool_item(session, pool_item_id=pool_item_id):
            raise PoolingError(f"No pool item with id {pool_item_id}.")
        session.commit()
        return {"pool_item_id": pool_item_id, "acceptance_status": "accepted"}


def niche_accounts(niche: str) -> list[dict]:
    """Accounts in a niche (id + name), for the per-clip distribute picker."""
    with get_session() as session:
        try:
            ids = assignments_db.account_ids_for_niche(session, niche)
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        names = (
            {a.id: a.name for a in session.query(Account).filter(Account.id.in_(ids)).all()}
            if ids
            else {}
        )
        return [{"id": account_id, "name": names.get(account_id, f"#{account_id}")} for account_id in ids]


def distribute_clip(pool_item_id: int, account_ids: list[int]) -> dict:
    """Distribute one pooled clip to the chosen accounts (idempotent, same-niche).

    Returns how many new assignments were created (accounts that already had the
    clip are skipped).
    """
    cleaned = [int(account_id) for account_id in (account_ids or [])]
    _ensure_pool_item_downloaded(pool_item_id)
    with get_session() as session:
        try:
            created = assignments_db.assign_pool_item_to_accounts(
                session, pool_item_id=pool_item_id, account_ids=cleaned
            )
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        session.commit()
        return {"pool_item_id": pool_item_id, "assigned": len(created)}


# Health states that mean an account cannot publish AT ALL — no Instagram
# profile assigned, or a profile that has never been logged in. Distinct from
# merely aging (warn/stale) or cooling down, which a re-login fixes without
# touching the backlog.
_UNPUBLISHABLE_STATES = {HealthState.NOT_CONFIGURED, HealthState.NO_SESSION}


def _publishable_accounts(accounts: list[Account]) -> list[Account]:
    """Accounts that could ever publish: profile assigned + a recorded login.

    Reuses the network-free readiness signal behind the publishing dashboard
    (blank profile → NOT_CONFIGURED, no saved session → NO_SESSION) so
    "assignable" can't drift from "publishable". Cheap enough for the hourly
    top-up tick: it only reads local profile files, never the network.
    """
    pool = ProfilePool.load()
    ready: list[Account] = []
    for account in accounts:
        profile = (account.instagram_profile or "").strip()
        if not profile:
            continue
        if local_health(profile, account.name, pool=pool).state in _UNPUBLISHABLE_STATES:
            continue
        ready.append(account)
    return ready


def distribute_niche(
    niche: str,
    max_per_account: int | None = None,
    *,
    publish_ready_only: bool = False,
) -> dict:
    """Auto-distribute a niche's undistributed pool across its accounts.

    Ranks the unassigned pool by tier-weighted source ER plus recency, spreads
    the strongest clips one-per-account (volume-balanced, jittered within score
    tiers so accounts don't all get the same top clip), and tops each account up
    to its rolling ``distribute_daily_target`` (how many unposted clips to keep
    ready; module default when unset). An explicit ``max_per_account`` remains a
    uniform override. Idempotent: re-running only places clips for accounts still
    under target and never double-books a clip. The chosen clips' footage is
    fetched in the background, so this returns without waiting on downloads.

    With ``publish_ready_only=True`` (the automatic top-up path) accounts whose
    Instagram profile is missing or has never been logged in receive nothing,
    so pool clips are never locked behind a queue that can't post them. The
    manual Distribute flows keep the default ``False`` — pre-stocking an
    account before its first login stays an explicit user choice.

    Returns how many assignments were created, a per-account
    breakdown, and a ``reason`` string when assigned is 0 so the caller can show
    a specific message instead of a generic one:
    - ``"no_accounts"``  — no accounts are in this niche yet.
    - ``"no_ready_accounts"`` — accounts exist, but none are publish-ready
      (only with ``publish_ready_only=True``).
    - ``"all_at_cap"``   — every account already holds its target clips.
    - ``"pool_empty"``   — all accepted clips are already assigned.
    """
    eligible_pool_item_ids = _unassigned_pool_item_ids(niche)
    with get_session() as session:
        accounts = session.query(Account).filter(Account.niche == niche).all()
        if publish_ready_only:
            accounts = _publishable_accounts(accounts)
        targets = {
            account.id: (
                max_per_account
                if max_per_account is not None
                else (account.distribute_daily_target or DEFAULT_DISTRIBUTE_DAILY_TARGET)
            )
            for account in accounts
        }
        try:
            created = assignments_db.distribute_niche(
                session,
                niche,
                max_per_account=max_per_account,
                # targets_by_account doubles as the DB layer's account filter,
                # so it must be passed whenever the ready-only filter applies.
                targets_by_account=(
                    targets if (max_per_account is None or publish_ready_only) else None
                ),
                eligible_pool_item_ids=eligible_pool_item_ids,
            )
        except ValueError as exc:  # unknown niche from _validate_niche
            raise PoolingError(str(exc)) from exc

        created_pool_item_ids = [assignment.pool_item_id for assignment in created]

        reason: str | None = None
        if not created:
            acct_ids = assignments_db.account_ids_for_niche(session, niche)
            if not acct_ids:
                reason = "no_accounts"
            elif not targets:
                reason = "no_ready_accounts"
            else:
                existing = assignments_db.assignment_counts_by_account(session, niche)
                reason = (
                    "all_at_cap"
                    if all(existing.get(a, 0) >= target for a, target in targets.items())
                    else "pool_empty"
                )

        per_account = Counter(assignment.account_id for assignment in created)
        pinned_per_account = Counter(
            assignment.account_id
            for assignment in created
            if getattr(assignment, "distribution_reason", None) == "pinned"
        )
        names = (
            {
                account_id: name
                for account_id, name in session.query(Account.id, Account.name)
                .filter(Account.id.in_(list(per_account)))
                .all()
            }
            if per_account
            else {}
        )
        session.commit()
        unique_targets = set(targets.values())
        response_cap = (
            max_per_account
            if max_per_account is not None
            else next(iter(unique_targets))
            if len(unique_targets) == 1
            else DEFAULT_DISTRIBUTE_DAILY_TARGET
            if not unique_targets
            else None
        )
        result: dict = {
            "niche": niche,
            "assigned": len(created),
            "pinned": sum(pinned_per_account.values()),
            "max_per_account": response_cap,
            "accounts": [
                {
                    "account_id": account_id,
                    "account_name": names.get(account_id, f"#{account_id}"),
                    "count": count,
                    "pinned": pinned_per_account.get(account_id, 0),
                    "target": targets[account_id],
                }
                for account_id, count in sorted(per_account.items(), key=lambda kv: -kv[1])
            ],
            "download_failures": 0,
        }
        if reason is not None:
            result["reason"] = reason
    _start_background_download(created_pool_item_ids)
    return result


def auto_top_up(niches: tuple[str, ...] | None = None) -> list[dict]:
    """Maintain each niche's rolling distribution backlog; the loop entry point.

    For each niche with accounts: when any account holds fewer than its
    ``distribute_daily_target`` ready clips, run the normal ranked distribution
    (which tops every account back up to its target). As posts and rejects drain
    the backlog the next tick refills it, so each account keeps about its target
    of clips ready without ballooning. Niches where everyone is still topped up
    are left untouched, so this is cheap to call on a timer. Only publish-ready
    accounts count: an account with no usable browser profile must neither
    trigger a refill nor receive clips (it would lock pool clips behind a queue
    that can never post). Returns one distribute result per niche refilled.
    """
    results: list[dict] = []
    for niche in niches or NICHES:
        with get_session() as session:
            accounts = session.query(Account).filter(Account.niche == niche).all()
            if not accounts:
                continue
            accounts = _publishable_accounts(accounts)
            if not accounts:
                continue
            counts = assignments_db.assignment_counts_by_account(session, niche)
            below_target = any(
                counts.get(account.id, 0)
                < (account.distribute_daily_target or DEFAULT_DISTRIBUTE_DAILY_TARGET)
                for account in accounts
            )
        if below_target:
            results.append(distribute_niche(niche, publish_ready_only=True))
    return results


def release_unpublishable_assignments(
    niches: tuple[str, ...] | None = None, *, dry_run: bool = False
) -> dict:
    """Release pool clips locked on accounts that can never publish them.

    Remediation for assignments created before the top-up tick gained its
    publish-ready filter: accounts whose Instagram profile is missing or was
    never logged in got booked up to their full target, and because ANY
    assignment row marks a pool item as distributed
    (:func:`assignments_db.assigned_pool_item_ids`), those clips could never be
    redistributed to real accounts. For each such account this deletes its
    still-``assigned`` rows plus the pending_review DownloadItems created
    alongside them; ``posted`` / ``skipped_duplicate`` rows are history and stay
    untouched. With ``dry_run=True`` it only reports what would be released.
    """
    summary: list[dict] = []
    total_assignments = 0
    total_items = 0
    with get_session() as session:
        for niche in niches or NICHES:
            accounts = session.query(Account).filter(Account.niche == niche).all()
            ready_ids = {account.id for account in _publishable_accounts(accounts)}
            for account in accounts:
                if account.id in ready_ids:
                    continue
                assignments = (
                    session.query(Assignment)
                    .filter(
                        Assignment.account_id == account.id,
                        Assignment.status == assignments_db.ASSIGNMENT_STATUS_ASSIGNED,
                    )
                    .all()
                )
                if not assignments:
                    continue
                deleted_items = 0
                for assignment in assignments:
                    pool_item = session.get(PoolItem, assignment.pool_item_id)
                    asset = (
                        session.get(MediaAsset, pool_item.media_asset_id)
                        if pool_item is not None
                        else None
                    )
                    if asset is not None:
                        # Only the untouched pending_review rows that
                        # _create_pending_review_item made for this assignment;
                        # anything the user advanced past review is kept.
                        pending_items = (
                            session.query(DownloadItem)
                            .filter(
                                DownloadItem.account_id == account.id,
                                DownloadItem.source_url == asset.canonical_source_url,
                                DownloadItem.status == "pending_review",
                            )
                            .all()
                        )
                        deleted_items += len(pending_items)
                        if not dry_run:
                            for item in pending_items:
                                session.delete(item)
                    if not dry_run:
                        session.delete(assignment)
                summary.append(
                    {
                        "account_id": account.id,
                        "account_name": account.name,
                        "niche": niche,
                        "assignments": len(assignments),
                        "pending_items": deleted_items,
                    }
                )
                total_assignments += len(assignments)
                total_items += deleted_items
        if not dry_run:
            session.commit()
    return {
        "dry_run": dry_run,
        "released_assignments": total_assignments,
        "deleted_pending_items": total_items,
        "accounts": summary,
    }


def release_missing_media_assignments(
    niches: tuple[str, ...] | None = None, *, dry_run: bool = False
) -> dict:
    """Release untouched legacy assignments whose shared media is missing.

    Distribution now downloads a pool asset before assigning it, but assignments
    created before that guard can still expose a pending-review Processing row
    with no local file. Release only assignments with no progressed DownloadItem;
    completed/drafted work is preserved for manual recovery.
    """
    released: list[dict] = []
    total_assignments = 0
    total_items = 0
    with get_session() as session:
        query = session.query(Assignment).filter(
            Assignment.status == assignments_db.ASSIGNMENT_STATUS_ASSIGNED
        )
        if niches is not None:
            query = query.filter(Assignment.niche.in_(niches))
        for assignment in query.all():
            pool_item = session.get(PoolItem, assignment.pool_item_id)
            asset = (
                session.get(MediaAsset, pool_item.media_asset_id)
                if pool_item is not None
                else None
            )
            if asset is not None and _asset_is_downloaded(asset):
                continue

            source_url = asset.canonical_source_url if asset is not None else None
            matching_items = (
                session.query(DownloadItem)
                .filter(
                    DownloadItem.account_id == assignment.account_id,
                    DownloadItem.source_url == source_url,
                )
                .all()
                if source_url is not None
                else []
            )
            if any(item.status != "pending_review" for item in matching_items):
                continue
            pending_items = [
                item for item in matching_items if item.status == "pending_review"
            ]
            account = session.get(Account, assignment.account_id)
            released.append(
                {
                    "assignment_id": assignment.id,
                    "account_id": assignment.account_id,
                    "account_name": account.name if account is not None else f"#{assignment.account_id}",
                    "niche": assignment.niche,
                    "pool_item_id": assignment.pool_item_id,
                    "shortcode": asset.source_shortcode if asset is not None else None,
                    "pending_items": len(pending_items),
                }
            )
            total_assignments += 1
            total_items += len(pending_items)
            if not dry_run:
                for item in pending_items:
                    session.delete(item)
                session.delete(assignment)
        if not dry_run:
            session.commit()
    return {
        "dry_run": dry_run,
        "released_assignments": total_assignments,
        "deleted_pending_items": total_items,
        "assignments": released,
    }


def repair_pending_review_media_links(*, dry_run: bool = False) -> dict:
    """Backfill legacy pending-review rows from their downloaded shared asset."""
    repaired: list[dict] = []
    with get_session() as session:
        pending_items = session.query(DownloadItem).filter(
            DownloadItem.status == "pending_review"
        ).all()
        for item in pending_items:
            if item.file_path and Path(item.file_path).exists():
                continue
            asset = (
                session.query(MediaAsset)
                .filter(MediaAsset.source_shortcode == item.video_id)
                .first()
                if item.video_id
                else None
            )
            if asset is None or not _asset_is_downloaded(asset):
                continue
            repaired.append(
                {
                    "item_id": item.id,
                    "account_id": item.account_id,
                    "shortcode": asset.source_shortcode,
                    "file_path": asset.original_download_path,
                }
            )
            if not dry_run:
                item.file_path = asset.original_download_path
        if not dry_run:
            session.commit()
    return {"dry_run": dry_run, "repaired_items": len(repaired), "items": repaired}


def distribute_niche_explicit(niche: str, targets_by_account: dict[int, int]) -> dict:
    """Add explicit clip counts to selected accounts, ignoring cadence targets.

    The chosen clips' footage is fetched in the background, so this returns
    without waiting on downloads.
    """
    requested = {
        int(account_id): max(0, int(target))
        for account_id, target in (targets_by_account or {}).items()
    }
    eligible_pool_item_ids = _unassigned_pool_item_ids(niche)
    with get_session() as session:
        existing = assignments_db.assignment_counts_by_account(session, niche)
        total_targets = {
            account_id: existing.get(account_id, 0) + count
            for account_id, count in requested.items()
        }
        try:
            created = assignments_db.distribute_niche(
                session,
                niche,
                targets_by_account=total_targets,
                eligible_pool_item_ids=eligible_pool_item_ids,
            )
        except ValueError as exc:
            raise PoolingError(str(exc)) from exc
        created_pool_item_ids = [row.pool_item_id for row in created]
        assigned = Counter(row.account_id for row in created)
        pinned = Counter(
            row.account_id
            for row in created
            if getattr(row, "distribution_reason", None) == "pinned"
        )
        names = {
            account.id: account.name
            for account in session.query(Account).filter(Account.id.in_(list(requested))).all()
        }
        session.commit()
        result = {
            "niche": niche,
            "assigned": len(created),
            "pinned": sum(pinned.values()),
            "max_per_account": None,
            "download_failures": 0,
            "accounts": [
                {
                    "account_id": account_id,
                    "account_name": names.get(account_id, f"#{account_id}"),
                    "count": assigned.get(account_id, 0),
                    "pinned": pinned.get(account_id, 0),
                    "target": target,
                }
                for account_id, target in requested.items()
            ],
        }
    _start_background_download(created_pool_item_ids)
    return result
