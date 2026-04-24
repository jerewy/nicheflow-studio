from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from urllib.parse import urlparse

from yt_dlp import YoutubeDL


@dataclass(frozen=True)
class ScrapedVideoCandidate:
    scrape_source_url: str
    source_url: str
    extractor: str | None
    video_id: str | None
    title: str | None
    channel_name: str | None
    published_at: dt.datetime | None
    description: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    discovery_query: str | None = None
    match_reason: str | None = None
    ranking_score: int | None = None


@dataclass(frozen=True)
class DiscoveryWeights:
    views: int = 35
    likes: int = 20
    recency: int = 25
    keyword_match: int = 20


def normalize_youtube_source_url(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return (None, "Enter a full YouTube channel or profile URL.")

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    if host not in {"youtube.com", "m.youtube.com"}:
        return (None, "Only YouTube channel or profile URLs are supported for intake right now.")

    path = parsed.path.rstrip("/")
    if not path:
        return (None, "Use a YouTube channel or profile URL.")

    source_root: str | None = None
    if path.startswith("/@"):
        handle = path.split("/", 2)[1]
        source_root = f"https://www.youtube.com/{handle}"
    elif path.startswith("/channel/") or path.startswith("/c/") or path.startswith("/user/"):
        parts = path.split("/")
        if len(parts) >= 3 and parts[2]:
            source_root = f"https://www.youtube.com/{parts[1]}/{parts[2]}"

    if source_root is None:
        return (None, "Use a YouTube channel or profile URL.")

    return (source_root, None)


def validate_youtube_source_url(url: str) -> str | None:
    _normalized, validation_error = normalize_youtube_source_url(url)
    return validation_error


def infer_youtube_source_type(url: str) -> str:
    normalized_url, _validation_error = normalize_youtube_source_url(url)
    path = urlparse(normalized_url or url).path.rstrip("/")
    if path.startswith("/@"):
        return "youtube_profile"
    return "youtube_channel"


def _parse_upload_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None

    try:
        parsed = dt.datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None

    return parsed.replace(tzinfo=dt.timezone.utc)


def _parse_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _entry_source_url(entry: dict[str, object]) -> str | None:
    for key in ("webpage_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    video_id = entry.get("id")
    if isinstance(video_id, str) and video_id.strip():
        return f"https://www.youtube.com/watch?v={video_id.strip()}"

    return None


def _thumbnail_url(entry: dict[str, object]) -> str | None:
    thumbnail = entry.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail.startswith(("http://", "https://")):
        return thumbnail

    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        for candidate in reversed(thumbnails):
            if not isinstance(candidate, dict):
                continue
            url = candidate.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url

    return None


def _candidate_from_entry(
    scrape_source_url: str,
    entry: dict[str, object],
    *,
    discovery_query: str | None = None,
) -> ScrapedVideoCandidate | None:
    source_url = _entry_source_url(entry)
    if source_url is None:
        return None

    extractor = entry.get("extractor")
    video_id = entry.get("id")
    title = entry.get("title")
    channel_name = entry.get("channel") or entry.get("uploader")
    published_at = _parse_upload_date(entry.get("upload_date"))
    description = entry.get("description")
    view_count = _parse_optional_int(entry.get("view_count"))
    like_count = _parse_optional_int(entry.get("like_count"))
    duration_seconds = _parse_optional_int(entry.get("duration"))
    thumbnail_url = _thumbnail_url(entry)

    return ScrapedVideoCandidate(
        scrape_source_url=scrape_source_url,
        source_url=source_url,
        extractor=extractor if isinstance(extractor, str) else None,
        video_id=video_id if isinstance(video_id, str) else None,
        title=title if isinstance(title, str) else None,
        channel_name=channel_name if isinstance(channel_name, str) else None,
        published_at=published_at,
        description=description if isinstance(description, str) else None,
        view_count=view_count,
        like_count=like_count,
        duration_seconds=duration_seconds,
        thumbnail_url=thumbnail_url,
        discovery_query=discovery_query,
    )


def _fetch_entries(*, source_url: str, fetch_limit: int) -> list[dict[str, object]]:
    ydl_opts = {
        "extract_flat": True,
        "ignoreerrors": True,
        "noplaylist": False,
        "playlistend": fetch_limit,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=False)

    entries = info.get("entries") if isinstance(info, dict) else None
    if not isinstance(entries, list):
        return []

    return [entry for entry in entries if isinstance(entry, dict)]


def _fetch_video_details(source_url: str) -> ScrapedVideoCandidate | None:
    ydl_opts = {
        "ignoreerrors": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=False)

    if not isinstance(info, dict):
        return None

    return _candidate_from_entry(source_url, info)


def _keyword_match_score(
    *,
    title: str | None,
    description: str | None,
    keywords: list[str],
) -> float:
    if not keywords:
        return 0.0

    title_text = (title or "").lower()
    description_text = (description or "").lower()
    hits = 0.0
    for keyword in keywords:
        normalized = keyword.strip().lower()
        if not normalized:
            continue
        if normalized in title_text:
            hits += 1.0
            continue
        if normalized in description_text:
            hits += 0.6

    if hits <= 0:
        return 0.0

    return min(hits / max(len(keywords), 1), 1.0)


def _recency_score(candidate: ScrapedVideoCandidate, max_age_days: int | None) -> float:
    if candidate.published_at is None:
        return 0.0

    published_at = candidate.published_at
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=dt.timezone.utc)

    age_days = max((dt.datetime.now(dt.timezone.utc) - published_at).days, 0)
    window = max_age_days or 365
    if window < 1:
        window = 365
    score = 1 - min(age_days / window, 1)
    return max(score, 0.0)


def _engagement_score(value: int | None) -> float:
    if value is None or value < 1:
        return 0.0
    return min(math.log10(value + 1) / 6, 1.0)


def rank_candidate(
    candidate: ScrapedVideoCandidate,
    *,
    keywords: list[str],
    weights: DiscoveryWeights,
    max_age_days: int | None = None,
) -> ScrapedVideoCandidate:
    total_weight = max(weights.views + weights.likes + weights.recency + weights.keyword_match, 1)
    view_component = _engagement_score(candidate.view_count)
    like_component = _engagement_score(candidate.like_count)
    recency_component = _recency_score(candidate, max_age_days)
    keyword_component = _keyword_match_score(
        title=candidate.title,
        description=candidate.description,
        keywords=keywords,
    )

    weighted_score = (
        view_component * weights.views
        + like_component * weights.likes
        + recency_component * weights.recency
        + keyword_component * weights.keyword_match
    ) / total_weight
    ranking_score = int(round(weighted_score * 100))

    reasons: list[str] = []
    if keyword_component >= 0.5:
        reasons.append("keyword match")
    if candidate.view_count and candidate.view_count >= 100_000:
        reasons.append("high views")
    if candidate.like_count and candidate.like_count >= 5_000:
        reasons.append("strong likes")
    if recency_component >= 0.7:
        reasons.append("recent")
    if not reasons:
        reasons.append("metadata match")

    return ScrapedVideoCandidate(
        scrape_source_url=candidate.scrape_source_url,
        source_url=candidate.source_url,
        extractor=candidate.extractor,
        video_id=candidate.video_id,
        title=candidate.title,
        channel_name=candidate.channel_name,
        published_at=candidate.published_at,
        description=candidate.description,
        view_count=candidate.view_count,
        like_count=candidate.like_count,
        duration_seconds=candidate.duration_seconds,
        thumbnail_url=candidate.thumbnail_url,
        discovery_query=candidate.discovery_query,
        match_reason=", ".join(reasons),
        ranking_score=ranking_score,
    )


def scrape_youtube_source(
    *,
    source_url: str,
    max_items: int,
    max_age_days: int | None = None,
) -> list[ScrapedVideoCandidate]:
    normalized_source_url, validation_error = normalize_youtube_source_url(source_url)
    if validation_error is not None:
        raise ValueError(validation_error)
    assert normalized_source_url is not None
    if max_items < 1:
        raise ValueError("Max items must be at least 1.")
    if max_age_days is not None and max_age_days < 1:
        raise ValueError("Max age days must be at least 1.")

    fetch_limit = max_items if max_age_days is None else min(max_items * 4, 200)
    entries = _fetch_entries(source_url=normalized_source_url, fetch_limit=fetch_limit)

    cutoff = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    candidates: list[ScrapedVideoCandidate] = []
    for entry in entries:
        source_candidate = _candidate_from_entry(normalized_source_url, entry)
        if source_candidate is None:
            continue
        details = _fetch_video_details(source_candidate.source_url)
        candidate = details or source_candidate
        if candidate.published_at is None:
            candidate = ScrapedVideoCandidate(
                scrape_source_url=source_url,
                source_url=candidate.source_url,
                extractor=candidate.extractor,
                video_id=candidate.video_id,
                title=candidate.title,
                channel_name=candidate.channel_name,
                published_at=source_candidate.published_at,
                description=candidate.description,
                view_count=candidate.view_count,
                like_count=candidate.like_count,
                duration_seconds=candidate.duration_seconds,
                thumbnail_url=candidate.thumbnail_url,
                discovery_query=None,
                match_reason=candidate.match_reason,
                ranking_score=candidate.ranking_score,
            )
        if candidate is None:
            continue

        if cutoff is not None:
            if candidate.published_at is None or candidate.published_at < cutoff:
                continue

        candidates.append(candidate)
        if len(candidates) >= max_items:
            break

    return candidates


def search_youtube_query(
    *,
    query: str,
    max_items: int,
    max_age_days: int | None = None,
) -> list[ScrapedVideoCandidate]:
    if max_items < 1:
        raise ValueError("Max items must be at least 1.")
    if max_age_days is not None and max_age_days < 1:
        raise ValueError("Max age days must be at least 1.")

    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Discovery query is required.")

    fetch_limit = max_items if max_age_days is None else min(max_items * 4, 200)
    entries = _fetch_entries(source_url=f"ytsearch{fetch_limit}:{normalized_query}", fetch_limit=fetch_limit)

    cutoff = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    candidates: list[ScrapedVideoCandidate] = []
    for entry in entries:
        search_candidate = _candidate_from_entry("", entry, discovery_query=normalized_query)
        if search_candidate is None:
            continue
        details = _fetch_video_details(search_candidate.source_url)
        candidate = details or search_candidate
        candidate = ScrapedVideoCandidate(
            scrape_source_url="",
            source_url=candidate.source_url,
            extractor=candidate.extractor,
            video_id=candidate.video_id,
            title=candidate.title,
            channel_name=candidate.channel_name or search_candidate.channel_name,
            published_at=candidate.published_at or search_candidate.published_at,
            description=candidate.description,
            view_count=candidate.view_count,
            like_count=candidate.like_count,
            duration_seconds=candidate.duration_seconds,
            thumbnail_url=candidate.thumbnail_url,
            discovery_query=normalized_query,
            match_reason=candidate.match_reason,
            ranking_score=candidate.ranking_score,
        )
        if cutoff is not None:
            if candidate.published_at is None or candidate.published_at < cutoff:
                continue
        candidates.append(candidate)
        if len(candidates) >= max_items:
            break

    return candidates
