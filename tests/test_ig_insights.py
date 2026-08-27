from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
import pytest
import subprocess
import sys
import urllib.error
import urllib.parse

from nicheflow_studio.db.models import AccountPostMetric
from nicheflow_studio.db.post_metrics import (
    top_titles_for_account,
    upsert_account_post_metrics,
)
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.services.instagram_insights import (
    GraphAPIError,
    build_metric_row,
    calculate_conversion_score,
    collect_account_metrics,
    extract_shortcode,
    fetch_account_media,
    fetch_media_insights,
    request_json,
)


def test_calculate_conversion_score_weights_high_intent_engagement() -> None:
    assert (
        calculate_conversion_score(
            reach=10,
            likes=5,
            comments=4,
            saved=2,
            shares=3,
        )
        == 2.8
    )


def test_calculate_conversion_score_uses_one_when_reach_is_zero() -> None:
    assert (
        calculate_conversion_score(
            reach=0,
            likes=1,
            comments=1,
            saved=1,
            shares=1,
        )
        == 9.0
    )


def test_upsert_account_post_metrics_is_idempotent() -> None:
    init_db()
    first_pulled_at = dt.datetime(2026, 6, 20, 1, tzinfo=dt.timezone.utc)
    second_pulled_at = dt.datetime(2026, 6, 20, 2, tzinfo=dt.timezone.utc)
    base_row = {
        "account_key": "pastmomentsdaily",
        "shortcode": "ABC123",
        "caption": "Original caption",
        "timestamp": dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
        "reach": 100,
        "views": 120,
        "likes": 10,
        "comments": 2,
        "saved": 3,
        "shares": 4,
        "total_interactions": 19,
        "conversion_score": 0.35,
        "pulled_at": first_pulled_at,
    }

    upsert_account_post_metrics([base_row])
    upsert_account_post_metrics(
        [
            {
                **base_row,
                "caption": "Updated caption",
                "reach": 200,
                "conversion_score": 0.2,
                "pulled_at": second_pulled_at,
            }
        ]
    )

    with get_session() as session:
        rows = session.query(AccountPostMetric).all()

        assert len(rows) == 1
        assert rows[0].caption == "Updated caption"
        assert rows[0].reach == 200
        assert rows[0].conversion_score == 0.2
        assert rows[0].pulled_at == second_pulled_at.replace(tzinfo=None)


def test_top_titles_for_account_returns_highest_conversion_scores_only() -> None:
    pulled_at = dt.datetime(2026, 6, 20, 2, tzinfo=dt.timezone.utc)
    timestamp = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def metric(
        account_key: str, shortcode: str, caption: str, score: float, reach: int = 5_000
    ) -> dict:
        return {
            "account_key": account_key,
            "shortcode": shortcode,
            "caption": caption,
            "timestamp": timestamp,
            "reach": reach,
            "views": reach,
            "likes": 0,
            "comments": 0,
            "saved": 0,
            "shares": 0,
            "total_interactions": 0,
            "conversion_score": score,
            "pulled_at": pulled_at,
        }

    upsert_account_post_metrics(
        [
            metric("pastmomentsdaily", "LOW", "Lower title", 0.1),
            metric("pastmomentsdaily", "HIGH", "Highest title", 0.9),
            metric("pastmomentsdaily", "MID", "Middle title", 0.5),
            metric("anotheraccount", "OTHER", "Wrong account", 2.0),
        ]
    )

    assert top_titles_for_account("pastmomentsdaily", n=2) == [
        "Highest title",
        "Middle title",
    ]


