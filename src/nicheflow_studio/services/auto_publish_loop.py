"""App-lifetime background loop for publishing due scheduled reels."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from nicheflow_studio.services.publish_now import auto_publish_enabled, publish_due_jobs

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60.0


class AutoPublishLoop:
    """Run the opt-in due-job publisher for the lifetime of the webview app."""

    def __init__(
        self,
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        enabled: Callable[[], bool] = auto_publish_enabled,
        publish: Callable[[], dict] = publish_due_jobs,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._enabled = enabled
        self._publish = publish
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

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.tick()
            if self._stop_event.wait(self._interval_seconds):
                return
