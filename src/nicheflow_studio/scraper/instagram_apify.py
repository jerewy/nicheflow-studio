"""Apify-based Instagram metadata source.

This is an alternative to the Playwright/instaloader path in
``scraper.instagram``. It calls the public ``apify/instagram-scraper``
Actor on Apify's infrastructure to fetch post/reel metadata, so your own
Instagram accounts never log in or send automated traffic.

Why this exists
---------------
Logging Playwright into a publisher account (e.g. ``pastmomentsdaily``) and
then scraping at scale gets the account flagged by Instagram's
automated-behavior heuristics. Apify scrapes from their own infrastructure
under their own ToS posture — your accounts stay clean.

Auth
----
Set ``APIFY_TOKEN`` in ``.env``. Get one from
https://console.apify.com/account/integrations.

Cost (Free plan, as of 2026-05)
-------------------------------
- $5 platform credit / month, resets on billing date
- ``apify/instagram-scraper`` charges ~$2.70 per 1,000 results on Free plan
- ~1,852 free results / month
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Iterable

from nicheflow_studio.core.apify_usage import record_apify_results
from nicheflow_studio.scraper.youtube import (
    DiscoveryWeights,
    ScrapedVideoCandidate,
    rank_candidate,
)

logger = logging.getLogger(__name__)

APIFY_ACTOR_ID = "apify/instagram-scraper"
APIFY_TOKEN_ENV = "APIFY_TOKEN"
_INSTAGRAM_DISCOVERY_WEIGHTS = DiscoveryWeights(
    views=10,
    likes=25,
    comments=30,
    recency=25,
    duration_fit=10,
    keyword_match=0,
)


class ApifyConfigError(RuntimeError):
    """Raised when Apify is not configured (missing token, missing dep)."""


class ApifyScrapeError(RuntimeError):
    """Raised when the Apify run fails or returns no usable data."""


def _require_token() -> str:
    token = os.environ.get(APIFY_TOKEN_ENV, "").strip()
    if not token:
        raise ApifyConfigError(
            f"{APIFY_TOKEN_ENV} not set. Add it to .env "
            f"(get one from https://console.apify.com/account/integrations)."
        )
    return token


def _require_client() -> object:
    try:
        from apify_client import ApifyClient  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover - exercised at runtime
        raise ApifyConfigError(
            "apify-client is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return ApifyClient(_require_token())


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _parse_timestamp(value: object) -> dt.datetime | None:
    """Parse Apify's ISO-8601 timestamp string into an aware datetime."""
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Apify returns "2023-03-22T12:34:56.000Z" - normalize Z to +00:00.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _candidate_from_apify_item(item: dict) -> ScrapedVideoCandidate | None:
    """Map one Apify ``instagram-scraper`` result row to a candidate.

    Returns ``None`` if the row does not have enough data to be useful
    (no short code and no URL).
    """
    short_code = item.get("shortCode") or item.get("id")
    source_url = item.get("url") or item.get("inputUrl")

    if not isinstance(short_code, str) or not short_code.strip():
        if not isinstance(source_url, str) or not source_url.strip():
            return None
        # Fall back to URL tail as id.
        short_code = source_url.rstrip("/").rsplit("/", 1)[-1]

    if not isinstance(source_url, str) or not source_url.strip():
        source_url = f"https://www.instagram.com/p/{short_code}/"

    caption = item.get("caption")
    description = caption if isinstance(caption, str) else None
    title = description.splitlines()[0][:120] if description else None

    owner = item.get("ownerUsername") or item.get("ownerFullName")
    published_at = _parse_timestamp(item.get("timestamp"))
    thumbnail = item.get("displayUrl") or item.get("thumbnailUrl")

    candidate = ScrapedVideoCandidate(
        scrape_source_url=source_url,
        source_url=source_url,
        extractor="apify:instagram",
        video_id=short_code,
        title=title,
        channel_name=owner if isinstance(owner, str) else None,
        published_at=published_at,
        description=description,
        view_count=_optional_int(item.get("videoViewCount") or item.get("videoPlayCount")),
        like_count=_optional_int(item.get("likesCount")),
        comment_count=_optional_int(item.get("commentsCount")),
        duration_seconds=_optional_int(item.get("videoDuration")),
        thumbnail_url=thumbnail if isinstance(thumbnail, str) else None,
        discovery_query="apify instagram batch",
        match_reason="apify metadata",
    )
    return rank_candidate(
        candidate,
        keywords=[],
        weights=_INSTAGRAM_DISCOVERY_WEIGHTS,
        max_age_days=30,
    )


