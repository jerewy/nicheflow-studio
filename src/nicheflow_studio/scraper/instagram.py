from __future__ import annotations

import datetime as dt
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from nicheflow_studio.core.instagram_session import (
    DEFAULT_PROFILE_NAME,
    instagram_yt_dlp_cookie_status,
    load_latest_instagram_session,
    load_playwright_cookies_into_instaloader,
)
from nicheflow_studio.core.instagram_profile_pool import ProfilePool
from nicheflow_studio.downloader.instagram import instagram_shortcode_from_url
from nicheflow_studio.scraper.youtube import DiscoveryWeights, ScrapedVideoCandidate, rank_candidate
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


@dataclass
class InstagramScrapeStats:
    input_urls: int = 0
    extraction_limit: int = 0
    attempted: int = 0
    extracted: int = 0
    skipped_old: int = 0
    failed_no_video: int = 0
    failed_unavailable: int = 0
    failed_rate_limited: int = 0
    failed_other: int = 0


class InstagramRateLimitError(Exception):
    """Raised when Instagram signals rate-limit or login-required during metadata extraction.

    ``partial_candidates`` contains any results collected before the limit was hit.
    """

    def __init__(
        self,
        message: str,
        partial_candidates: list,
        stats: InstagramScrapeStats | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_candidates = partial_candidates
        self.stats = stats or InstagramScrapeStats()


try:
    import instaloader
except ImportError:  # pragma: no cover - exercised through runtime error path
    instaloader = None


_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_HASHTAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_INSTAGRAM_DISCOVERY_WEIGHTS = DiscoveryWeights(
    views=10,
    likes=25,
    comments=30,
    recency=25,
    duration_fit=10,
    keyword_match=0,
)


def _require_instaloader():
    if instaloader is None:
        raise RuntimeError("Instaloader is not installed. Run pip install -r requirements.txt.")
    return instaloader


def _make_loader() -> object:
    instaloader_module = _require_instaloader()
    loader = instaloader_module.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        max_connection_attempts=3,
        request_timeout=30.0,
        quiet=True,
        sanitize_paths=True,
    )
    load_latest_instagram_session(loader)
    return loader


def _normalize_tag(value: str) -> str | None:
    tag = value.strip().lstrip("#").strip("/")
    if not tag or _HASHTAG_RE.fullmatch(tag) is None:
        return None
    return tag


def normalize_instagram_source_url(value: str) -> tuple[str | None, str | None]:
    raw_value = value.strip()
    if not raw_value:
        return (None, "Enter an Instagram hashtag, keyword, or profile URL.")

    tag = _normalize_tag(raw_value)
    if tag is not None and not raw_value.startswith(("http://", "https://")):
        return (f"https://www.instagram.com/explore/tags/{tag}/", None)

    parsed = urlparse(raw_value)
    if parsed.scheme not in {"http", "https"}:
        return (None, "Enter an Instagram hashtag, keyword, or profile URL.")

    host = parsed.netloc.lower()
    if host not in _INSTAGRAM_HOSTS:
        return (None, "Only Instagram source URLs are supported for Instagram intake.")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "explore" and parts[1] == "tags":
        if len(parts) < 3:
            return (None, "Enter a complete Instagram hashtag URL.")
        tag = _normalize_tag(parts[2])
        if tag is None:
            return (None, "Enter a valid Instagram hashtag.")
        return (f"https://www.instagram.com/explore/tags/{tag}/", None)

    if len(parts) >= 1 and parts[0] not in {"p", "reel", "tv", "stories", "explore"}:
        username = parts[0].strip()
        if _normalize_tag(username) is None:
            return (None, "Enter a valid Instagram profile URL.")
        return (f"https://www.instagram.com/{username}/", None)

    return (None, "Use an Instagram hashtag, keyword, or profile URL for source intake.")


def validate_instagram_source_url(value: str) -> str | None:
    _normalized, validation_error = normalize_instagram_source_url(value)
    return validation_error


def infer_instagram_source_type(source_url: str) -> str:
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "explore" and parts[1] == "tags":
        return "instagram_hashtag"
    return "instagram_profile"


def instagram_source_label(source_url: str) -> str:
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "explore" and parts[1] == "tags":
        return f"#{parts[2]}"
    if parts:
        return f"@{parts[0]}"
    return source_url


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _post_int_attr(post: object, attr_name: str) -> int | None:
    if not hasattr(post, attr_name):
        return None
    return _optional_int(getattr(post, attr_name))


