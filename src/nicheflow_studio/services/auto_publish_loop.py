"""App-lifetime background loop for publishing due scheduled reels."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from nicheflow_studio.services.publish_now import auto_publish_enabled, publish_due_jobs

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60.0
# Top-up the niche pools' distribution once at startup and then hourly: the
# assignment lifecycle frees backlog slots as posts go out, and this is what
# refills them without a manual "Auto-distribute" click.
_DEFAULT_TOP_UP_EVERY_TICKS = 60


def _default_top_up() -> list[dict]:
    # Lazy import: pooling pulls in the distribution stack, which the loop
    # shouldn't pay for at import time (mirrors the publish_reel seam).
    from nicheflow_studio.services.pooling import auto_top_up

    return auto_top_up()


class AutoPublishLoop:
    """Run the opt-in due-job publisher for the lifetime of the webview app."""

    def __init__(
        self,
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        enabled: Callable[[], bool] = auto_publish_enabled,
        publish: Callable[[], dict] = publish_due_jobs,
        top_up: Callable[[], list[dict]] = _default_top_up,
        top_up_every_ticks: int = _DEFAULT_TOP_UP_EVERY_TICKS,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._enabled = enabled
        self._publish = publish
        self._top_up = top_up
        self._top_up_every_ticks = max(1, top_up_every_ticks)
        self._ticks_since_top_up: int | None = None  # None -> top-up on first tick
        self._stop_event = threading.Event()
        self._tick_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start one daemon worker; repeated calls while running are a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="nicheflow-auto-publish",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        """Signal the worker to stop and wait briefly for an idle worker to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def tick(self) -> bool:
        """Run one due-job check, or skip it when another tick is still active."""
        if not self._tick_lock.acquire(blocking=False):
            logger.info("Skipping auto-publish tick because the previous tick is still running.")
            return False
        try:
            self._maybe_top_up()
            if not self._enabled():
                return True
            summary = self._publish()
            if summary.get("posted", 0) + summary.get("failed", 0) > 0:
                logger.info("Auto-publish summary: %s", summary)
            return True
        except Exception:
            logger.exception("Auto-publish tick failed.")
            return True
        finally:
            self._tick_lock.release()

    def _maybe_top_up(self) -> None:
        """Refill under-stocked niche distributions at startup and then hourly.

        Independent of the auto-publish toggle: topping up only creates
        pending-review items (no downloads, no posting), so it is always safe.
        """
        if self._ticks_since_top_up is not None:
            self._ticks_since_top_up += 1
            if self._ticks_since_top_up < self._top_up_every_ticks:
                return
        self._ticks_since_top_up = 0
        try:
            refilled = self._top_up()
            for result in refilled:
                if result.get("assigned", 0) > 0:
                    logger.info("Auto top-up distributed: %s", result)
        except Exception:
            logger.exception("Auto top-up failed; will retry on the next interval.")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.tick()
            if self._stop_event.wait(self._interval_seconds):
                return
