"""Single-Reel capture into a shared NicheFlow pool."""

from __future__ import annotations

from dataclasses import asdict
from urllib.parse import urlparse

from nicheflow_studio.core.apify_usage import (
    monthly_apify_usage,
    record_apify_results,
)  # noqa: E501
from nicheflow_studio.db.media_library import find_media_asset
from nicheflow_studio.db.models import Account, PoolItem
from nicheflow_studio.db.pool_intake import (
    ReelMetadata,
    add_reel_to_pool,
)
from nicheflow_studio.db.pools import (
    POOL_STATUS_ACCEPTED,
    POOL_STATUS_PENDING_REVIEW,
    VALID_NICHES,
)  # noqa: E501
from nicheflow_studio.db.session import get_session
from nicheflow_studio.downloader.instagram import instagram_shortcode_from_url
from nicheflow_studio.scraper.instagram_apify import scrape_instagram_urls_apify  # noqa: E501
from nicheflow_studio.scraper.youtube import ScrapedVideoCandidate


class PoolCaptureError(RuntimeError):
    """Raised when a browser capture cannot be added to a pool."""


def _captured_video_count(session, niche: str) -> int:
    """Videos captured into ``niche``'s pool, counting both awaiting-review and
    already-approved items. This is the extension popup's "did my capture land"
    confirmation number, so it must not go quiet while a clip sits in the desktop
    app's pending-review queue — unlike :func:`pool_size`, which defaults to
    accepted-only for the distribution-facing counts."""
    return (
        session.query(PoolItem)
        .filter(
            PoolItem.niche == niche,
            PoolItem.acceptance_status.in_([POOL_STATUS_ACCEPTED, POOL_STATUS_PENDING_REVIEW]),
        )
        .count()
    )


def capture_dashboard() -> dict:
    """Return compact pool and Apify usage stats for the extension popup."""
    with get_session() as session:
        pools = {}
        for niche in sorted(VALID_NICHES):
            accounts = (
                session.query(Account)
                .filter(Account.niche == niche)
                .order_by(Account.name.asc(), Account.id.asc())
                .all()
            )
            pools[niche] = {
                "video_count": _captured_video_count(session, niche),
                "accounts": [{"id": account.id, "name": account.name} for account in accounts],
            }
    return {"pools": pools, "apify_usage": monthly_apify_usage()}


def normalize_instagram_media_url(url: str) -> str:
    """Return one canonical Instagram media URL or raise a user-facing error."""  # noqa: E501
    shortcode = instagram_shortcode_from_url(url)
    if shortcode is None:
        raise PoolCaptureError("Open an Instagram Reel or post before capturing.")  # noqa: E501

    path = urlparse(url.strip()).path
    parts = [part for part in path.split("/") if part]
    kind = parts[0].lower() if parts else "p"
    if kind == "reels":
        kind = "reel"
    if kind not in {"p", "reel", "tv"}:
        kind = "p"
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def _metadata_from_candidate(candidate: ScrapedVideoCandidate) -> ReelMetadata:
    duration = candidate.duration_seconds
    return ReelMetadata(
        source_url=candidate.source_url,
        shortcode=candidate.video_id,
        channel_name=candidate.channel_name,
        title=candidate.title,
        description=candidate.description,
        published_at=candidate.published_at,
        view_count=candidate.view_count,
        like_count=candidate.like_count,
        comment_count=candidate.comment_count,
        duration_seconds=int(duration) if duration is not None else None,
        thumbnail_url=candidate.thumbnail_url,
    )


def _normalize_niche(niche: str) -> str:
    niche = (niche or "").strip().lower()
    if niche not in VALID_NICHES:
        raise PoolCaptureError(
            f"Pool must be one of: {', '.join(sorted(VALID_NICHES))}."
        )  # noqa: E501
    return niche


def _existing_pool_result(normalized_url: str, shortcode: str | None) -> dict | None:  # noqa: E501
    with get_session() as session:
        asset = find_media_asset(
            session,
            source_url=normalized_url,
            shortcode=shortcode,
        )
        if asset is None:
            return None
        existing = (
            session.query(PoolItem)
            .filter(
                PoolItem.media_asset_id == asset.id,
                PoolItem.acceptance_status.in_(
                    [POOL_STATUS_ACCEPTED, POOL_STATUS_PENDING_REVIEW]
                ),
            )
            .first()
        )
        if existing is None:
            return None
        return {
            "status": "duplicate",
            "niche": existing.niche,
            "shortcode": shortcode,
            "message": f"Already in the {existing.niche.title()} pool - skipped.",  # noqa: E501
            "source_url": normalized_url,
            "channel_name": None,
            "title": None,
        }


