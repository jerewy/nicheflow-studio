from __future__ import annotations

import datetime as dt

from nicheflow_studio.scraper import instagram_apify


def test_apify_item_maps_metadata_into_candidate() -> None:
    candidate = instagram_apify._candidate_from_apify_item(
        {
            "shortCode": "ABC123",
            "url": "https://www.instagram.com/reel/ABC123/",
            "caption": "A forgotten effects trick shaped the scene\nSecond line",
            "ownerUsername": "pastmomentsdaily",
            "timestamp": "2026-05-25T12:34:56.000Z",
            "videoViewCount": 123456,
            "likesCount": "7890",
            "commentsCount": 321,
            "videoDuration": 18.7,
            "displayUrl": "https://cdn.example/thumb.jpg",
        }
    )

    assert candidate is not None
    assert candidate.extractor == "apify:instagram"
    assert candidate.video_id == "ABC123"
    assert candidate.source_url == "https://www.instagram.com/reel/ABC123/"
    assert candidate.channel_name == "pastmomentsdaily"
    assert candidate.title == "A forgotten effects trick shaped the scene"
    assert candidate.description == "A forgotten effects trick shaped the scene\nSecond line"
    assert candidate.published_at == dt.datetime(2026, 5, 25, 12, 34, 56, tzinfo=dt.timezone.utc)
    assert candidate.view_count == 123456
    assert candidate.like_count == 7890
    assert candidate.comment_count == 321
    assert candidate.duration_seconds == 18
    assert candidate.thumbnail_url == "https://cdn.example/thumb.jpg"
    assert candidate.ranking_score is not None
    assert candidate.ranking_score > 0
    assert candidate.match_reason is not None
    assert "apify metadata" not in candidate.match_reason


def test_scrape_instagram_urls_apify_reads_actor_dataset(monkeypatch) -> None:
    captured_input: dict | None = None

    class FakeActor:
        def call(self, *, run_input, timeout_secs):  # noqa: ANN001
            nonlocal captured_input
            captured_input = run_input
            assert timeout_secs == 300
            return {"defaultDatasetId": "dataset-1"}

    class FakeDataset:
        def iterate_items(self):
            return iter(
                [
                    {
                        "shortCode": "ONE",
                        "url": "https://www.instagram.com/reel/ONE/",
                        "caption": "First candidate",
                        "ownerUsername": "source",
                        "timestamp": "2026-05-25T00:00:00Z",
                        "videoPlayCount": 1000,
                        "likesCount": 120,
                        "commentsCount": 12,
                        "videoDuration": 9,
                    }
                ]
            )

    class FakeClient:
        def actor(self, actor_id: str):
            assert actor_id == instagram_apify.APIFY_ACTOR_ID
            return FakeActor()

        def dataset(self, dataset_id: str):
            assert dataset_id == "dataset-1"
            return FakeDataset()

    monkeypatch.setattr(instagram_apify, "_require_client", lambda: FakeClient())

    candidates = instagram_apify.scrape_instagram_urls_apify(
        ["https://www.instagram.com/reel/ONE/"],
        results_limit=5,
    )

    assert captured_input == {
        "directUrls": ["https://www.instagram.com/reel/ONE/"],
        "resultsType": "details",
        "resultsLimit": 5,
        "addParentData": False,
    }
    assert len(candidates) == 1
    assert candidates[0].video_id == "ONE"
    assert candidates[0].view_count == 1000
    assert candidates[0].like_count == 120
    assert candidates[0].comment_count == 12


def test_scrape_instagram_source_apify_uses_since_and_rewrites_source_url(monkeypatch) -> None:
    captured_input: dict | None = None

    class FakeActor:
        def call(self, *, run_input, timeout_secs):  # noqa: ANN001
            nonlocal captured_input
            captured_input = run_input
            assert timeout_secs == 600
            return {"defaultDatasetId": "dataset-source"}

    class FakeDataset:
        def iterate_items(self):
            return iter(
                [
                    {
                        "shortCode": "SOURCE1",
                        "url": "https://www.instagram.com/reel/SOURCE1/",
                        "caption": "Source candidate",
                        "ownerUsername": "source",
                        "timestamp": "2026-05-25T00:00:00Z",
                        "videoViewCount": 1000,
                        "likesCount": 120,
                        "commentsCount": 12,
                        "videoDuration": 9,
                    },
                    {
                        "shortCode": "OLD1",
                        "url": "https://www.instagram.com/reel/OLD1/",
                        "caption": "Old candidate",
                        "ownerUsername": "source",
                        "timestamp": "2026-04-01T00:00:00Z",
                        "videoViewCount": 1000,
                        "likesCount": 120,
                        "commentsCount": 12,
                        "videoDuration": 9,
                    },
                ]
            )

    class FakeClient:
        def actor(self, actor_id: str):
            assert actor_id == instagram_apify.APIFY_ACTOR_ID
            return FakeActor()

        def dataset(self, dataset_id: str):
            assert dataset_id == "dataset-source"
            return FakeDataset()

    monkeypatch.setattr(instagram_apify, "_require_client", lambda: FakeClient())

    source_url = "https://www.instagram.com/source/"
    candidates = instagram_apify.scrape_instagram_source_apify(
        source_url=source_url,
        max_items=10,
        max_age_days=14,
        since=dt.datetime(2026, 5, 24, 9, 30, tzinfo=dt.timezone.utc),
    )

    assert captured_input == {
        "directUrls": [source_url],
        "resultsType": "posts",
        "resultsLimit": 10,
        "addParentData": False,
        "onlyPostsNewerThan": "2026-05-24",
    }
    assert len(candidates) == 1
    assert candidates[0].scrape_source_url == source_url
    assert candidates[0].source_url == "https://www.instagram.com/reel/SOURCE1/"
    assert candidates[0].video_id == "SOURCE1"
    assert candidates[0].view_count == 1000
