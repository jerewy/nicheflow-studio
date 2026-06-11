from __future__ import annotations

import io

from scripts import nicheflow_capture_host
from scripts.nicheflow_capture_host import (
    read_native_message,
    write_native_message,
)


def test_native_message_round_trip() -> None:
    stream = io.BytesIO()
    payload = {
        "action": "capture_to_pool",
        "url": "https://www.instagram.com/reel/ABC123/",
    }

    write_native_message(stream, payload)
    stream.seek(0)

    assert read_native_message(stream) == payload


def test_dashboard_action(monkeypatch) -> None:
    monkeypatch.setattr(
        "nicheflow_studio.services.pool_capture.capture_dashboard",
        lambda: {"pools": {}, "apify_usage": {"used": 3}},
    )

    response = nicheflow_capture_host.handle_message({"action": "get_dashboard"})  # noqa: E501

    assert response["ok"] is True
    assert response["dashboard"]["apify_usage"]["used"] == 3


def test_batch_action(monkeypatch) -> None:
    monkeypatch.setattr(
        "nicheflow_studio.services.pool_capture.capture_instagram_reels_to_pool",  # noqa: E501
        lambda items: {"summary": {"queued": len(items)}},
    )

    response = nicheflow_capture_host.handle_message(
        {
            "action": "capture_batch",
            "items": [{"url": "https://instagram.com/reel/X/"}],
        }  # noqa: E501
    )

    assert response["ok"] is True
    assert response["batch"]["summary"]["queued"] == 1


def test_capture_action_without_pin_remains_backward_compatible(monkeypatch) -> None:
    captured = {}

    def fake_capture(url, *, niche, pinned_account_id=None):
        captured.update(url=url, niche=niche, pinned_account_id=pinned_account_id)
        return {"status": "added"}

    monkeypatch.setattr(
        "nicheflow_studio.services.pool_capture.capture_instagram_reel_to_pool",
        fake_capture,
    )

    response = nicheflow_capture_host.handle_message(
        {"action": "capture_to_pool", "url": "https://instagram.com/reel/X/"}
    )

    assert response["ok"] is True
    assert captured["pinned_account_id"] is None
