"""Browser-driven Instagram Reel publisher.

Drives the Instagram web composer with Playwright using a saved per-account
profile (the same profiles the login/scraper scripts use). This is the engine
behind the app's auto-publish: ``publish_reel()`` posts one reel and returns a
structured :class:`PublishResult` the UI/worker can persist.

Design notes:
- Headed real Chrome + randomized human pauses keep the session looking like a
  person, not a bot. Callers MUST serialize calls (one browser at a time).
- Instagram's web DOM shifts often, so each step tries several candidate
  selectors and clicks the first VISIBLE match.
- Failures are returned as data (``PublishResult``), never raised to the caller,
  so a batch run can record the error and move on.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from nicheflow_studio.core.instagram_session import (
    DEFAULT_PROFILE_NAME,
    profile_dir as session_profile_dir,
)

log = logging.getLogger(__name__)

INSTAGRAM_URL = "https://www.instagram.com/"

# --- Selectors. Instagram relabels/restructures these periodically; keep each
# step tolerant by listing several candidates. ----------------------------
_NEW_POST_SELECTORS = (
    'svg[aria-label="New post"]',
    'a[href="#"]:has(svg[aria-label="New post"])',
    '[role="button"]:has-text("Create")',
    'a:has-text("Create")',
)
_POST_MENU_SELECTORS = (
    '[role="menuitem"]:has-text("Post")',
    'span:has-text("Post")',
)
_SELECT_FROM_COMPUTER_SELECTORS = (
    'button:has-text("Select from computer")',
    '[role="button"]:has-text("Select from computer")',
)
_NEXT_SELECTORS = (
    'div[role="button"]:has-text("Next")',
    'button:has-text("Next")',
)
# Crop/aspect expander on the first edit screen. IG defaults to a cropped
# ratio; we must open this and pick 9:16 or a vertical reel gets letterboxed.
_CROP_BUTTON_SELECTORS = (
    'svg[aria-label="Select crop"]',
    'button:has(svg[aria-label="Select crop"])',
    '[aria-label="Select crop"]',
)
_ASPECT_9_16_SELECTORS = (
    'span:text-is("9:16")',
    'div[role="button"]:has-text("9:16")',
    'span:has-text("9:16")',
    '[aria-label="9:16"]',
)
_SHARE_SELECTORS = (
    'div[role="button"]:text-is("Share")',
    'button:text-is("Share")',
    'div[role="button"]:has-text("Share")',
    'button:has-text("Share")',
)
_CAPTION_SELECTORS = (
    'div[aria-label="Write a caption..."][contenteditable="true"]',
    'textarea[aria-label="Write a caption..."]',
    'div[contenteditable="true"][role="textbox"]',
)
_SHARED_CONFIRMATION_TEXT = (
    "Your reel has been shared",
    "Your post has been shared",
    "Reel shared",
    "Post shared",
)
_DISMISS_SELECTORS = (
    'button:has-text("Allow all cookies")',
    'button:has-text("Not Now")',
    'button:has-text("Not now")',
    'button:has-text("OK")',
    'div[role="button"]:has-text("OK")',
)
# Text that means Instagram flagged the session — stop posting on this account.
_CHECKPOINT_MARKERS = (
    "suspicious login attempt",
    "we restrict certain activity",
    "we restricted",
    "help us confirm",
    "confirm it's you",
    "please wait a few minutes",
    "try again later",
    "your account has been disabled",
    "we detected unusual activity",
    "automated behavior",
)


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a single publish attempt.

    status:
        ``posted``     - reel shared and confirmed.
        ``dry_run``    - reached Share with do_share=False (no post made).
        ``failed``     - a step failed (see error_message).
        ``checkpoint`` - Instagram flagged the session; caller should cool down.
    """

    status: str
    posted_url: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"posted", "dry_run"}

    @property
    def is_checkpoint(self) -> bool:
        return self.status == "checkpoint"


