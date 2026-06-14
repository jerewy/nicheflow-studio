import asyncio
from pathlib import Path

import pytest

from nicheflow_studio.publisher import instagram_publisher
from nicheflow_studio.publisher.instagram_publisher import (
    _BASE_VIEWPORT,
    _IGNORED_CHROMIUM_DEFAULT_ARGS,
    _POST_MENU_SELECTORS,
    _PUBLISH_CHROME_ARGS,
    _SELECT_FROM_COMPUTER_SELECTORS,
    PublishResult,
    _capture_posted_url,
    _format_control_dump,
    _human_delay_seconds,
    _profile_viewport,
    publish_reel,
)


def test_publish_result_ok_is_true_for_posted_and_dry_run() -> None:
    assert PublishResult("posted").ok is True
    assert PublishResult("dry_run").ok is True
    assert PublishResult("failed", error_message="x").ok is False
    assert PublishResult("checkpoint", error_message="x").ok is False


def test_publish_result_flags_checkpoint() -> None:
    assert PublishResult("checkpoint", error_message="suspicious").is_checkpoint is True
    assert PublishResult("posted").is_checkpoint is False


def test_publish_browser_only_ignores_automation_default_arg() -> None:
    assert _IGNORED_CHROMIUM_DEFAULT_ARGS == ("--enable-automation",)


def test_publish_browser_args_do_not_include_unsupported_stealth_flags() -> None:
    assert _PUBLISH_CHROME_ARGS == ("--start-minimized",)
    assert "--disable-blink-features=AutomationControlled" not in _PUBLISH_CHROME_ARGS


def test_publish_reel_fails_fast_when_video_missing(tmp_path: Path) -> None:
    # Missing file must short-circuit BEFORE any browser launch, so this stays
    # a pure unit test (no Playwright, no network).
    missing = tmp_path / "nope.mp4"

    result = publish_reel("main", missing, "caption")

    assert result.status == "failed"
    assert result.ok is False
    assert "video not found" in (result.error_message or "")


def test_publish_reel_fails_fast_when_cover_missing(tmp_path: Path) -> None:
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"video")

    result = publish_reel("main", video, "caption", cover_image_path=tmp_path / "missing.jpg")

    assert result.status == "failed"
    assert result.ok is False
    assert "cover image not found" in (result.error_message or "")


class _FakeLinkLocator:
    def __init__(self, href: str | None) -> None:
        self._href = href

    @property
    def first(self):
        return self

    async def get_attribute(self, _name: str, timeout: int | None = None) -> str | None:
        return self._href


class _FakeDialog:
    def __init__(self, href: str | None) -> None:
        self._href = href

    @property
    def first(self):
        return self

    def locator(self, _selector: str) -> _FakeLinkLocator:
        return _FakeLinkLocator(self._href)


class _FakeDialogMatches:
    def __init__(self, href: str | None, count: int) -> None:
        self._dialog = _FakeDialog(href)
        self._count = count

    def filter(self, *, has_text: str):
        return self

    async def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self._dialog


class _FakePage:
    def __init__(self, *, dialog_href: str | None, dialog_count: int) -> None:
        self._dialog_href = dialog_href
        self._dialog_count = dialog_count

    def locator(self, selector: str):
        if selector == '[role="dialog"]':
            return _FakeDialogMatches(self._dialog_href, self._dialog_count)
        return _FakeLinkLocator("https://www.instagram.com/reel/stale/")


def test_click_first_dismissing_sweeps_then_clicks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The first-post dialog ("Video posts are now shared as reels") blocks the
    # first click attempt; the sweep between attempts must clear it so the
    # second attempt lands.
    calls: list[str] = []

    async def fake_dismiss(page) -> None:  # noqa: ANN001
        calls.append("dismiss")

    async def fake_click(page, selectors, *, timeout) -> bool:  # noqa: ANN001
        calls.append("click")
        return calls.count("click") >= 2

    monkeypatch.setattr(instagram_publisher, "_dismiss_interstitials", fake_dismiss)
    monkeypatch.setattr(instagram_publisher, "_click_first", fake_click)

    clicked = asyncio.run(
        instagram_publisher._click_first_dismissing(object(), ("sel",), timeout=30000)
    )

    assert clicked is True
    assert calls == ["dismiss", "click", "dismiss", "click"]