def test_fetch_media_insights_falls_back_after_unsupported_metrics() -> None:
    requested_metric_sets: list[str] = []

    def fake_fetch_json(url: str) -> dict[str, object]:
        metrics = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["metric"][0]
        requested_metric_sets.append(metrics)
        if "views" in metrics:
            raise GraphAPIError(400, "unsupported metric; token=secret-token")
        return {
            "data": [
                {"name": "reach", "values": [{"value": 100}]},
                {"name": "saved", "values": [{"value": 7}]},
                {"name": "shares", "values": [{"value": 5}]},
            ]
        }

    result = fetch_media_insights(
        media_id="17890000000000000",
        token="secret-token",
        fetch_json=fake_fetch_json,
    )

    assert requested_metric_sets == [
        "reach,likes,comments,saved,shares,total_interactions,views",
        "reach,likes,comments,saved,shares,total_interactions",
    ]
    assert result == {"reach": 100, "saved": 7, "shares": 5}


def test_fetch_media_insights_drops_shares_and_interactions_on_second_400() -> None:
    requested_metric_sets: list[str] = []

    def fake_fetch_json(url: str) -> dict[str, object]:
        metrics = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["metric"][0]
        requested_metric_sets.append(metrics)
        if len(requested_metric_sets) < 3:
            raise GraphAPIError(400, "unsupported metric")
        return {
            "data": [
                {"name": "reach", "values": [{"value": 50}]},
                {"name": "likes", "values": [{"value": 4}]},
            ]
        }

    result = fetch_media_insights(
        media_id="17890000000000000",
        token="secret-token",
        fetch_json=fake_fetch_json,
    )

    assert requested_metric_sets[-1] == "reach,likes,comments,saved"
    assert result == {"reach": 50, "likes": 4}


def test_extract_shortcode_supports_reel_and_post_permalinks() -> None:
    assert (
        extract_shortcode("https://www.instagram.com/reel/ABC_123/?utm_source=ig_web_copy_link")
        == "ABC_123"
    )
    assert extract_shortcode("https://www.instagram.com/p/XYZ-789/") == "XYZ-789"


def test_fetch_account_media_follows_paging_next() -> None:
    requested_urls: list[str] = []

    def fake_fetch_json(url: str) -> dict[str, object]:
        requested_urls.append(url)
        if len(requested_urls) == 1:
            return {
                "data": [{"id": "1"}],
                "paging": {"next": "https://graph.instagram.com/next-page"},
            }
        return {"data": [{"id": "2"}]}

    media = fetch_account_media(
        user_id="12345",
        token="secret-token",
        fetch_json=fake_fetch_json,
    )

    assert media == [{"id": "1"}, {"id": "2"}]
    assert requested_urls[1] == "https://graph.instagram.com/next-page"
    assert "permalink" in requested_urls[0]


def test_build_metric_row_normalizes_missing_metrics_and_calculates_score() -> None:
    pulled_at = dt.datetime(2026, 6, 20, 3, tzinfo=dt.timezone.utc)

    row = build_metric_row(
        account_key="pastmomentsdaily",
        media={
            "permalink": "https://www.instagram.com/reel/ABC123/",
            "caption": "A real caption",
            "timestamp": "2026-06-01T12:30:00+0000",
        },
        insights={"reach": 100, "likes": 10, "saved": 5},
        pulled_at=pulled_at,
    )

    assert row == {
        "account_key": "pastmomentsdaily",
        "shortcode": "ABC123",
        "media_id": None,  # this fixture media carries no Graph id
        "caption": "A real caption",
        "timestamp": dt.datetime(2026, 6, 1, 12, 30, tzinfo=dt.timezone.utc),
        "reach": 100,
        "views": 0,
        "likes": 10,
        "comments": 0,
        "saved": 5,
        "shares": 0,
        "total_interactions": 0,
        "conversion_score": 0.25,
        "pulled_at": pulled_at,
    }


def test_request_json_redacts_token_from_graph_error_body() -> None:
    def failing_opener(request: object, timeout: int) -> object:
        raise urllib.error.HTTPError(
            url="https://graph.instagram.com/v21.0/me",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"token secret-token is invalid"}'),
        )

    try:
        request_json(
            "https://graph.instagram.com/v21.0/me?access_token=secret-token",
            token="secret-token",
            opener=failing_opener,
        )
    except GraphAPIError as exc:
        assert exc.status_code == 400
        assert exc.body == '{"error":"token <redacted> is invalid"}'
        assert "secret-token" not in str(exc)
    else:
        raise AssertionError("Expected GraphAPIError")


