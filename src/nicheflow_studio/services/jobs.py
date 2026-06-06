"""Minimal in-process background-job manager (UI-independent).

The migration's bridge calls must return quickly, but generation/export/publish
work is slow. This manager runs such work on a daemon thread and exposes a
JSON-serializable status snapshot the React UI can poll
(``docs/UI_MIGRATION_PLAN.md`` "Background Job Contract").

It is deliberately tiny: start a callable, poll its status, read the result.
There is no persistence — jobs live for the process lifetime, which is all the
poll-based UI needs. Anything richer (cancellation, queues) is deferred until a
real need appears.
"""

from __future__ import annotations

import inspect
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Job status values.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


def _accepts_progress(func: Callable[..., Any]) -> bool:
    """True if ``func`` declares a ``progress`` parameter (so we can inject one)."""
    try:
        return "progress" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


@dataclass
class Job:
    id: str
    status: str = PENDING
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str | None = None

    def snapshot(self) -> dict:
        """JSON-serializable view for the bridge/UI."""
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    """Runs callables on daemon threads and tracks their status thread-safely."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def start(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        """Run ``func(*args, **kwargs)`` in the background; return the job id.

        The callable's return value becomes the job ``result`` (it should be
        JSON-serializable so the bridge can return it). Any exception is captured
        as the job ``error`` with status ``failed`` — it never propagates.

        If ``func`` declares a ``progress`` parameter, a ``report(fraction,
        message="")`` callback is injected so long jobs can update their status
        as they run. Callables without that parameter are called unchanged.
        """
        job = Job(id=uuid.uuid4().hex)
        with self._lock:
            self._jobs[job.id] = job

        def report(fraction: float, message: str = "") -> None:
            with self._lock:
                job.progress = max(0.0, min(1.0, float(fraction)))
                if message:
                    job.message = message

        call_kwargs = dict(kwargs)
        if _accepts_progress(func):
            call_kwargs["progress"] = report

        def _run() -> None:
            with self._lock:
                job.status = RUNNING
            try:
                result = func(*args, **call_kwargs)
            except Exception as exc:  # noqa: BLE001 - boundary: record, don't crash the thread
                logger.exception("Background job %s failed", job.id)
                with self._lock:
                    job.error = str(exc)
                    job.status = FAILED
                return
            with self._lock:
                job.result = result
                job.status = SUCCEEDED

        thread = threading.Thread(target=_run, name=f"job-{job.id}", daemon=True)
        with self._lock:
            self._threads[job.id] = thread
        thread.start()
        return job.id

    def get(self, job_id: str) -> dict | None:
        """Return the job's status snapshot, or ``None`` if the id is unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job is not None else None

    def join(self, job_id: str, timeout: float = 5.0) -> dict | None:
        """Block until a job finishes (or ``timeout``); return its snapshot.

        Intended for tests and synchronous callers; the UI polls ``get`` instead.
        """
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(job_id)
