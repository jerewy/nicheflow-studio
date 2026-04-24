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
                "upload_date": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).strftime("%Y%m%d"),
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
    )

    ranked = rank_candidate(
        candidate,
        keywords=["funny", "gaming"],
        weights=DiscoveryWeights(),
    )

    assert ranked.ranking_score is not None
    assert ranked.ranking_score > 0
    assert ranked.match_reason is not None