def test_collect_account_metrics_builds_rows_from_mocked_graph_responses() -> None:
    pulled_at = dt.datetime(2026, 6, 20, 4, tzinfo=dt.timezone.utc)

    def fake_fetch_json(url: str) -> dict[str, object]:
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/me"):
            return {
                "user_id": "12345",
                "username": "pastmomentsdaily",
                "account_type": "MEDIA_CREATOR",
                "media_count": 1,
            }
        if path.endswith("/12345/media"):
            return {
                "data": [
                    {
                        "id": "media-1",
                        "permalink": "https://www.instagram.com/reel/ABC123/",
                        "caption": "A caption",
                        "timestamp": "2026-06-01T12:30:00+0000",
                    }
                ]
            }
        if path.endswith("/media-1/insights"):
            return {
                "data": [
                    {"name": "reach", "values": [{"value": 100}]},
                    {"name": "likes", "values": [{"value": 10}]},
                    {"name": "saved", "values": [{"value": 5}]},
                ]
            }
        raise AssertionError(f"Unexpected URL: {url}")

    account, rows = collect_account_metrics(
        account_key="pastmomentsdaily",
        user_id="12345",
        token="secret-token",
        fetch_json=fake_fetch_json,
        pulled_at=pulled_at,
    )

    assert account["username"] == "pastmomentsdaily"
    assert len(rows) == 1
    assert rows[0]["shortcode"] == "ABC123"
    assert rows[0]["conversion_score"] == 0.25


def test_ig_insights_cli_help_does_not_require_credentials() -> None:
    project_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, str(project_root / "scripts" / "ig_insights.py"), "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "account_key" in result.stdout


def test_top_titles_for_account_ignores_barely_distributed_posts() -> None:
    # conversion_score is interactions/reach, so a post Instagram never pushed
    # scores on a tiny denominator. On a real account the WORST post (163 views,
    # reach 75) outranked everything but the one viral hit. Few-shot examples
    # teach register, so that would train the model on the posts that failed.
    pulled_at = dt.datetime(2026, 6, 20, 2, tzinfo=dt.timezone.utc)
    timestamp = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def metric(shortcode: str, caption: str, score: float, reach: int) -> dict:
        return {
            "account_key": "floortest",
            "shortcode": shortcode,
            "caption": caption,
            "timestamp": timestamp,
            "reach": reach,
            "views": reach,
            "likes": 0,
            "comments": 0,
            "saved": 0,
            "shares": 0,
            "total_interactions": 0,
            "conversion_score": score,
            "pulled_at": pulled_at,
        }

    upsert_account_post_metrics(
        [
            metric("FLOP", "Flop that nobody saw", 0.99, reach=75),
            metric("REAL", "Real winner", 0.20, reach=50_000),
        ]
    )

    assert top_titles_for_account("floortest") == ["Real winner"]


def test_collect_account_metrics_checkpoints_each_page() -> None:
    # One request per post against a per-hour quota means a large account can be
    # throttled part way through. Rows must be handed over per page so a failure
    # keeps what was already pulled instead of discarding the whole run.
    saved_pages: list[list[dict[str, object]]] = []

    def fake_fetch_json(url: str) -> dict[str, object]:
        if "/media?" in url:
            return {
                "data": [{"id": "1", "permalink": "https://www.instagram.com/reel/AAA/"}],
                "paging": {"next": "https://graph.instagram.com/page2"},
            }
        if url == "https://graph.instagram.com/page2":
            return {"data": [{"id": "2", "permalink": "https://www.instagram.com/reel/BBB/"}]}
        if "/insights?" in url:
            return {"data": [{"name": "reach", "values": [{"value": 10}]}]}
        return {"user_id": "12345", "username": "acct", "media_count": 2}

    _account, rows = collect_account_metrics(
        account_key="acct",
        user_id="12345",
        token="secret-token",
        fetch_json=fake_fetch_json,
        pulled_at=dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc),
        on_page=saved_pages.append,
    )

    assert [row["shortcode"] for row in rows] == ["AAA", "BBB"]
    # Two pages checkpointed separately, not one flush at the end.
    assert [[row["shortcode"] for row in page] for page in saved_pages] == [["AAA"], ["BBB"]]