def scrape_instagram_source_apify(
    *,
    source_url: str,
    max_items: int,
    max_age_days: int | None = None,
    since: dt.datetime | None = None,
    timeout_secs: int = 600,
) -> list[ScrapedVideoCandidate]:
    """Scrape an Instagram **profile or hashtag** source via Apify.

    This is the Apify-backed replacement for
    :func:`nicheflow_studio.scraper.instagram.scrape_instagram_source`.

    Cost control
    ------------
    Apify charges per returned result, so two things keep the bill down:

    1. ``max_items`` caps how many posts the Actor returns. We pass it as
       ``resultsLimit`` to Apify, which is a hard ceiling.
    2. ``since`` tells Apify to skip posts older than that timestamp via
       ``onlyPostsNewerThan``. Pass the source's ``last_scraped_at`` (with a
       small safety buffer) on subsequent runs — the Actor short-circuits
       early and you only pay for genuinely new posts.

    Parameters
    ----------
    source_url:
        IG profile URL (``https://www.instagram.com/<user>/``) or hashtag URL
        (``https://www.instagram.com/explore/tags/<tag>/``). Apify's
        instagram-scraper accepts both via ``directUrls``.
    max_items:
        Hard ceiling on the number of posts returned. Mapped to ``resultsLimit``.
    max_age_days:
        If set, candidates older than this many days are filtered out
        client-side after Apify returns them. This is a belt-and-braces filter
        in addition to ``since``.
    since:
        If set, sent to Apify as ``onlyPostsNewerThan`` so the Actor skips
        older posts at the source. Strongly recommended on re-scrapes.

    Raises
    ------
    ApifyConfigError, ApifyScrapeError
        Same semantics as :func:`scrape_instagram_urls_apify`.
    """
    if not isinstance(source_url, str) or not source_url.strip():
        raise ValueError("source_url is required.")
    if max_items < 1:
        raise ValueError("Max items must be at least 1.")
    if max_age_days is not None and max_age_days < 1:
        raise ValueError("Max age days must be at least 1.")

    client = _require_client()

    run_input: dict[str, object] = {
        "directUrls": [source_url],
        "resultsType": "posts",
        "resultsLimit": max_items,
        "addParentData": False,
    }
    if since is not None:
        # Apify accepts an ISO date string for onlyPostsNewerThan.
        run_input["onlyPostsNewerThan"] = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")

    logger.info(
        "Calling Apify Actor %s for source %s (limit=%d, since=%s)",
        APIFY_ACTOR_ID,
        source_url,
        max_items,
        run_input.get("onlyPostsNewerThan", "none"),
    )

    try:
        run = client.actor(APIFY_ACTOR_ID).call(  # type: ignore[attr-defined]
            run_input=run_input,
            timeout_secs=timeout_secs,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error to callers
        raise ApifyScrapeError(f"Apify Actor call failed: {exc}") from exc

    if not isinstance(run, dict) or not run.get("defaultDatasetId"):
        raise ApifyScrapeError(f"Apify Actor returned no dataset: {run!r}")

    dataset_id = run["defaultDatasetId"]
    items = list(client.dataset(dataset_id).iterate_items())  # type: ignore[attr-defined]
    # Apify bills per dataset row, including rows the filters below drop —
    # record here, where the billed count is known, so the free-tier warning
    # stays honest no matter what callers do with the candidates.
    record_apify_results(len(items))

    cutoff: dt.datetime | None = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    candidates: list[ScrapedVideoCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = _candidate_from_apify_item(item)
        if candidate is None:
            continue
        # Belt-and-braces age filter in case Apify returned items older than
        # ``since`` (e.g. timezone fuzz at the day boundary).
        if cutoff is not None:
            if candidate.published_at is None or candidate.published_at < cutoff:
                continue
        # Rewrite scrape_source_url so downstream dedup keys by the source URL
        # the user actually configured, not the per-post URL Apify returned.
        candidates.append(
            ScrapedVideoCandidate(
                scrape_source_url=source_url,
                source_url=candidate.source_url,
                extractor=candidate.extractor,
                video_id=candidate.video_id,
                title=candidate.title,
                channel_name=candidate.channel_name,
                published_at=candidate.published_at,
                description=candidate.description,
                view_count=candidate.view_count,
                like_count=candidate.like_count,
                comment_count=candidate.comment_count,
                duration_seconds=candidate.duration_seconds,
                thumbnail_url=candidate.thumbnail_url,
                discovery_query=candidate.discovery_query,
                match_reason=candidate.match_reason,
                ranking_score=candidate.ranking_score,
            )
        )
        if len(candidates) >= max_items:
            break

    logger.info(
        "Apify source scrape returned %d candidate(s) from %d raw row(s)",
        len(candidates),
        len(items),
    )
    return candidates


def scrape_instagram_urls_apify(
    urls: Iterable[str],
    *,
    results_limit: int | None = None,
    timeout_secs: int = 300,
) -> list[ScrapedVideoCandidate]:
    """Fetch metadata for a batch of Instagram post/reel URLs via Apify.

    Parameters
    ----------
    urls:
        Direct Instagram post / reel URLs. Profile / hashtag URLs also work
        but return many more rows (each counted against your Apify quota).
    results_limit:
        Hard cap on rows the Actor will return. Defaults to ``len(urls)`` so
        per-post URLs map 1:1 and your Apify spend is predictable.
    timeout_secs:
        How long to wait for the Actor run to finish. Apify Actor runs
        typically complete in 5-30s for small batches.

    Raises
    ------
    ApifyConfigError
        If ``APIFY_TOKEN`` is missing or ``apify-client`` is not installed.
    ApifyScrapeError
        If the Actor run fails or returns no rows.
    """
    url_list = [u for u in urls if isinstance(u, str) and u.strip()]
    if not url_list:
        return []

    limit = results_limit if results_limit is not None else len(url_list)
    client = _require_client()

    run_input = {
        "directUrls": url_list,
        "resultsType": "details",
        "resultsLimit": limit,
        "addParentData": False,
    }

    logger.info(
        "Calling Apify Actor %s with %d URL(s), limit=%d",
        APIFY_ACTOR_ID,
        len(url_list),
        limit,
    )

    try:
        run = client.actor(APIFY_ACTOR_ID).call(  # type: ignore[attr-defined]
            run_input=run_input,
            timeout_secs=timeout_secs,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error to callers
        raise ApifyScrapeError(f"Apify Actor call failed: {exc}") from exc

    if not isinstance(run, dict) or not run.get("defaultDatasetId"):
        raise ApifyScrapeError(f"Apify Actor returned no dataset: {run!r}")

    dataset_id = run["defaultDatasetId"]
    items = list(client.dataset(dataset_id).iterate_items())  # type: ignore[attr-defined]
    # Apify bills per dataset row, including unusable/error rows and rows whose
    # parse fails below — record here, where the billed count is known, even
    # when this function goes on to raise.
    record_apify_results(len(items))

    candidates: list[ScrapedVideoCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = _candidate_from_apify_item(item)
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        raise ApifyScrapeError(
            f"Apify returned {len(items)} row(s) but none had usable metadata."
        )

    logger.info("Apify returned %d candidate(s) from %d URL(s)", len(candidates), len(url_list))
    return candidates
