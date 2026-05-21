from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlparse

from nicheflow_studio.core.instagram_session import load_latest_instagram_session
from nicheflow_studio.downloader.instagram import instagram_shortcode_from_url
from nicheflow_studio.scraper.youtube import DiscoveryWeights, ScrapedVideoCandidate, rank_candidate
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

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
        max_connection_attempts=1,
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


def scrape_instagram_url(url: str) -> ScrapedVideoCandidate:
    if instagram_shortcode_from_url(url) is None:
        raise ValueError("Use an Instagram Reel or post URL.")

    ydl_opts = {
        "ignoreerrors": False,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
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
    if max_items is not None and max_items < 1:
        raise ValueError("Max items must be at least 1.")
    if max_age_days is not None and max_age_days < 1:
        raise ValueError("Max age days must be at least 1.")

    cutoff = None
    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)

    candidates: list[ScrapedVideoCandidate] = []
    for url in urls:
        normalized_url = url.strip()
        if not normalized_url:
            continue
        try:
            candidate = scrape_instagram_url(normalized_url)
        except DownloadError:
            continue
        if cutoff is not None:
            if candidate.published_at is None or candidate.published_at < cutoff:
                continue
        candidates.append(candidate)
        if max_items is not None and len(candidates) >= max_items:
            break

    return candidates


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
