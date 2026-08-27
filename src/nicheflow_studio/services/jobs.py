"""Minimal in-process background-job manager (UI-independent).

The migration's bridge calls must return quickly, but generation/export/publish
work is slow. This manager runs such work on a daemon thread and exposes a
JSON-serializable status snapshot the React UI can poll
(``docs/UI_MIGRATION_PLAN.md`` "Background Job Contract").

It is deliberately tiny: start a callable, poll its status, read the result.
There is no persistence — jobs live for the process lifetime, which is all the
poll-based UI needs. Queues are still deferred until a real need appears.

Cancellation is cooperative: :meth:`JobManager.cancel` sets a per-job
``threading.Event``, and a callable that declares a ``cancel_event`` parameter is
handed that event so it can poll it and bail out (e.g. the export job kills its
FFmpeg subprocess). Callables that don't declare it simply run to completion.
"""

from __future__ import annotations

import inspect
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Job status values.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELED = "canceled"


class JobCanceled(Exception):
    """Raised by a cooperative job when it observes its ``cancel_event`` is set.

    The manager treats this as a clean ``CANCELED`` outcome rather than a
    ``FAILED`` one, so a user-requested cancel never surfaces as an error.
    """


def _accepts_param(func: Callable[..., Any], name: str) -> bool:
    """True if ``func`` declares a parameter ``name`` (so we can inject it)."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def _jsonable(value: Any) -> Any:
    """Coerce a job result into something ``json.dumps`` accepts.

    Only ``Path`` needs handling today, but the failure it caused deserves the
    general guard: a job returning one raised ``TypeError: Object of type
    WindowsPath is not JSON serializable`` *inside pywebview's own serializer*,
    after the work had already succeeded. The UI saw a failed call with no error
    message and no way to tell that the render itself was fine.

    Paths are the natural return value for anything that writes a file, so this
    belongs here rather than at each call site — ``snapshot`` is what promises
    the result is serializable.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass
class Job:
    id: str
    status: str = PENDING
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str | None = None
    # Set by JobManager.cancel(); cooperative jobs poll it and raise JobCanceled.
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        """JSON-serializable view for the bridge/UI."""
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "result": _jsonable(self.result),
            "error": self.error,
            "cancel_requested": self.cancel_event.is_set(),
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
        if _accepts_param(func, "progress"):
            call_kwargs["progress"] = report
        if _accepts_param(func, "cancel_event"):
            call_kwargs["cancel_event"] = job.cancel_event

        def _run() -> None:
            with self._lock:
                job.status = RUNNING
            try:
                result = func(*args, **call_kwargs)
            except JobCanceled as exc:
                # A user-requested cancel is a clean outcome, not a failure.
                with self._lock:
                    job.message = str(exc) or "Canceled."
                    job.status = CANCELED
                return
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

    def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation of a job.

        Sets the job's ``cancel_event`` so a cooperative callable can bail out.
        Returns ``True`` when the flag was set on a still-active job, ``False``
        when the id is unknown or the job already finished. Best-effort: a job
        that doesn't poll the event runs to completion regardless.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in (SUCCEEDED, FAILED, CANCELED):
                return False
            job.cancel_event.set()
            return True

    def join(self, job_id: str, timeout: float = 5.0) -> dict | None:
        """Block until a job finishes (or ``timeout``); return its snapshot.

        Intended for tests and synchronous callers; the UI polls ``get`` instead.
        """
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(job_id)