def test_collect_account_metrics_keeps_earlier_pages_when_a_later_one_fails() -> None:
    def fake_fetch_json(url: str) -> dict[str, object]:
        if "/media?" in url:
            return {
                "data": [{"id": "1", "permalink": "https://www.instagram.com/reel/AAA/"}],
                "paging": {"next": "https://graph.instagram.com/page2"},
            }
        if url == "https://graph.instagram.com/page2":
            raise GraphAPIError(429, "rate limited")
        if "/insights?" in url:
            return {"data": [{"name": "reach", "values": [{"value": 10}]}]}
        return {"user_id": "12345", "username": "acct", "media_count": 2}

    saved_pages: list[list[dict[str, object]]] = []
    with pytest.raises(GraphAPIError):
        collect_account_metrics(
            account_key="acct",
            user_id="12345",
            token="secret-token",
            fetch_json=fake_fetch_json,
            pulled_at=dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc),
            on_page=saved_pages.append,
        )

    assert [[row["shortcode"] for row in page] for page in saved_pages] == [["AAA"]]


def test_fetch_media_insights_returns_none_for_pre_conversion_media() -> None:
    # Instagram permanently refuses insights for media posted before the account
    # became a business/creator profile (subcode 2108006). Raising there aborted
    # a real 329-post account at post 300; those posts must be skipped instead.
    def fake_fetch_json(url: str) -> dict[str, object]:
        raise GraphAPIError(
            400,
            '{"error":{"message":"Media posted before...","code":100,'
            '"error_subcode":2108006}}',
        )

    assert (
        fetch_media_insights(
            media_id="17890000000000000",
            token="secret-token",
            fetch_json=fake_fetch_json,
        )
        is None
    )


def test_collect_account_metrics_skips_media_without_insights() -> None:
    def fake_fetch_json(url: str) -> dict[str, object]:
        if "/media?" in url:
            return {
                "data": [
                    {"id": "1", "permalink": "https://www.instagram.com/reel/AAA/"},
                    {"id": "2", "permalink": "https://www.instagram.com/reel/BBB/"},
                ]
            }
        if "/2/insights?" in url:
            raise GraphAPIError(400, '{"error":{"error_subcode":2108006}}')
        if "/insights?" in url:
            return {"data": [{"name": "reach", "values": [{"value": 10}]}]}
        return {"user_id": "12345", "username": "acct", "media_count": 2}

    account, rows = collect_account_metrics(
        account_key="acct",
        user_id="12345",
        token="secret-token",
        fetch_json=fake_fetch_json,
        pulled_at=dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc),
    )

    # The measurable post survives; the unmeasurable one is skipped, not stored
    # as zeros (which would read as a flop in the analysis).
    assert [row["shortcode"] for row in rows] == ["AAA"]
    assert account["insights_unavailable"] == 1


def test_build_metric_row_carries_the_graph_media_id() -> None:
    """The media id is the join key back to the UploadJob that wrote the title.

    Without it, a cloud-published post's metrics can only be matched to its
    draft by caption text, which breaks on edits, reposts, and duplicates.
    """
    row = build_metric_row(
        account_key="pastmomentsdaily",
        media={
            "id": "17912345678901234",
            "permalink": "https://www.instagram.com/reel/ABC123/",
            "caption": "A real caption",
            "timestamp": "2026-06-01T12:30:00+0000",
        },
        insights={"reach": 100, "likes": 10},
        pulled_at=dt.datetime(2026, 6, 20, 3, tzinfo=dt.timezone.utc),
    )

    assert row["media_id"] == "17912345678901234"
    assert row["shortcode"] == "ABC123"