def test_click_first_dismissing_gives_up_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_dismiss(page) -> None:  # noqa: ANN001
        calls.append("dismiss")

    async def fake_click(page, selectors, *, timeout) -> bool:  # noqa: ANN001
        calls.append("click")
        await asyncio.sleep(0.05)  # consume the budget so the deadline passes
        return False

    monkeypatch.setattr(instagram_publisher, "_dismiss_interstitials", fake_dismiss)
    monkeypatch.setattr(instagram_publisher, "_click_first", fake_click)

    clicked = asyncio.run(
        instagram_publisher._click_first_dismissing(object(), ("sel",), timeout=40)
    )

    assert clicked is False
    assert "click" in calls  # it tried before giving up


class _FakeBootPage:
    """Page stub for the composer-open step: records reload() calls."""

    def __init__(self) -> None:
        self.reloads = 0

    async def reload(self, **_kwargs) -> None:
        self.reloads += 1


def test_open_composer_waits_for_slow_app_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # goto() returns at domcontentloaded while Instagram can still be on its
    # logo splash; the New post button must be awaited with the generous
    # dismissing budget, not _click_first's 8s default (which failed live with
    # "could not find the 'New post' / Create button" on a laggy load). If the
    # page never renders (blank white SPA), reload once before giving up.
    captured: dict = {"attempts": []}

    async def fake_dismissing(page, selectors, *, timeout) -> bool:  # noqa: ANN001
        captured["attempts"].append((selectors, timeout))
        return False  # never renders, even after the reload

    async def fake_dump(page, reason) -> None:  # noqa: ANN001
        captured["dump"] = reason

    monkeypatch.setattr(instagram_publisher, "_click_first_dismissing", fake_dismissing)
    monkeypatch.setattr(instagram_publisher, "_dump_composer_state", fake_dump)
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)
    page = _FakeBootPage()

    with pytest.raises(RuntimeError, match="New post"):
        asyncio.run(instagram_publisher._open_composer_and_attach(page, Path("x.mp4")))

    assert page.reloads == 1
    assert len(captured["attempts"]) == 2  # before and after the reload
    for selectors, timeout in captured["attempts"]:
        assert selectors == instagram_publisher._NEW_POST_SELECTORS
        assert timeout >= 60000


def test_open_composer_recovers_after_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    # The blank-page reload is a recovery path: when the app shell renders
    # after the reload, the flow must continue instead of failing.
    state = {"calls": 0}

    class _StopFlow(Exception):
        pass

    async def fake_dismissing(page, selectors, *, timeout) -> bool:  # noqa: ANN001
        state["calls"] += 1
        return state["calls"] >= 2  # blank before reload, renders after

    async def fake_drop_screen(page, **_kwargs) -> bool:  # noqa: ANN001
        raise _StopFlow  # made it past the New post step

    monkeypatch.setattr(instagram_publisher, "_click_first_dismissing", fake_dismissing)
    monkeypatch.setattr(instagram_publisher, "_ensure_file_drop_screen", fake_drop_screen)
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)
    page = _FakeBootPage()

    with pytest.raises(_StopFlow):
        asyncio.run(instagram_publisher._open_composer_and_attach(page, Path("x.mp4")))

    assert page.reloads == 1
    assert state["calls"] == 2


async def _instant(*_args, **_kwargs) -> None:
    return None


