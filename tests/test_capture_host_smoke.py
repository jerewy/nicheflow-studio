from __future__ import annotations

import pytest

from scripts.smoke_capture_host import validate_dashboard_response


def test_smoke_requires_pool_account_targets() -> None:
    with pytest.raises(RuntimeError, match="missing account targets"):
        validate_dashboard_response(
            {
                "ok": True,
                "dashboard": {"pools": {"history": {"video_count": 10}}},
            }
        )


def test_smoke_accepts_current_dashboard_contract() -> None:
    validate_dashboard_response(
        {
            "ok": True,
            "dashboard": {
                "pools": {
                    "history": {
                        "video_count": 10,
                        "accounts": [{"id": 7, "name": "Past Moments Daily"}],
                    }
                }
            },
        }
    )
