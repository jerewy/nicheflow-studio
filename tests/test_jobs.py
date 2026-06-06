from __future__ import annotations

from nicheflow_studio.services.jobs import FAILED, SUCCEEDED, JobManager


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