def test_ensure_file_drop_screen_waits_for_lazy_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Create dialog lazy-loads its content (a spinner on slow connections);
    # the old fixed 3-attempt loop gave up in ~10s and failed live with
    # "could not find 'Select from computer'". The poll must keep checking.
    checks = {"count": 0}

    async def fake_any_visible(page, selectors) -> bool:  # noqa: ANN001
        checks["count"] += 1
        return checks["count"] >= 3  # dialog content appears on the 3rd check

    async def fake_click(page, selectors, *, timeout) -> bool:  # noqa: ANN001
        return False  # no Create dropdown in this layout

    monkeypatch.setattr(instagram_publisher, "_any_visible", fake_any_visible)
    monkeypatch.setattr(instagram_publisher, "_click_first", fake_click)
    monkeypatch.setattr(instagram_publisher, "_dismiss_interstitials", _instant)
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)

    found = asyncio.run(instagram_publisher._ensure_file_drop_screen(object()))

    assert found is True
    assert checks["count"] == 3


def test_ensure_file_drop_screen_gives_up_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_any_visible(page, selectors) -> bool:  # noqa: ANN001
        return False

    monkeypatch.setattr(instagram_publisher, "_any_visible", fake_any_visible)
    monkeypatch.setattr(instagram_publisher, "_dismiss_interstitials", _instant)
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)

    found = asyncio.run(
        instagram_publisher._ensure_file_drop_screen(object(), timeout=0)
    )

    assert found is False


class _FakeCaptionBox:
    def __init__(self, visible_after_checks: int) -> None:
        self.visible_after_checks = visible_after_checks
        self.checks = 0
        self.clicked = False

    @property
    def first(self):
        return self

    async def is_visible(self) -> bool:
        self.checks += 1
        return self.checks >= self.visible_after_checks

    async def click(self) -> None:
        self.clicked = True


class _FakeKeyboard:
    def __init__(self) -> None:
        self.inserted: str | None = None

    async def insert_text(self, text: str) -> None:
        self.inserted = text


class _FakeCaptionPage:
    def __init__(self, visible_after_checks: int) -> None:
        self.box = _FakeCaptionBox(visible_after_checks)
        self.keyboard = _FakeKeyboard()

    def locator(self, _selector: str) -> _FakeCaptionBox:
        return self.box


def test_fill_caption_waits_for_slow_caption_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The caption box renders late on a slow machine; a single selector pass
    # failed live with "could not find the caption field". The poll must retry
    # until it appears.
    monkeypatch.setattr(instagram_publisher, "_dismiss_interstitials", _instant)
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)
    # All 3 selectors miss on the first pass; the box shows up on the next one.
    page = _FakeCaptionPage(visible_after_checks=4)

    asyncio.run(instagram_publisher._fill_caption(page, "the caption"))

    assert page.box.clicked is True
    assert page.keyboard.inserted == "the caption"


def test_fill_caption_gives_up_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instagram_publisher, "_dismiss_interstitials", _instant)
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)
    page = _FakeCaptionPage(visible_after_checks=10**9)

    with pytest.raises(RuntimeError, match="caption field"):
        asyncio.run(instagram_publisher._fill_caption(page, "the caption", timeout=0))


def test_format_control_dump_dedupes_and_trims() -> None:
    labels = [
        "  Select   from computer ",  # whitespace collapses
        "Post",
        "Post",  # duplicate dropped
        "",  # empty dropped
        "x" * 70,  # over-long dropped
        "Next",
    ]

    dump = _format_control_dump(labels)

    assert dump == "Select from computer | Post | Next"


def test_format_control_dump_empty_is_marked() -> None:
    assert _format_control_dump([]) == "(none)"


def test_post_menu_prefers_exact_post_over_substring() -> None:
    # Exact-text match must come first so the Create dropdown's "Post" is picked
    # and never "Reel"/"Story"; substring fallbacks come after.
    assert _POST_MENU_SELECTORS[0] == '[role="menuitem"]:text-is("Post")'
    assert any("Select from computer" in sel for sel in _SELECT_FROM_COMPUTER_SELECTORS)


def test_capture_posted_url_ignores_page_wide_stale_links() -> None:
    page = _FakePage(dialog_href=None, dialog_count=0)

    assert asyncio.run(_capture_posted_url(page)) is None


def test_capture_posted_url_uses_confirmation_dialog_link() -> None:
    page = _FakePage(dialog_href="/reel/current/", dialog_count=1)

    assert asyncio.run(_capture_posted_url(page)) == "https://www.instagram.com/reel/current/"


