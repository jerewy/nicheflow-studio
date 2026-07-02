from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from nicheflow_studio.services import cloud_publisher
from nicheflow_studio.services.cloud_publisher import CloudPublisherError


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def read(self) -> bytes:
        return self._payload


@pytest.fixture
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev/")
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret-key")


def test_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_API_KEY", raising=False)
    assert cloud_publisher.is_configured() is False
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_URL", "https://worker.example.dev")
    assert cloud_publisher.is_configured() is False  # key still missing
    monkeypatch.setenv("CLOUDFLARE_PUBLISHER_API_KEY", "secret-key")
    assert cloud_publisher.is_configured() is True


def test_create_job_posts_payload_with_auth(monkeypatch: pytest.MonkeyPatch, _configured) -> None:
    captured: list = []

    def fake_urlopen(request, timeout=120):  # noqa: ANN001
        captured.append(request)
        return _FakeResponse(b'{"id":"job1","status":"awaiting_upload","upload_path":"/v1/jobs/job1/media"}')

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", fake_urlopen)

    result = cloud_publisher.create_job(
        external_id="local-7",
        account_key="pastmomentsdaily",
        caption="hi",
        scheduled_at="2026-06-15T02:00:00Z",
        file_name="reel.mp4",
    )

    assert result["upload_path"] == "/v1/jobs/job1/media"
    req = captured[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://worker.example.dev/v1/jobs"
    assert req.headers.get("Authorization") == "Bearer secret-key"
    body = json.loads(req.data)
    assert body["external_id"] == "local-7"
    assert body["account_key"] == "pastmomentsdaily"
    assert body["scheduled_at"] == "2026-06-15T02:00:00Z"


def test_schedule_reel_creates_then_uploads(
    monkeypatch: pytest.MonkeyPatch, _configured, tmp_path
) -> None:
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"video-bytes")
    captured: list = []

    def fake_urlopen(request, timeout=120):  # noqa: ANN001
        captured.append(request)
        if request.get_method() == "POST":
            return _FakeResponse(b'{"id":"job1","upload_path":"/v1/jobs/job1/media","status":"awaiting_upload"}')
        return _FakeResponse(b'{"id":"job1","status":"scheduled"}')

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", fake_urlopen)

    cloud_publisher.schedule_reel(
        external_id="local-7",
        account_key="pastmomentsdaily",
        caption="hi",
        scheduled_at="2026-06-15T02:00:00Z",
        video_path=video,
    )

    assert len(captured) == 2
    create_req, upload_req = captured
    assert create_req.get_method() == "POST"
    assert upload_req.get_method() == "PUT"
    assert upload_req.full_url == "https://worker.example.dev/v1/jobs/job1/media"
    assert upload_req.data == b"video-bytes"
    assert upload_req.headers.get("Content-type") == "video/mp4"


def test_schedule_reel_missing_video_raises(_configured, tmp_path) -> None:
    with pytest.raises(CloudPublisherError, match="video not found"):
        cloud_publisher.schedule_reel(
            external_id="x",
            account_key="pastmomentsdaily",
            caption="",
            scheduled_at="2026-06-15T02:00:00Z",
            video_path=tmp_path / "missing.mp4",
        )


def test_http_error_becomes_cloud_publisher_error(
    monkeypatch: pytest.MonkeyPatch, _configured
) -> None:
    def fake_urlopen(request, timeout=120):  # noqa: ANN001
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"Unauthorized")
        )

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(CloudPublisherError, match="HTTP 401"):
        cloud_publisher.get_usage()


def test_upsert_account_puts_metadata_without_token(
    monkeypatch: pytest.MonkeyPatch, _configured
) -> None:
    captured: list = []

    def fake_urlopen(request, timeout=120):  # noqa: ANN001
        captured.append(request)
        return _FakeResponse(b'{"account_key":"beneathhistory","enabled":true,"daily_limit":3}')

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", fake_urlopen)

    result = cloud_publisher.upsert_account(
        account_key="beneathhistory",
        instagram_user_id="17841400000000000",
        token_secret_name="IG_TOKEN_BENEATHHISTORY",
        enabled=True,
        daily_limit=3,
        min_gap_minutes=240,
    )

    assert result["account_key"] == "beneathhistory"
    req = captured[0]
    assert req.get_method() == "PUT"
    assert req.full_url == "https://worker.example.dev/v1/accounts"
    assert req.headers.get("Authorization") == "Bearer secret-key"
    body = json.loads(req.data)
    assert body["instagram_user_id"] == "17841400000000000"
    assert body["token_secret_name"] == "IG_TOKEN_BENEATHHISTORY"
    assert body["daily_limit"] == 3
    # The IG token must never be part of the account registration payload.
    assert "token" not in body and "access_token" not in body


