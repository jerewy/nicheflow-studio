from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nicheflow_studio.core.instagram_profile_pool import AUTO_PROFILE, Profile
from nicheflow_studio.core import instagram_session
from nicheflow_studio.scraper import instagram as scraper_module


_SCRAPE_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "instagram_scrape_urls.py"
_SCRAPE_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "instagram_scrape_urls_script", _SCRAPE_SCRIPT_PATH
)
assert _SCRAPE_SCRIPT_SPEC is not None and _SCRAPE_SCRIPT_SPEC.loader is not None
scrape_script = importlib.util.module_from_spec(_SCRAPE_SCRIPT_SPEC)
_SCRAPE_SCRIPT_SPEC.loader.exec_module(scrape_script)


class _FakePost:
    def __init__(self, shortcode: str) -> None:
        self.shortcode = shortcode
        self.is_video = True
        self.caption = f"caption {shortcode}"
        self.date_utc = None
        self.owner_username = "creator"
        self.video_view_count = 1000
        self.video_play_count = None
        self.likes = 50
        self.comments = 5
        self.video_duration = 12
        self.url = "https://example.com/thumb.jpg"


class _ScriptedInstaloader:
    """Yield a sequence of outcomes for sequential Post.from_shortcode calls."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    @property
    def Post(self):
        owner = self

        class _Post:
            @staticmethod
            def from_shortcode(_context, shortcode: str):
                owner.calls.append(shortcode)
                if not owner._outcomes:
                    raise RuntimeError("no scripted outcome left")
                outcome = owner._outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return _Post


@pytest.fixture
def fast_jitter(monkeypatch):
    monkeypatch.setattr(scraper_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(scraper_module.random, "uniform", lambda *_a, **_k: 0.0)


@pytest.fixture
def stub_loader(monkeypatch):
    def fake_build(_profile_name: str):
        return MagicMock(name=f"loader-{_profile_name}"), True

    monkeypatch.setattr(scraper_module, "_build_loader_for_profile", fake_build)


def test_rate_limit_switches_to_next_profile_and_retries_url(
    tmp_path, fast_jitter, stub_loader, monkeypatch
) -> None:
    fake_post = _FakePost("aaa111")
    scripted = _ScriptedInstaloader([RuntimeError("429 Too Many Requests"), fake_post])
    monkeypatch.setattr(scraper_module, "instaloader", scripted)

    pool = MagicMock()
    retry_path = tmp_path / "retry_queue.json"

    candidates, stats, _ = scraper_module.scrape_instagram_urls_instaloader(
        ["https://www.instagram.com/reel/aaa111/"],
        profile_names=["main", "alt1"],
        pool=pool,
        retry_queue_path=retry_path,
    )

    assert len(candidates) == 1
    assert candidates[0].video_id == "aaa111"
    assert stats.failed_rate_limited == 0  # the switch absorbed the rate limit
    assert stats.extracted == 1
    pool.mark_rate_limited.assert_called_once_with("main")
    assert not retry_path.exists()  # nothing queued — second profile saved it


def test_all_profiles_rate_limited_queues_url_for_retry(
    tmp_path, fast_jitter, stub_loader, monkeypatch
) -> None:
    scripted = _ScriptedInstaloader(
        [RuntimeError("429"), RuntimeError("rate limit"), RuntimeError("login required")]
    )
    monkeypatch.setattr(scraper_module, "instaloader", scripted)

    retry_path = tmp_path / "retry_queue.json"
    candidates, stats, _ = scraper_module.scrape_instagram_urls_instaloader(
        ["https://www.instagram.com/reel/bbb222/"],
        profile_names=["main", "alt1", "alt2"],
        retry_queue_path=retry_path,
    )

    assert candidates == []
    assert stats.failed_rate_limited == 1
    queued = json.loads(retry_path.read_text(encoding="utf-8"))
    assert len(queued) == 1
    assert queued[0]["url"] == "https://www.instagram.com/reel/bbb222/"
    assert queued[0]["reason"] == "rate_limited_all_profiles"


def test_instagram_please_wait_401_is_treated_as_rate_limit() -> None:
    error = RuntimeError(
        'JSON Query to graphql/query: 401 Unauthorized - "fail" status, '
        'message "Please wait a few minutes before you try again."'
    )

    assert scraper_module._classify_instaloader_error(error) == "rate_limit"


def test_per_profile_budget_rotates_after_quota(
    fast_jitter, stub_loader, monkeypatch
) -> None:
    posts = [_FakePost("c1"), _FakePost("c2"), _FakePost("c3")]
    scripted = _ScriptedInstaloader(posts)
    monkeypatch.setattr(scraper_module, "instaloader", scripted)

    # Track which loader was used per call via _build_loader_for_profile
    seen_profiles: list[str] = []

    def fake_build(profile_name: str):
        seen_profiles.append(profile_name)
        return MagicMock(), True

    monkeypatch.setattr(scraper_module, "_build_loader_for_profile", fake_build)

    candidates, stats, _ = scraper_module.scrape_instagram_urls_instaloader(
        [
            "https://www.instagram.com/reel/c1/",
            "https://www.instagram.com/reel/c2/",
            "https://www.instagram.com/reel/c3/",
        ],
        profile_names=["main", "alt1"],
        per_profile_budget=2,
    )

    assert len(candidates) == 3
    assert stats.extracted == 3
    # Built loader for 'main' first, then switched to 'alt1' after hitting budget=2.
    assert seen_profiles == ["main", "alt1"]


def test_consecutive_failures_rotate_to_next_profile(
    tmp_path, fast_jitter, monkeypatch
) -> None:
    seen_profiles: list[str] = []

    def fake_build(profile_name: str):
        seen_profiles.append(profile_name)
        return MagicMock(), True

    monkeypatch.setattr(scraper_module, "_build_loader_for_profile", fake_build)
    scripted = _ScriptedInstaloader(
        [
            RuntimeError("not found"),
            RuntimeError("not found"),
            RuntimeError("not found"),
            _FakePost("ok1"),
        ]
    )
    monkeypatch.setattr(scraper_module, "instaloader", scripted)

    candidates, _stats, _ = scraper_module.scrape_instagram_urls_instaloader(
        [
            "https://www.instagram.com/reel/x1/",
            "https://www.instagram.com/reel/x2/",
            "https://www.instagram.com/reel/x3/",
            "https://www.instagram.com/reel/ok1/",
        ],
        profile_names=["main", "alt1"],
        retry_queue_path=tmp_path / "retry.json",
    )

    assert len(candidates) == 1
    assert seen_profiles == ["main", "alt1"]


def test_single_profile_mode_keeps_existing_behavior(
    fast_jitter, stub_loader, monkeypatch
) -> None:
    scripted = _ScriptedInstaloader([_FakePost("solo1")])
    monkeypatch.setattr(scraper_module, "instaloader", scripted)

    candidates, stats, session_used = scraper_module.scrape_instagram_urls_instaloader(
        ["https://www.instagram.com/reel/solo1/"],
    )

    assert len(candidates) == 1
    assert stats.extracted == 1
    assert session_used is True


def test_auto_profile_resolution_sorts_never_used_and_used_profiles(monkeypatch) -> None:
    first = Profile(name="alt2")
    used = Profile(
        name="alt1",
        last_used=scrape_script.dt.datetime(2026, 5, 23, tzinfo=scrape_script.dt.timezone.utc),
    )
    unused = Profile(name="main")

    class FakePool:
        def pick(self):
            return first

        def available(self):
            return [used, first, unused]

    monkeypatch.setattr(scrape_script.ProfilePool, "load", lambda: FakePool())

    assert scrape_script._resolve_profiles(AUTO_PROFILE) == ["alt2", "main", "alt1"]


# ---------------------------------------------------------------------------
# Adaptive per-request delay based on profile failure_count
# ---------------------------------------------------------------------------


def test_adaptive_delay_multiplier_zero_failures_is_one() -> None:
    """A clean profile uses the base delay band, no multiplier."""
    assert scraper_module._adaptive_delay_multiplier(0) == 1.0


def test_adaptive_delay_multiplier_grows_with_failure_count() -> None:
    """Each prior 429 multiplies the next delay by 1.5x, so a profile that
    just came out of cooldown doesn't immediately crash back into a 429.
    Capped at 3 failures (~3.375x) to keep delays under ~30s/request."""
    assert scraper_module._adaptive_delay_multiplier(1) == pytest.approx(1.5)
    assert scraper_module._adaptive_delay_multiplier(2) == pytest.approx(2.25)
    assert scraper_module._adaptive_delay_multiplier(3) == pytest.approx(3.375)
    # Cap holds: 10 failures still produces the 3-failure multiplier.
    assert scraper_module._adaptive_delay_multiplier(10) == pytest.approx(3.375)


def test_adaptive_delay_multiplier_negative_failures_treated_as_zero() -> None:
    """Defensive: a corrupted/negative failure_count should not produce a
    delay shorter than the base band."""
    assert scraper_module._adaptive_delay_multiplier(-5) == 1.0