class _SeqRandom:
    """Deterministic RNG stub: pops from fixed sequences for random()/uniform()."""

    def __init__(self, randoms: list[float], uniforms: list[float] | None = None) -> None:
        self._randoms = list(randoms)
        self._uniforms = list(uniforms or [])

    def random(self) -> float:
        return self._randoms.pop(0)

    def uniform(self, a: float, b: float) -> float:
        return self._uniforms.pop(0) if self._uniforms else (a + b) / 2


def test_human_delay_stays_within_window_without_long_pause() -> None:
    # First random() drives the base sample; second (>=0.08) skips the rare
    # long-dwell branch, so the delay must sit at the window edges.
    assert _human_delay_seconds(0.6, 1.8, rng=_SeqRandom([0.0, 1.0])) == pytest.approx(0.6)
    assert _human_delay_seconds(0.6, 1.8, rng=_SeqRandom([1.0, 1.0])) == pytest.approx(1.8)


def test_human_delay_is_skewed_toward_the_low_end() -> None:
    # A flat uniform at u=0.5 would land at the midpoint (1.2); the u**1.7 skew
    # must pull a mid sample noticeably below it (more short pauses than long).
    midpoint = 0.6 + (1.8 - 0.6) * 0.5
    delay = _human_delay_seconds(0.6, 1.8, rng=_SeqRandom([0.5, 1.0]))
    assert delay < midpoint


def test_human_delay_occasionally_adds_a_thinking_pause() -> None:
    # Second random() < 0.08 triggers the longer dwell on top of the base sample.
    base = _human_delay_seconds(0.6, 1.8, rng=_SeqRandom([0.5, 1.0]))
    longer = _human_delay_seconds(0.6, 1.8, rng=_SeqRandom([0.5, 0.0], uniforms=[0.7]))
    assert longer == pytest.approx(base + 0.7)


def test_profile_viewport_is_deterministic_per_account() -> None:
    # Stable per account, run to run: the same profile must always map to the
    # same window size (string-seeded Random is process-independent).
    assert _profile_viewport("gaming_alt_3") == _profile_viewport("gaming_alt_3")


def test_profile_viewport_varies_across_accounts_within_bounds() -> None:
    # Different accounts should not all share one identical viewport (that is the
    # cross-account correlation tell), but each must stay near the base size.
    keys = ["main", "gaming_alt_1", "gaming_alt_2", "gaming_alt_3", "history_a"]
    sizes = {tuple(_profile_viewport(k).values()) for k in keys}
    assert len(sizes) > 1
    for key in keys:
        vp = _profile_viewport(key)
        assert _BASE_VIEWPORT[0] - 40 <= vp["width"] <= _BASE_VIEWPORT[0] + 60
        assert _BASE_VIEWPORT[1] - 30 <= vp["height"] <= _BASE_VIEWPORT[1] + 50


class _FakeMouse:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.wheels: list[tuple[int, int]] = []
        self._raise = raise_error

    async def wheel(self, dx: int, dy: int) -> None:
        if self._raise:
            raise instagram_publisher.PlaywrightError("wheel blew up")
        self.wheels.append((dx, dy))


class _FakeFeedPage:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.mouse = _FakeMouse(raise_error=raise_error)


def test_warm_up_feed_scrolls_down_then_back_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # Warm-up should skim the feed (>=1 scroll down) and end heading back up,
    # never scroll the composer. Last wheel delta must be negative (upward).
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)
    page = _FakeFeedPage()

    asyncio.run(instagram_publisher._warm_up_feed(page))

    assert len(page.mouse.wheels) >= 2
    assert page.mouse.wheels[0][1] > 0  # first scroll goes down
    assert page.mouse.wheels[-1][1] < 0  # ends scrolling back up


def test_warm_up_feed_swallows_playwright_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # A warm-up is cosmetic; a wheel/PlaywrightError must never break a publish.
    monkeypatch.setattr(instagram_publisher, "_human_pause", _instant)
    page = _FakeFeedPage(raise_error=True)

    asyncio.run(instagram_publisher._warm_up_feed(page))  # must not raise