def capture_instagram_reels_to_pool(items: list[dict]) -> dict:
    """Batch-fetch metadata for queued Reels and add them to shared pools."""
    if not items:
        raise PoolCaptureError("The capture queue is empty.")
    if len(items) > 50:
        raise PoolCaptureError("Process at most 50 queued Reels per batch.")

    results: list[dict] = []
    pending: list[dict] = []
    seen_shortcodes: set[str] = set()
    for item in items:
        url = item.get("url") if isinstance(item, dict) else None
        niche = item.get("niche", "history") if isinstance(item, dict) else "history"  # noqa: E501
        pinned_account_id = item.get("pinned_account_id") if isinstance(item, dict) else None
        if not isinstance(url, str):
            results.append({"status": "failed", "message": "Queued item has no URL."})  # noqa: E501
            continue
        try:
            normalized_url = normalize_instagram_media_url(url)
            normalized_niche = _normalize_niche(str(niche))
            normalized_pin = int(pinned_account_id) if pinned_account_id not in (None, "") else None
        except (PoolCaptureError, TypeError, ValueError) as exc:
            results.append(
                {"status": "failed", "source_url": url, "message": str(exc)}
            )  # noqa: E501
            continue

        shortcode = instagram_shortcode_from_url(normalized_url)
        if shortcode in seen_shortcodes:
            results.append(
                {
                    "status": "duplicate",
                    "niche": normalized_niche,
                    "shortcode": shortcode,
                    "source_url": normalized_url,
                    "message": "Repeated in this batch - skipped.",
                }
            )
            continue
        if shortcode:
            seen_shortcodes.add(shortcode)

        existing = _existing_pool_result(normalized_url, shortcode)
        if existing is not None:
            results.append(existing)
            continue
        pending.append(
            {
                "source_url": normalized_url,
                "shortcode": shortcode,
                "niche": normalized_niche,
                "pinned_account_id": normalized_pin,
            }
        )

    candidates = (
        scrape_instagram_urls_apify(
            [item["source_url"] for item in pending],
            results_limit=len(pending),
        )
        if pending
        else []
    )
    record_apify_results(len(candidates))
    by_shortcode = {
        candidate.video_id: candidate  # noqa: E501
        for candidate in candidates
        if candidate.video_id
    }  # noqa: E501
    with get_session() as session:
        for item in pending:
            candidate = by_shortcode.get(item["shortcode"])
            if candidate is None:
                results.append(
                    {
                        "status": "failed",
                        "source_url": item["source_url"],
                        "shortcode": item["shortcode"],
                        "message": "Apify returned no metadata for this Reel.",
                    }
                )
                continue
            metadata = _metadata_from_candidate(candidate)
            result = add_reel_to_pool(
                session,
                niche=item["niche"],
                metadata=metadata,
                pinned_account_id=item["pinned_account_id"],
            )
            payload = asdict(result)
            payload["source_url"] = metadata.source_url
            payload["channel_name"] = metadata.channel_name
            payload["title"] = metadata.title
            results.append(payload)
        session.commit()

    summary = {
        "queued": len(items),
        "added": sum(result["status"] == "added" for result in results),
        "duplicates": sum(result["status"] == "duplicate" for result in results),  # noqa: E501
        "failed": sum(result["status"] == "failed" for result in results),
        "apify_results": len(candidates),
    }
    return {"summary": summary, "results": results, "dashboard": capture_dashboard()}  # noqa: E501


def capture_instagram_reel_to_pool(
    url: str, *, niche: str = "history", pinned_account_id: int | None = None
) -> dict:
    """Fetch one Reel's metadata and add it to a shared niche pool."""
    batch = capture_instagram_reels_to_pool(
        [{"url": url, "niche": niche, "pinned_account_id": pinned_account_id}]
    )
    result = batch["results"][0]
    if result["status"] == "failed":
        raise PoolCaptureError(result["message"])
    result["dashboard"] = batch["dashboard"]
    return result
