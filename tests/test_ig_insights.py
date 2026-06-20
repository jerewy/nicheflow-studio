from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
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

    def metric(account_key: str, shortcode: str, caption: str, score: float) -> dict:
        return {
            "account_key": account_key,
            "shortcode": shortcode,
            "caption": caption,
            "timestamp": timestamp,
            "reach": 100,
            "views": 100,
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