def _parse_timestamp(value: object) -> dt.datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    return None


def _parse_upload_date(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.strptime(value.strip(), "%Y%m%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc)


def _thumbnail_from_info(info: dict[str, object]) -> str | None:
    thumbnail = info.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail.startswith(("http://", "https://")):
        return thumbnail

    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        for candidate in reversed(thumbnails):
            if not isinstance(candidate, dict):
                continue
            url = candidate.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return None


def _candidate_from_yt_dlp_info(
    scrape_source_url: str,
    info: dict[str, object],
) -> ScrapedVideoCandidate | None:
    video_id = info.get("id")
    if not isinstance(video_id, str) or not video_id.strip():
        video_id = instagram_shortcode_from_url(scrape_source_url)
    if video_id is None:
        return None

    source_url = info.get("webpage_url")
    if not isinstance(source_url, str) or not source_url.startswith(("http://", "https://")):
        source_url = f"https://www.instagram.com/p/{video_id}/"

    title = info.get("title")
    description = info.get("description")
    channel_name = info.get("channel") or info.get("uploader")
    published_at = _parse_timestamp(info.get("timestamp")) or _parse_upload_date(
        info.get("upload_date")
    )

    return ScrapedVideoCandidate(
        scrape_source_url=scrape_source_url,
        source_url=source_url,
        extractor="instagram",
        video_id=video_id,
        title=title if isinstance(title, str) else None,
        channel_name=channel_name if isinstance(channel_name, str) else None,
        published_at=published_at,
        description=description if isinstance(description, str) else None,
        view_count=_optional_int(info.get("view_count")),
        like_count=_optional_int(info.get("like_count")),
        comment_count=_optional_int(info.get("comment_count")),
        duration_seconds=_optional_int(info.get("duration")),
        thumbnail_url=_thumbnail_from_info(info),
        discovery_query="instagram url batch",
        match_reason="instagram metadata",
    )


def _is_rate_limit_error(exc: DownloadError) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "rate-limit reached",
            "401",
            "429",
            "please wait a few minutes",
            "login required",
            "not logged in",
            "login_required",
        )
    )


def _is_no_video_error(exc: DownloadError) -> bool:
    msg = str(exc).lower()
    return "no video" in msg or "no video formats" in msg


def _is_unavailable_error(exc: DownloadError) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "not available",
            "isn't available",
            "private",
            "can't be seen",
            "cannot be seen",
            "requested content is not available",
        )
    )


