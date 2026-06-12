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
import datetime as dt
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
from nicheflow_studio.core.paths import logs_dir

log = logging.getLogger(__name__)

INSTAGRAM_URL = "https://www.instagram.com/"
_IGNORED_CHROMIUM_DEFAULT_ARGS = ("--enable-automation",)
_PUBLISH_CHROME_ARGS = ("--start-minimized",)

# --- Selectors. Instagram relabels/restructures these periodically; keep each
# step tolerant by listing several candidates. ----------------------------
_NEW_POST_SELECTORS = (
    'svg[aria-label="New post"]',
    'a[href="#"]:has(svg[aria-label="New post"])',
    '[role="button"]:has-text("Create")',
    'a:has-text("Create")',
)
# The "+" Create button opens a dropdown (Post / Reel / Story / Live) on the
# current web UI before the file-drop dialog; older layouts skip straight to it.
# We must pick "Post" specifically and never "Reel"/"Story", so prefer exact-text
# matches first. ``text-is`` is exact (avoids matching "Posts" etc.).
_POST_MENU_SELECTORS = (
    '[role="menuitem"]:text-is("Post")',
    'svg[aria-label="Post"]',
    '[role="menuitem"]:has-text("Post")',
    'div[role="button"]:text-is("Post")',
    'a[role="link"]:has-text("Post")',
    'span:text-is("Post")',
)
_SELECT_FROM_COMPUTER_SELECTORS = (
    'button:has-text("Select from computer")',
    '[role="button"]:has-text("Select from computer")',
    'button:has-text("Select From Computer")',
    'div[role="button"]:has-text("Select from computer")',
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
_COVER_EDIT_SELECTORS = (
    'div[role="button"]:has-text("Edit cover")',
    'button:has-text("Edit cover")',
    'div[role="button"]:has-text("Cover")',
    'button:has-text("Cover")',
)
_COVER_ADD_FROM_COMPUTER_SELECTORS = (
    'button:has-text("Add from computer")',
    'div[role="button"]:has-text("Add from computer")',
    'button:has-text("Upload from computer")',
    'div[role="button"]:has-text("Upload from computer")',
    'button:has-text("Select from computer")',
    'div[role="button"]:has-text("Select from computer")',
)
_COVER_DONE_SELECTORS = (
    'div[role="button"]:text-is("Done")',
    'button:text-is("Done")',
    'div[role="button"]:text-is("Apply")',
    'button:text-is("Apply")',
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
                        # Bounded click: the outer loop owns the deadline. The
                        # default 30s actionability wait can hang here when a
                        # modal overlay (first-post dialog) blocks the control.
                        await element.click(timeout=2500)
                        return True
                except PlaywrightError:
                    continue
        await page.wait_for_timeout(300)
    return False


async def _any_visible(page: Page, selectors: tuple[str, ...]) -> bool:
    """True if any selector has at least one visible match (no click)."""
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
        except PlaywrightError:
            continue
        for index in range(count):
            try:
                if await locator.nth(index).is_visible():
                    return True
            except PlaywrightError:
                continue
    return False


def _format_control_dump(labels: list[str]) -> str:
    """Compact, de-duplicated one-line summary of visible control labels.

    Pure so it is unit-testable; the async dumper feeds it the page's visible
    button/menuitem text when a composer step can't find its target.
    """
    seen: list[str] = []
    for raw in labels:
        text = " ".join(str(raw).split())
        if not text or len(text) > 60:
            continue
        if text not in seen:
            seen.append(text)
    return " | ".join(seen[:40]) or "(none)"


async def _dump_composer_state(page: Page, reason: str) -> None:
    """Save a screenshot + visible control labels when a composer step fails.

    This is the evidence trail for layout drift (e.g. the Create dropdown
    changing): a failed run leaves a PNG in data/logs and logs what Instagram
    actually rendered, so the exact selector fix can be made from ground truth.
    Never fatal.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        out_dir = logs_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        shot = out_dir / f"publish-{reason}-{stamp}.png"
        await page.screenshot(path=str(shot))
        log.warning("composer diagnostic screenshot saved: %s", shot)
    except Exception:  # noqa: BLE001 - diagnostics must never break publishing
        log.debug("could not capture composer screenshot", exc_info=True)
    try:
        labels = await page.eval_on_selector_all(
            'button, [role="button"], [role="menuitem"], a[role="link"]',
            "els => els.filter(e => e.offsetParent !== null)"
            ".map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())"
            ".filter(Boolean)",
        )
        log.warning("composer visible controls (%s): %s", reason, _format_control_dump(labels))
    except Exception:  # noqa: BLE001
        log.debug("could not enumerate composer controls", exc_info=True)


async def _ensure_file_drop_screen(page: Page) -> bool:
    """Make the 'Select from computer' button reachable.

    Handles both web layouts without branching on account type: if the upload
    button isn't visible yet, the Create dropdown (Post / Reel / Story / Live)
    is probably open, so click the 'Post' option and look again. Returns True
    once the upload button is visible.
    """
    for _ in range(3):
        if await _any_visible(page, _SELECT_FROM_COMPUTER_SELECTORS):
            return True
        # Pick "Post" from the Create dropdown (never Reel/Story). No-op if the
        # dropdown isn't there — then the next loop re-checks the upload button.
        await _click_first(page, _POST_MENU_SELECTORS, timeout=2500)
        await _human_pause(0.4, 0.9)
    return await _any_visible(page, _SELECT_FROM_COMPUTER_SELECTORS)


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


async def _click_first_dismissing(
    page: Page, selectors: tuple[str, ...], *, timeout: float = 45000
) -> bool:
    """:func:`_click_first` with interstitial sweeps between attempts.

    First-post dialogs ("Video posts are now shared as reels") can appear
    seconds AFTER the post-attach dismissal pass — Instagram reads the video
    before showing the dialog — and then sit over the composer while a plain
    ``_click_first`` burns its whole budget against a blocked control.
    Sweeping before each attempt clears the dialog whenever it shows up.
    """
    slice_ms = 5000.0
    deadline = time.monotonic() + timeout / 1000
    while True:
        await _dismiss_interstitials(page)
        remaining_ms = (deadline - time.monotonic()) * 1000
        if remaining_ms <= 0:
            return False
        if await _click_first(page, selectors, timeout=min(slice_ms, remaining_ms)):
            return True


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
    """Open the Create dialog and hand the video to the file chooser.

    Works for both personal and professional accounts: the difference between
    layouts is whether a Create dropdown sits in front of the file-drop screen,
    which :func:`_ensure_file_drop_screen` resolves. On failure we capture a
    diagnostic screenshot so the cause is visible rather than guessed.
    """
    if not await _click_first(page, _NEW_POST_SELECTORS):
        await _dump_composer_state(page, "new-post-button-missing")
        raise RuntimeError("could not find the 'New post' / Create button")
    await _human_pause(0.3, 0.8)
    if not await _ensure_file_drop_screen(page):
        await _dump_composer_state(page, "select-from-computer-missing")
        raise RuntimeError(
            "could not find 'Select from computer' (saved a diagnostic screenshot "
            "in data/logs); the composer layout may have changed"
        )
    async with page.expect_file_chooser() as fc_info:
        if not await _click_first(page, _SELECT_FROM_COMPUTER_SELECTORS):
            await _dump_composer_state(page, "select-from-computer-click-failed")
            raise RuntimeError("could not click 'Select from computer'")
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
    if not await _click_first_dismissing(page, _CROP_BUTTON_SELECTORS, timeout=45000):
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
        if not await _click_first_dismissing(page, _NEXT_SELECTORS, timeout=timeout_ms):
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


async def _set_reel_cover(page: Page, cover_image: Path | None) -> bool:
    """Best-effort upload of a custom Reel cover on the final composer screen."""
    if cover_image is None:
        return False
    if not await _click_first(page, _COVER_EDIT_SELECTORS, timeout=6000):
        log.warning("could not find Instagram cover editor; leaving auto cover")
        return False
    await _human_pause(0.4, 0.9)
    try:
        async with page.expect_file_chooser(timeout=7000) as fc_info:
            if not await _click_first(page, _COVER_ADD_FROM_COMPUTER_SELECTORS, timeout=7000):
                log.warning("could not find cover upload button; leaving auto cover")
                return False
        chooser = await fc_info.value
        await chooser.set_files(str(cover_image))
    except (PlaywrightError, PlaywrightTimeout) as exc:
        log.warning("could not upload Instagram cover image: %s", exc)
        return False
    await _human_pause(0.8, 1.5)
    if not await _click_first(page, _COVER_DONE_SELECTORS, timeout=7000):
        log.warning("could not confirm Instagram cover image; continuing with selected cover")
    else:
        await _human_pause(0.4, 0.9)
    log.info("set Instagram reel cover to %s", cover_image.name)
    return True


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
    cover_image: Path | None,
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
            # Stay non-headless (a real Chrome window is far less bot-like than
            # headless) and start minimized so it doesn't steal focus. We ignore
            # Chrome's default "--enable-automation" switch below to avoid the
            # automation banner, but do not add unsupported stealth flags because
            # Chrome surfaces them as a security warning.
            "args": list(_PUBLISH_CHROME_ARGS),
            # Playwright disables Chromium's sandbox by default. Explicitly keep
            # it enabled so headed publishing does not show Chrome's
            # unsupported --no-sandbox security warning.
            "chromium_sandbox": True,
            "ignore_default_args": list(_IGNORED_CHROMIUM_DEFAULT_ARGS),
        }
        if channel:  # real Chrome looks less like automation than bundled Chromium
            launch_kwargs["channel"] = channel
        try:
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        except PlaywrightError as exc:
            if not channel:
                return PublishResult("failed", error_message=f"could not launch browser: {exc}")
            log.warning(
                "channel '%s' unavailable (%s); falling back to bundled Chromium", channel, exc
            )
            launch_kwargs.pop("channel", None)
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)

        page = context.pages[0] if context.pages else await context.new_page()
        # Force-minimize through the DevTools protocol. A persistent Chrome profile
        # restores its saved window state and IGNORES --start-minimized, so the flag
        # alone doesn't keep the publish window out of the way — this does. Automation
        # (file upload, clicks) is CDP-driven and works fine while minimized. Cosmetic
        # only: never fail a publish if minimizing doesn't take.
        try:
            cdp_session = await context.new_cdp_session(page)
            window_info = await cdp_session.send("Browser.getWindowForTarget")
            await cdp_session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_info["windowId"],
                    "bounds": {"windowState": "minimized"},
                },
            )
        except Exception:  # noqa: BLE001 - minimizing is cosmetic
            log.debug("could not minimize publish window", exc_info=True)
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
            if cover_image is not None:
                log.info("setting custom cover %s", cover_image.name)
                await _set_reel_cover(page, cover_image)
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
    cover_image_path: str | Path | None = None,
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
    cover_image = Path(cover_image_path) if cover_image_path else None
    if cover_image is not None and not cover_image.exists():
        return PublishResult("failed", error_message=f"cover image not found: {cover_image}")

    profile = (profile_name or DEFAULT_PROFILE_NAME).strip() or DEFAULT_PROFILE_NAME
    result = asyncio.run(
        _publish_reel_async(
            profile_dir=session_profile_dir(profile),
            video=video_path,
            cover_image=cover_image,
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
