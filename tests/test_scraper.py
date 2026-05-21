from __future__ import annotations

import datetime as dt

from nicheflow_studio.scraper.youtube import (
    DiscoveryWeights,
    normalize_youtube_source_url,
    ScrapedVideoCandidate,
    rank_candidate,
    scrape_youtube_source,
    search_youtube_query,
    validate_youtube_source_url,
)
from nicheflow_studio.scraper.instagram import (
    infer_instagram_source_type,
    normalize_instagram_source_url,
    scrape_instagram_url,
    scrape_instagram_source,
    scrape_instagram_urls,
)


class FakeYoutubeDL:
    def __init__(self, opts):  # noqa: ANN001
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def extract_info(self, source_url: str, download: bool = False):  # noqa: FBT002
        assert download is False
        if source_url == "https://www.youtube.com/@clips":
            today = dt.datetime.now(dt.timezone.utc)
            recent = today.strftime("%Y%m%d")
            old = (today - dt.timedelta(days=90)).strftime("%Y%m%d")
            return {
                "entries": [
                    {
                        "id": "recent1",
                        "title": "Recent 1",
                        "channel": "Channel A",
                        "upload_date": recent,
                    },
                    {
                        "id": "old1",
                        "title": "Old 1",
                        "channel": "Channel A",
                        "upload_date": old,
                    },
                ]
            }
        if source_url == "https://www.youtube.com/watch?v=recent1":
            return {
                "id": "recent1",
                "webpage_url": source_url,
                "title": "Recent 1",
                "channel": "Channel A",
                "upload_date": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"),
                "view_count": 250000,
                "like_count": 12000,
                "description": "A recent viral clip",
            }
        if source_url == "https://www.youtube.com/watch?v=old1":
            return {
                "id": "old1",
                "webpage_url": source_url,
                "title": "Old 1",
                "channel": "Channel A",
                "upload_date": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).strftime(
                    "%Y%m%d"
                ),
                "view_count": 100,
                "like_count": 10,
                "description": "An old clip",
            }
        if source_url.startswith("ytsearch"):
            return {
                "entries": [
                    {
                        "id": "search1",
                        "title": "Search Result",
                        "channel": "Search Channel",
                        "upload_date": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"),
                    }
                ]
            }
        if source_url == "https://www.youtube.com/watch?v=search1":
            return {
                "id": "search1",
                "webpage_url": source_url,
                "title": "Search Result",
                "channel": "Search Channel",
                "upload_date": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"),
                "view_count": 50000,
                "like_count": 3000,
                "description": "great funny gaming highlight",
            }
        raise AssertionError(f"Unexpected source_url {source_url}")


class FakeInstagramYoutubeDL:
    def __init__(self, opts):  # noqa: ANN001
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def extract_info(self, source_url: str, download: bool = False):  # noqa: FBT002
        assert download is False
        shortcode = source_url.rstrip("/").rsplit("/", 1)[-1]
        if shortcode == "photoOnly":
            from yt_dlp.utils import DownloadError

            raise DownloadError("ERROR: [Instagram] photoOnly: There is no video in this post")
        return {
            "id": shortcode,
            "webpage_url": source_url,
            "title": "Video by meme.ig",
            "description": "Changing skins in a game means customizing the look.",
            "duration": 10.935,
            "timestamp": 1779061450,
            "uploader": "MEMES",
            "channel": "meme.ig",
            "like_count": 6713,
            "comment_count": 24,
            "thumbnail": "https://cdn.example/thumb.jpg",
            "extractor": "Instagram",
        }


def test_validate_youtube_source_url_accepts_profile_and_channel_urls() -> None:
    assert validate_youtube_source_url("https://www.youtube.com/@clips") is None
    assert validate_youtube_source_url("https://www.youtube.com/channel/abc123") is None
    assert validate_youtube_source_url("https://www.youtube.com/@clips/shorts") is None


def test_normalize_youtube_source_url_trims_subpages_to_root() -> None:
    normalized, error = normalize_youtube_source_url("https://www.youtube.com/@clips/shorts")
    assert error is None
    assert normalized == "https://www.youtube.com/@clips"


def test_validate_youtube_source_url_rejects_watch_urls() -> None:
    assert (
        validate_youtube_source_url("https://www.youtube.com/watch?v=abc123")
        == "Use a YouTube channel or profile URL."
    )