def _build_yt_dlp_opts() -> dict:
    opts: dict = {
        "ignoreerrors": False,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    status = instagram_yt_dlp_cookie_status()
    if status.cookiefile is not None:
        opts["cookiefile"] = status.cookiefile
    return opts


def scrape_instagram_url(url: str) -> ScrapedVideoCandidate:
    if instagram_shortcode_from_url(url) is None:
        raise ValueError("Use an Instagram Reel or post URL.")

    ydl_opts = _build_yt_dlp_opts()
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("Instagram metadata extraction returned no data.")

    candidate = _candidate_from_yt_dlp_info(url, info)
    if candidate is None:
        raise RuntimeError("Instagram metadata extraction returned no media id.")
    return rank_candidate(
        candidate,
        keywords=[],
        weights=_INSTAGRAM_DISCOVERY_WEIGHTS,
        max_age_days=30,
    )


def scrape_instagram_urls(
    urls: list[str],
    *,
    max_items: int | None = None,
    max_age_days: int | None = None,
) -> list[ScrapedVideoCandidate]:
    candidates, _stats = scrape_instagram_urls_with_stats(
        urls,
        max_items=max_items,
        max_age_days=max_age_days,
    )
    return candidates


def scrape_instagram_urls_with_stats(
    urls: list[str],
    *,
    max_items: int | None = None,
    max_age_days: int | None = None,
) -> tuple[list[ScrapedVideoCandidate], InstagramScrapeStats]:
    if max_items is not None and max_items < 1:
        raise ValueError("Max items must be at least 1.")
    if max_age_days is not None and max_age_days < 1:
        raise ValueError("Max age days must be at least 1.")

    cutoff = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    normalized_urls = [url.strip() for url in urls if url.strip()]
    urls_to_attempt = normalized_urls[:max_items] if max_items is not None else normalized_urls
    stats = InstagramScrapeStats(
        input_urls=len(normalized_urls),
        extraction_limit=len(urls_to_attempt),
    )
    candidates: list[ScrapedVideoCandidate] = []
    for index, normalized_url in enumerate(urls_to_attempt):
        if index > 0:
            time.sleep(random.uniform(2.0, 5.0))
        stats.attempted += 1
        try:
            candidate = scrape_instagram_url(normalized_url)
        except DownloadError as exc:
            if _is_rate_limit_error(exc):
                stats.failed_rate_limited += 1
                raise InstagramRateLimitError(
                    "Instagram rate limit hit. Wait 15-30 min before running again.",
                    candidates,
                    stats,
                ) from exc
            if _is_no_video_error(exc):
                stats.failed_no_video += 1
            elif _is_unavailable_error(exc):
                stats.failed_unavailable += 1
            else:
                stats.failed_other += 1
            continue
        if cutoff is not None:
            if candidate.published_at is None or candidate.published_at < cutoff:
                stats.skipped_old += 1
                continue
        candidates.append(candidate)
        stats.extracted += 1

    return candidates, stats


def _candidate_from_post(source_url: str, post: object) -> ScrapedVideoCandidate | None:
    if not getattr(post, "is_video", False):
        return None

    shortcode = getattr(post, "shortcode", None)
    if not isinstance(shortcode, str) or not shortcode:
        return None

    caption = getattr(post, "caption", None)
    if not isinstance(caption, str):
        caption = None

    date_utc = getattr(post, "date_utc", None)
    if isinstance(date_utc, dt.datetime) and date_utc.tzinfo is None:
        date_utc = date_utc.replace(tzinfo=dt.timezone.utc)

    owner_username = getattr(post, "owner_username", None)
    title = caption.splitlines()[0][:120] if caption else f"Instagram Reel {shortcode}"
    view_count = _post_int_attr(post, "video_view_count") or _post_int_attr(
        post, "video_play_count"
    )
    like_count = _post_int_attr(post, "likes")
    comment_count = _post_int_attr(post, "comments")
    duration_seconds = _post_int_attr(post, "video_duration")
    thumbnail_url = getattr(post, "url", None)

    return ScrapedVideoCandidate(
        scrape_source_url=source_url,
        source_url=f"https://www.instagram.com/reel/{shortcode}/",
        extractor="instagram",
        video_id=shortcode,
        title=title,
        channel_name=owner_username if isinstance(owner_username, str) else None,
        published_at=date_utc if isinstance(date_utc, dt.datetime) else None,
        description=caption,
        view_count=view_count,
        like_count=like_count,
        comment_count=comment_count,
        duration_seconds=duration_seconds,
        thumbnail_url=thumbnail_url if isinstance(thumbnail_url, str) else None,
        discovery_query=instagram_source_label(source_url),
        match_reason="instagram video",
    )


def _posts_for_source(loader: object, source_url: str):
    instaloader_module = _require_instaloader()
    parsed = urlparse(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "explore" and parts[1] == "tags":
        hashtag = instaloader_module.Hashtag.from_name(loader.context, parts[2])
        return hashtag.get_posts()

    if parts:
        profile = instaloader_module.Profile.from_username(loader.context, parts[0])
        return profile.get_posts()

    raise ValueError("Use an Instagram hashtag, keyword, or profile URL for source intake.")


def scrape_instagram_source(
    *,
    source_url: str,
    max_items: int,
    max_age_days: int | None = None,
) -> list[ScrapedVideoCandidate]:
    normalized_source_url, validation_error = normalize_instagram_source_url(source_url)
    if validation_error is not None:
        raise ValueError(validation_error)
    assert normalized_source_url is not None
    if max_items < 1:
        raise ValueError("Max items must be at least 1.")
    if max_age_days is not None and max_age_days < 1:
        raise ValueError("Max age days must be at least 1.")

    cutoff = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    loader = _make_loader()
    candidates: list[ScrapedVideoCandidate] = []
    for post in _posts_for_source(loader, normalized_source_url):
        candidate = _candidate_from_post(normalized_source_url, post)
        if candidate is None:
            continue
        if cutoff is not None:
            if candidate.published_at is None or candidate.published_at < cutoff:
                continue
        candidates.append(candidate)
        if len(candidates) >= max_items:
            break

    return candidates


# ---------------------------------------------------------------------------
# Instaloader URL-batch path (parallel to yt-dlp, does not affect existing flow)
# ---------------------------------------------------------------------------

def _classify_instaloader_error(exc: Exception) -> str:
    """Return 'rate_limit', 'unavailable', 'no_video', or 'other'."""
    exc_type = type(exc).__name__.lower()
    exc_msg = str(exc).lower()

    if any(t in exc_type for t in ("toomanyrequests", "connectionreset")):
        return "rate_limit"
    if any(
        t in exc_msg
        for t in (
            "too many",
            "401",
            "429",
            "rate limit",
            "please wait a few minutes",
            "login required",
            "checkpoint",
            # 403 on /graphql/query means Instagram blocked the request
            # signature (UA/app-id). Behaviour-wise it's identical to a
            # rate-limit from the scraper's perspective: cool this profile
            # down, rotate to the next, and ultimately fall through to
            # yt-dlp. Without this, instaloader's internal retry loop
            # hammers the same URL forever and the scraper never moves on.
            "403",
            "forbidden",
        )
    ):
        return "rate_limit"
    if any(t in exc_type for t in ("notfound", "badrequest")):
        return "unavailable"
    if any(t in exc_msg for t in ("not found", "does not exist", "private", "unavailable")):
        return "unavailable"
    if "no video" in exc_msg or "not a video" in exc_msg:
        return "no_video"
    return "other"


def _build_loader_for_profile(profile_name: str) -> tuple[object, bool]:
    """Build an instaloader with cookies injected from the named profile."""
    loader = _make_loader()
    has_session = load_playwright_cookies_into_instaloader(loader, profile_name)
    return loader, has_session


def _append_retry_queue(retry_queue_path: Path | None, url: str, reason: str) -> None:
    if retry_queue_path is None:
        return
    entry = {
        "url": url,
        "reason": reason,
        "queued_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    retry_queue_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if retry_queue_path.exists():
        try:
            raw = json.loads(retry_queue_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = raw
        except Exception:  # noqa: BLE001
            existing = []
    # de-dupe by URL — last reason wins
    existing = [item for item in existing if not (isinstance(item, dict) and item.get("url") == url)]
    existing.append(entry)
    retry_queue_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _adaptive_delay_multiplier(prior_failure_count: int) -> float:
    """Multiplier applied to the random per-request delay based on a profile's
    recent rate-limit history.

    Profiles that just took a 429 should not return to baseline pacing the
    moment they come out of cooldown — Instagram is still warm on them.
    1.5x growth per recent failure, capped at 3 failures (3.375x), keeps the
    cadence in safe territory without going past ~30s/request.
    """
    failures = max(0, min(prior_failure_count, 3))
    return 1.5 ** failures


def scrape_instagram_urls_instaloader(
    urls: list[str],
    *,
    max_items: int | None = None,
    max_age_days: int | None = None,
    # Wider default delay band (was 4-8s) — more human cadence, less likely
    # to trip Instagram's burst detector on the first few requests of a run.
    delay_range: tuple[float, float] = (6.0, 14.0),
    profile_names: list[str] | None = None,
    pool: ProfilePool | None = None,
    per_profile_budget: int | None = None,
    consecutive_failure_limit: int = 3,
    retry_queue_path: Path | None = None,
    on_candidate: Callable[[ScrapedVideoCandidate], None] | None = None,
) -> tuple[list[ScrapedVideoCandidate], InstagramScrapeStats, bool]:
    """Fetch metadata for Instagram Reel URLs via instaloader, optionally rotating profiles.

    When ``profile_names`` is None the function behaves like the original single-profile
    scraper. When a list is given, the scraper rotates between profiles: on rate-limit
    it switches profile and retries the same URL (so no URL is lost to a single bad
    profile). Per-profile soft budget and consecutive-failure caps cause early rotation.

    Returns ``(candidates, stats, playwright_session_used)`` — ``True`` if at least one
    profile loaded a sessionid.
    """
    instaloader_module = _require_instaloader()

    normalized_urls = [u.strip() for u in urls if u.strip()]
    urls_to_attempt = normalized_urls[:max_items] if max_items is not None else normalized_urls

    stats = InstagramScrapeStats(
        input_urls=len(normalized_urls),
        extraction_limit=len(urls_to_attempt),
    )

    cutoff = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    profiles = profile_names or [DEFAULT_PROFILE_NAME]
    profile_iter = iter(profiles)
    current_profile = next(profile_iter, DEFAULT_PROFILE_NAME)
    loader, playwright_session_used = _build_loader_for_profile(current_profile)
    session_used_any = playwright_session_used
    profile_extractions = 0
    consecutive_failures = 0
    candidates: list[ScrapedVideoCandidate] = []

    def _switch_profile(reason: str) -> bool:
        nonlocal current_profile, loader, profile_extractions, consecutive_failures
        nonlocal playwright_session_used, session_used_any
        try:
            current_profile = next(profile_iter)
        except StopIteration:
            return False
        loader, playwright_session_used = _build_loader_for_profile(current_profile)
        session_used_any = session_used_any or playwright_session_used
        profile_extractions = 0
        consecutive_failures = 0
        print(f"scraper: switched to profile '{current_profile}' ({reason})")
        return True

    rate_limit_hit = False
    url_index = 0
    while url_index < len(urls_to_attempt):
        url = urls_to_attempt[url_index]
        shortcode = instagram_shortcode_from_url(url)
        if not shortcode:
            stats.failed_other += 1
            url_index += 1
            continue

        if url_index > 0 or profile_extractions > 0:
            # Adaptive delay: profiles with recent rate-limit history use a
            # multiplied delay so they don't crash straight back into a 429.
            # When the pool isn't passed in, behaves identically to before.
            multiplier = 1.0
            if pool is not None:
                prior = pool.profiles.get(current_profile)
                if prior is not None:
                    multiplier = _adaptive_delay_multiplier(prior.failure_count)
            base_delay = random.uniform(*delay_range)
            time.sleep(base_delay * multiplier)

        stats.attempted += 1
        try:
            post = instaloader_module.Post.from_shortcode(loader.context, shortcode)
        except Exception as exc:  # noqa: BLE001
            error_kind = _classify_instaloader_error(exc)
            if error_kind == "rate_limit":
                if pool is not None:
                    pool.mark_rate_limited(current_profile)
                if _switch_profile("rate-limited"):
                    # Retry the same URL on the new profile without double-counting.
                    stats.attempted -= 1
                    continue
                stats.failed_rate_limited += 1
                rate_limit_hit = True
                _append_retry_queue(retry_queue_path, url, "rate_limited_all_profiles")
                break
            if error_kind == "unavailable":
                stats.failed_unavailable += 1
            elif error_kind == "no_video":
                stats.failed_no_video += 1
            else:
                stats.failed_other += 1
            _append_retry_queue(retry_queue_path, url, error_kind)
            consecutive_failures += 1
            if consecutive_failures >= consecutive_failure_limit:
                if pool is not None:
                    pool.mark_rate_limited(current_profile, hours=2)
                if not _switch_profile(f"{consecutive_failures} consecutive failures"):
                    break
            url_index += 1
            continue

        if not getattr(post, "is_video", False):
            stats.failed_no_video += 1
            url_index += 1
            continue

        candidate = _candidate_from_post(url, post)
        if candidate is None:
            stats.failed_other += 1
            url_index += 1
            continue

        candidate = rank_candidate(
            candidate,
            keywords=[],
            weights=_INSTAGRAM_DISCOVERY_WEIGHTS,
            max_age_days=max_age_days,
        )

        if cutoff is not None:
            if candidate.published_at is None or candidate.published_at < cutoff:
                stats.skipped_old += 1
                url_index += 1
                continue

        stats.extracted += 1
        candidates.append(candidate)
        if on_candidate is not None:
            on_candidate(candidate)
        consecutive_failures = 0
        profile_extractions += 1
        url_index += 1

        if per_profile_budget is not None and profile_extractions >= per_profile_budget:
            if pool is not None:
                pool.mark_used(current_profile, count=profile_extractions)
            if not _switch_profile(f"budget {per_profile_budget} reached"):
                # No more profiles — stop here cleanly rather than over-using the last one.
                break

    # Mark final profile usage in the pool so LRU tracking is accurate.
    if pool is not None and profile_extractions > 0:
        pool.mark_used(current_profile, count=profile_extractions)

    return candidates, stats, session_used_any
