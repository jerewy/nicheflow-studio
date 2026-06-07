import asyncio
from pathlib import Path

from nicheflow_studio.publisher.instagram_publisher import (
    _IGNORED_CHROMIUM_DEFAULT_ARGS,
    PublishResult,
    _capture_posted_url,
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


def test_capture_posted_url_ignores_page_wide_stale_links() -> None:
    page = _FakePage(dialog_href=None, dialog_count=0)

    assert asyncio.run(_capture_posted_url(page)) is None


def test_capture_posted_url_uses_confirmation_dialog_link() -> None:
    page = _FakePage(dialog_href="/reel/current/", dialog_count=1)

    assert asyncio.run(_capture_posted_url(page)) == "https://www.instagram.com/reel/current/"