async def _human_pause(min_s: float = 0.6, max_s: float = 1.8) -> None:
    """Sleep a randomized, human-ish amount. Constant timing is an automation tell."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _has_session(context) -> bool:  # noqa: ANN001
    cookies = await context.cookies(INSTAGRAM_URL)
    return any(c.get("name") == "sessionid" and c.get("value") for c in cookies)


async def _click_first(page: Page, selectors: tuple[str, ...], *, timeout: float = 8000) -> bool:
    """Click the first VISIBLE match across all selectors.

    Iterates every match of each selector (not just ``.first``): Instagram often
    renders a hidden element ahead of the real control in DOM order, so keying on
    ``.first`` alone silently skips the visible button.
    """
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except PlaywrightError:
                continue
            for index in range(count):
                element = locator.nth(index)
                try:
                    if await element.is_visible():
                        await _human_pause(0.3, 0.9)
                        await element.click()
                        return True
                except PlaywrightError:
                    continue
        await page.wait_for_timeout(300)
    return False


async def _dismiss_interstitials(page: Page) -> None:
    """Best-effort close of cookie banners / 'not now' prompts; never fatal."""
    for selector in _DISMISS_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible():
                await locator.click()
                await _human_pause(0.4, 1.0)
        except PlaywrightError:
            continue


async def _detect_checkpoint(page: Page) -> str | None:
    """Return the matched marker text if Instagram is showing a challenge/restriction."""
    for marker in _CHECKPOINT_MARKERS:
        try:
            if await page.get_by_text(marker, exact=False).first.is_visible():
                return marker
        except PlaywrightError:
            continue
    return None


async def _open_composer_and_attach(page: Page, video: Path) -> None:
    """Open the Create dialog and hand the video to the file chooser."""
    if not await _click_first(page, _NEW_POST_SELECTORS):
        raise RuntimeError("could not find the 'New post' / Create button")
    await _human_pause(0.3, 0.8)
    # The Post/Reel submenu only appears on some IG layouts; modern "Create new
    # post" goes straight to the file-drop screen. When it's absent this probe
    # would otherwise burn its whole timeout doing nothing — keep it short so we
    # don't sit on the composer for seconds. When present, it shows in <1s.
    await _click_first(page, _POST_MENU_SELECTORS, timeout=1000)
    await _human_pause(0.3, 0.8)
    async with page.expect_file_chooser() as fc_info:
        if not await _click_first(page, _SELECT_FROM_COMPUTER_SELECTORS):
            raise RuntimeError("could not find 'Select from computer'")
    chooser = await fc_info.value
    await chooser.set_files(str(video))
    await _human_pause(0.5, 1.0)
    await _dismiss_interstitials(page)  # "Video posts are now shared as reels" -> OK


async def _select_9_16_aspect(page: Page) -> None:
    """Expand the crop control and choose 9:16. Warns (not fatal) if absent.

    Large videos can take 30-60 s to process in Instagram's browser uploader
    before the crop screen appears — use a generous timeout so we don't give up
    while the upload is still in progress.
    """
    if not await _click_first(page, _CROP_BUTTON_SELECTORS, timeout=45000):
        log.warning("could not find crop control; leaving aspect as-is")
        return
    await _human_pause(0.3, 0.7)
    if not await _click_first(page, _ASPECT_9_16_SELECTORS, timeout=5000):
        log.warning("could not find 9:16 option; leaving aspect as-is")
        return
    log.info("set aspect ratio to 9:16")
    await _human_pause(0.3, 0.8)


async def _advance_through_edit_steps(page: Page) -> None:
    """Set 9:16 on the crop screen, then click through crop -> filter screens.

    The first 'Next' click (crop → filter) may need to wait a long time because
    Instagram processes the video in the browser before the button becomes active.
    Give it the same generous budget as the crop-control search; the second 'Next'
    (filter → caption) is just a UI transition and is fast.
    """
    await _select_9_16_aspect(page)
    timeouts = [60000, 15000]  # crop-screen Next can be slow; filter-screen Next is fast
    for timeout_ms in timeouts:
        if not await _click_first(page, _NEXT_SELECTORS, timeout=timeout_ms):
            raise RuntimeError("could not advance with 'Next' during edit steps")
        await _human_pause(0.6, 1.4)


async def _fill_caption(page: Page, caption: str) -> None:
    if not caption:
        return
    for selector in _CAPTION_SELECTORS:
        box = page.locator(selector).first
        try:
            if await box.is_visible():
                await box.click()
                await _human_pause(0.4, 1.0)
                # Insert the whole caption at once (paste-like) instead of typing
                # per-key: far faster for long captions and preserves newlines.
                await page.keyboard.insert_text(caption)
                await _human_pause()
                return
        except PlaywrightError:
            continue
    raise RuntimeError("could not find the caption field")


async def _wait_for_share_confirmation(page: Page, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for text in _SHARED_CONFIRMATION_TEXT:
            try:
                if await page.get_by_text(text, exact=False).first.is_visible():
                    return True
            except PlaywrightError:
                pass
        await page.wait_for_timeout(1000)
    return False


async def _capture_posted_url(page: Page) -> str | None:
    """Best-effort grab of the new reel's permalink from the confirmation dialog.

    Keep this scoped to the success dialog. The Instagram home shell can contain
    old feed/profile links; grabbing the first page-wide ``/reel/`` or ``/p/``
    anchor can record the wrong URL for a successful upload.
    """
    dialog = None
    for text in _SHARED_CONFIRMATION_TEXT:
        try:
            matches = page.locator('[role="dialog"]').filter(has_text=text)
            if await matches.count():
                dialog = matches.first
                break
        except PlaywrightError:
            continue
    if dialog is None:
        return None

    for selector in ('a[href*="/reel/"]', 'a[href*="/p/"]'):
        try:
            href = await dialog.locator(selector).first.get_attribute("href", timeout=2500)
        except PlaywrightError:
            continue
        if href:
            return INSTAGRAM_URL.rstrip("/") + href if href.startswith("/") else href
    return None


async def _idle_until_closed(context) -> None:  # noqa: ANN001
    try:
        while context.pages:
            await asyncio.sleep(1)
    except PlaywrightError:
        return


async def _publish_reel_async(
    *,
    profile_dir: Path,
    video: Path,
    caption: str,
    do_share: bool,
    keep_open: bool,
    channel: str | None,
    upload_timeout_s: float,
    capture_url: bool,
) -> PublishResult:
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        launch_kwargs: dict = {
            "user_data_dir": str(profile_dir.resolve()),
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
        }
        if channel:  # real Chrome looks less like automation than bundled Chromium
            launch_kwargs["channel"] = channel
        try:
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        except PlaywrightError as exc:
            if not channel:
                return PublishResult("failed", error_message=f"could not launch browser: {exc}")
            log.warning("channel '%s' unavailable (%s); falling back to bundled Chromium", channel, exc)
            launch_kwargs.pop("channel", None)
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)

        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(INSTAGRAM_URL, wait_until="domcontentloaded", timeout=60000)
            if not await _has_session(context):
                return PublishResult(
                    "failed", error_message="not logged in (no sessionid); re-login required"
                )
            await _human_pause(0.5, 1.0)
            await _dismiss_interstitials(page)

            marker = await _detect_checkpoint(page)
            if marker:
                return PublishResult("checkpoint", error_message=f"checkpoint detected: {marker}")

            log.info("opening composer and attaching %s", video.name)
            await _open_composer_and_attach(page, video)
            log.info("advancing through edit steps")
            await _advance_through_edit_steps(page)
            log.info("filling caption")
            await _fill_caption(page, caption)
            await _human_pause(1.0, 2.0)

            if not do_share:
                if keep_open:
                    await _idle_until_closed(context)
                return PublishResult("dry_run")

            log.info("sharing")
            if not await _click_first(page, _SHARE_SELECTORS, timeout=10000):
                raise RuntimeError("could not find the 'Share' button")

            if not await _wait_for_share_confirmation(page, upload_timeout_s):
                marker = await _detect_checkpoint(page)
                if marker:
                    return PublishResult(
                        "checkpoint", error_message=f"checkpoint detected after share: {marker}"
                    )
                return PublishResult(
                    "failed",
                    error_message=f"no share confirmation within {upload_timeout_s:.0f}s",
                )

            posted_url = await _capture_posted_url(page) if capture_url else None
            await _human_pause(4.0, 6.0)
            return PublishResult("posted", posted_url=posted_url)

        except (RuntimeError, PlaywrightTimeout) as exc:
            if keep_open:
                await _idle_until_closed(context)
            return PublishResult("failed", error_message=str(exc))
        finally:
            try:
                await context.close()
            except PlaywrightError:
                pass


def publish_reel(
    profile_name: str,
    video: str | Path,
    caption: str,
    *,
    do_share: bool = True,
    channel: str | None = "chrome",
    upload_timeout_s: float = 300.0,
    keep_open: bool = False,
    capture_url: bool = True,
) -> PublishResult:
    """Publish a single reel as the account behind ``profile_name``.

    Synchronous: runs its own asyncio loop, so it is safe to call from a Qt
    worker thread. Never raises for flow failures — inspect the returned
    :class:`PublishResult`. Callers MUST NOT run two of these concurrently.
    """
    video_path = Path(video)
    if not video_path.exists():
        return PublishResult("failed", error_message=f"video not found: {video_path}")

    profile = (profile_name or DEFAULT_PROFILE_NAME).strip() or DEFAULT_PROFILE_NAME
    result = asyncio.run(
        _publish_reel_async(
            profile_dir=session_profile_dir(profile),
            video=video_path,
            caption=caption or "",
            do_share=do_share,
            keep_open=keep_open,
            channel=channel,
            upload_timeout_s=upload_timeout_s,
            capture_url=capture_url,
        )
    )
    log.info(
        "publish_reel profile=%s status=%s url=%s error=%s",
        profile,
        result.status,
        result.posted_url,
        result.error_message,
    )
    return result