def test_run_due_posts_to_run_endpoint(monkeypatch: pytest.MonkeyPatch, _configured) -> None:
    captured: list = []

    def fake_urlopen(request, timeout=120):  # noqa: ANN001
        captured.append(request)
        return _FakeResponse(b'{"processed":1,"mode":"live"}')

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", fake_urlopen)

    result = cloud_publisher.run_due()

    assert result == {"processed": 1, "mode": "live"}
    req = captured[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://worker.example.dev/v1/run"


def test_cloud_account_key_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_PUBLISH_ACCOUNTS", raising=False)
    assert cloud_publisher.cloud_publish_map() == {}
    assert cloud_publisher.cloud_account_key_for(7) is None

    monkeypatch.setenv("CLOUDFLARE_PUBLISH_ACCOUNTS", '{"7": "pastmomentsdaily", "12": "beneathhistory"}')
    assert cloud_publisher.cloud_account_key_for(7) == "pastmomentsdaily"
    assert cloud_publisher.cloud_account_key_for(12) == "beneathhistory"
    assert cloud_publisher.cloud_account_key_for(99) is None

    # Malformed JSON degrades to an empty map (feature stays inert, never crashes).
    monkeypatch.setenv("CLOUDFLARE_PUBLISH_ACCOUNTS", "not json")
    assert cloud_publisher.cloud_publish_map() == {}


def test_unconfigured_request_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_PUBLISHER_API_KEY", raising=False)
    with pytest.raises(CloudPublisherError, match="is not set"):
        cloud_publisher.list_jobs()


def test_transient_network_error_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _configured
) -> None:
    monkeypatch.setattr(cloud_publisher, "_RETRY_BACKOFF_S", 0)
    calls = {"n": 0}

    def flaky_urlopen(request, timeout=120):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < cloud_publisher._MAX_ATTEMPTS:
            raise urllib.error.URLError("The write operation timed out")
        return _FakeResponse(b'{"processed":1,"mode":"live"}')

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", flaky_urlopen)

    result = cloud_publisher.run_due()

    assert result == {"processed": 1, "mode": "live"}
    assert calls["n"] == cloud_publisher._MAX_ATTEMPTS


def test_transient_network_error_exhausts_retries(
    monkeypatch: pytest.MonkeyPatch, _configured
) -> None:
    monkeypatch.setattr(cloud_publisher, "_RETRY_BACKOFF_S", 0)
    calls = {"n": 0}

    def always_flaky_urlopen(request, timeout=120):  # noqa: ANN001
        calls["n"] += 1
        raise urllib.error.URLError("EOF occurred in violation of protocol (_ssl.c:2406)")

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", always_flaky_urlopen)

    with pytest.raises(CloudPublisherError, match="Cloud publisher unreachable"):
        cloud_publisher.run_due()

    assert calls["n"] == cloud_publisher._MAX_ATTEMPTS


def test_concurrent_uploads_are_serialized(
    monkeypatch: pytest.MonkeyPatch, _configured, tmp_path
) -> None:
    """Two simultaneous schedule_reel calls must not send overlapping requests
    (the actual root cause of the flaky network errors this module retries)."""
    import threading

    in_flight = {"count": 0, "max_seen": 0}
    lock = threading.Lock()

    def fake_urlopen(request, timeout=120):  # noqa: ANN001
        with lock:
            in_flight["count"] += 1
            in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["count"])
        time.sleep(0.05)
        with lock:
            in_flight["count"] -= 1
        if request.get_method() == "POST":
            return _FakeResponse(b'{"id":"job1","upload_path":"/v1/jobs/job1/media","status":"awaiting_upload"}')
        return _FakeResponse(b'{"id":"job1","status":"scheduled"}')

    monkeypatch.setattr(cloud_publisher.urllib.request, "urlopen", fake_urlopen)

    videos = []
    for name in ("a.mp4", "b.mp4"):
        video = tmp_path / name
        video.write_bytes(b"video-bytes")
        videos.append(video)

    threads = [
        threading.Thread(
            target=cloud_publisher.schedule_reel,
            kwargs={
                "external_id": f"local-{i}",
                "account_key": "pastmomentsdaily",
                "caption": "hi",
                "scheduled_at": "2026-06-15T02:00:00Z",
                "video_path": video,
            },
        )
        for i, video in enumerate(videos)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert in_flight["max_seen"] == 1
