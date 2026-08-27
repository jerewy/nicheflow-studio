from __future__ import annotations

import json
import threading
from pathlib import Path

from nicheflow_studio.services.jobs import (
    CANCELED,
    FAILED,
    SUCCEEDED,
    JobCanceled,
    JobManager,
)


def test_job_runs_and_stores_result() -> None:
    manager = JobManager()
    job_id = manager.start(lambda x, y: x + y, 2, 3)

    snapshot = manager.join(job_id)

    assert snapshot is not None
    assert snapshot["status"] == SUCCEEDED
    assert snapshot["result"] == 5
    assert snapshot["error"] is None


def test_job_captures_exception_as_failure() -> None:
    manager = JobManager()

    def boom() -> None:
        raise ValueError("kaboom")

    job_id = manager.start(boom)
    snapshot = manager.join(job_id)

    assert snapshot is not None
    assert snapshot["status"] == FAILED
    assert "kaboom" in snapshot["error"]
    assert snapshot["result"] is None


def test_get_unknown_job_returns_none() -> None:
    manager = JobManager()
    assert manager.get("does-not-exist") is None


def test_kwargs_are_passed_through() -> None:
    manager = JobManager()
    job_id = manager.start(lambda *, a, b: a * b, a=4, b=5)
    snapshot = manager.join(job_id)
    assert snapshot["result"] == 20


def test_job_injects_progress_callback_when_declared() -> None:
    manager = JobManager()

    def work(progress) -> str:
        progress(0.5, "halfway")
        return "ok"

    job_id = manager.start(work)
    snapshot = manager.join(job_id)

    assert snapshot["status"] == SUCCEEDED
    assert snapshot["result"] == "ok"
    assert snapshot["progress"] == 0.5
    assert snapshot["message"] == "halfway"


def test_job_without_progress_param_runs_unchanged() -> None:
    manager = JobManager()
    job_id = manager.start(lambda: 7)
    snapshot = manager.join(job_id)
    assert snapshot["result"] == 7
    assert snapshot["progress"] == 0.0


def test_cooperative_job_cancels_when_it_observes_the_event() -> None:
    manager = JobManager()
    started = threading.Event()
    release = threading.Event()

    def work(cancel_event: threading.Event) -> str:
        started.set()
        release.wait(2.0)
        if cancel_event.is_set():
            raise JobCanceled("stopped by user")
        return "done"

    job_id = manager.start(work)
    assert started.wait(2.0)
    # Cancel while the job is still running, then let it reach its cancel check.
    assert manager.cancel(job_id) is True
    release.set()

    snapshot = manager.join(job_id)
    assert snapshot is not None
    assert snapshot["status"] == CANCELED
    assert snapshot["cancel_requested"] is True
    assert snapshot["error"] is None


def test_cancel_returns_false_for_unknown_or_finished_job() -> None:
    manager = JobManager()
    assert manager.cancel("does-not-exist") is False

    job_id = manager.start(lambda: 1)
    manager.join(job_id)
    # Already succeeded — nothing left to cancel.
    assert manager.cancel(job_id) is False


def test_job_without_cancel_event_param_runs_unchanged() -> None:
    # A non-cooperative callable never sees the event and completes normally.
    manager = JobManager()
    job_id = manager.start(lambda: 9)
    snapshot = manager.join(job_id)
    assert snapshot["status"] == SUCCEEDED
    assert snapshot["result"] == 9


def test_snapshot_serializes_a_path_result() -> None:
    """A job that writes a file naturally returns a Path.

    Left raw, that raised inside pywebview's own JSON serializer *after* the work
    had succeeded, so every Clip Studio card reported "Render failed" with no
    error message while the finished file sat on disk.
    """
    manager = JobManager()
    job_id = manager.start(lambda: Path("C:/out/clip.mp4"))

    snapshot = manager.join(job_id)

    assert snapshot is not None
    assert snapshot["status"] == SUCCEEDED
    assert isinstance(snapshot["result"], str)
    # The actual requirement: it survives the serializer that broke.
    json.dumps(snapshot)


def test_snapshot_serializes_paths_nested_in_a_result() -> None:
    manager = JobManager()
    job_id = manager.start(lambda: {"video": Path("a.mp4"), "extras": [Path("b.srt")]})

    snapshot = manager.join(job_id)

    assert snapshot is not None
    json.dumps(snapshot)
    assert isinstance(snapshot["result"]["video"], str)
    assert isinstance(snapshot["result"]["extras"][0], str)