def test_scrape_youtube_source_filters_by_recency_and_limit(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.scraper.youtube.YoutubeDL", FakeYoutubeDL)

    candidates = scrape_youtube_source(
        source_url="https://www.youtube.com/@clips",
        max_items=1,
        max_age_days=30,
    )

    assert len(candidates) == 1
    assert candidates[0].video_id == "recent1"
    assert candidates[0].source_url == "https://www.youtube.com/watch?v=recent1"
    assert candidates[0].view_count == 250000
    assert candidates[0].like_count == 12000


def test_search_youtube_query_returns_rich_metadata(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.scraper.youtube.YoutubeDL", FakeYoutubeDL)

    candidates = search_youtube_query(
        query="funny gaming",
        max_items=2,
        max_age_days=30,
    )

    assert len(candidates) == 1
    assert candidates[0].discovery_query == "funny gaming"
    assert candidates[0].description == "great funny gaming highlight"
    assert candidates[0].view_count == 50000
    assert candidates[0].like_count == 3000


def test_rank_candidate_uses_keyword_match_and_engagement() -> None:
    candidate = ScrapedVideoCandidate(
        scrape_source_url="",
        source_url="https://www.youtube.com/watch?v=abc123",
        extractor="youtube",
        video_id="abc123",
        title="Funny Gaming Clip",
        channel_name="Channel A",
        published_at=dt.datetime.now(dt.timezone.utc),
        description="best funny moments and gaming edits",
        view_count=200000,
        like_count=10000,
        comment_count=80,
        duration_seconds=18,
    )

    ranked = rank_candidate(
        candidate,
        keywords=["funny", "gaming"],
        weights=DiscoveryWeights(),
    )

    assert ranked.ranking_score is not None
    assert ranked.ranking_score > 0
    assert ranked.match_reason is not None


class FakeInstagramPost:
    def __init__(self, shortcode: str, *, is_video: bool = True) -> None:
        self.shortcode = shortcode
        self.is_video = is_video
        self.caption = "Funny gaming moment #gaming"
        self.owner_username = "respawnclips"
        self.date_utc = dt.datetime.now(dt.timezone.utc)
        self.video_view_count = 120000
        self.likes = 9000
        self.comments = 52
        self.video_duration = 12
        self.url = "https://cdn.example/thumb.jpg"


class FakeInstagramHashtag:
    @classmethod
    def from_name(cls, context, name: str):  # noqa: ANN001
        assert name == "gaming"
        return cls()

    def get_posts(self):
        return iter(
            [
                FakeInstagramPost("abc123"),
                FakeInstagramPost("photo123", is_video=False),
            ]
        )


class FakeInstagramProfile:
    @classmethod
    def from_username(cls, context, username: str):  # noqa: ANN001
        assert username == "respawnclips"
        return cls()

    def get_posts(self):
        return iter([FakeInstagramPost("profile123")])


class FakeInstaloader:
    Hashtag = FakeInstagramHashtag
    Profile = FakeInstagramProfile

    class Instaloader:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.context = object()


def test_normalize_instagram_source_accepts_keyword_and_hashtag_url() -> None:
    normalized, error = normalize_instagram_source_url("#gaming")

    assert error is None
    assert normalized == "https://www.instagram.com/explore/tags/gaming/"
    assert infer_instagram_source_type(normalized) == "instagram_hashtag"


def test_scrape_instagram_url_returns_metadata(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.scraper.instagram.YoutubeDL", FakeInstagramYoutubeDL)

    candidate = scrape_instagram_url("https://www.instagram.com/p/DYdWmVauJRQ/")

    assert candidate.extractor == "instagram"
    assert candidate.video_id == "DYdWmVauJRQ"
    assert candidate.source_url == "https://www.instagram.com/p/DYdWmVauJRQ/"
    assert candidate.title == "Video by meme.ig"
    assert candidate.channel_name == "meme.ig"
    assert candidate.description == "Changing skins in a game means customizing the look."
    assert candidate.duration_seconds == 10
    assert candidate.like_count == 6713
    assert candidate.comment_count == 24
    assert candidate.ranking_score is not None
    assert candidate.ranking_score > 0
    assert candidate.thumbnail_url == "https://cdn.example/thumb.jpg"
    assert candidate.published_at is not None
    assert candidate.match_reason is not None
    assert "strong likes" in candidate.match_reason


def test_scrape_instagram_urls_batches_metadata(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.scraper.instagram.YoutubeDL", FakeInstagramYoutubeDL)

    candidates = scrape_instagram_urls(
        [
            "https://www.instagram.com/p/DYdWmVauJRQ/",
            "https://www.instagram.com/p/DYdxGRpO7Am/",
        ],
        max_items=2,
    )

    assert [candidate.video_id for candidate in candidates] == ["DYdWmVauJRQ", "DYdxGRpO7Am"]


def test_scrape_instagram_urls_skips_non_video_posts(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.scraper.instagram.YoutubeDL", FakeInstagramYoutubeDL)

    candidates = scrape_instagram_urls(
        [
            "https://www.instagram.com/reel/DYdWmVauJRQ/",
            "https://www.instagram.com/p/photoOnly/",
            "https://www.instagram.com/reel/DYdxGRpO7Am/",
        ],
        max_items=3,
    )

    assert [candidate.video_id for candidate in candidates] == ["DYdWmVauJRQ", "DYdxGRpO7Am"]


def test_scrape_instagram_source_returns_video_candidates(monkeypatch) -> None:
    monkeypatch.setattr("nicheflow_studio.scraper.instagram.instaloader", FakeInstaloader)

    candidates = scrape_instagram_source(
        source_url="https://www.instagram.com/explore/tags/gaming/",
        max_items=2,
    )

    assert len(candidates) == 1
    assert candidates[0].extractor == "instagram"
    assert candidates[0].source_url == "https://www.instagram.com/reel/abc123/"
    assert candidates[0].video_id == "abc123"
    assert candidates[0].view_count == 120000
    assert candidates[0].comment_count == 52
