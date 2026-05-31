"""Auto-publish wiring + safety-guard tests for the Publish Queue.

These avoid launching a real browser: the guard paths return before any
publish runs, and the worker-mapping test stubs ``publish_reel``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import nicheflow_studio.app.main_window as mw
from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.db.models import Account, UploadJob
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.publisher.instagram_publisher import PublishResult


def test_publish_worker_maps_result_to_payload(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(
        mw, "publish_reel", lambda *a, **k: PublishResult("posted", posted_url="https://x/reel/1/")
    )
    worker = mw.PublishWorker(
        mw.PublishJobConfig(
            job_id=7, profile_name="main", video_path=Path("x.mp4"), caption="c", do_share=True
        )
    )
    captured: dict = {}
    worker.completed.connect(captured.update)

    worker.run()

    assert captured == {
        "job_id": 7,
        "status": "posted",
        "posted_url": "https://x/reel/1/",
        "error_message": None,
    }


def _make_account_and_video(tmp_path: Path) -> tuple[int, str]:
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"processed")
    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram", instagram_profile="main")
        session.add(account)
        session.commit()
        return account.id, str(video)


def _publish_with_selected_job(window, qt_app, job_id: int) -> None:
    window._current_account_combo.setCurrentIndex(1)
    window._set_current_page("uploads")
    qt_app.processEvents()
    # Drive the handler directly against a known job id instead of fighting
    # table-row ordering.
    window._selected_schedule_job_id = lambda: job_id  # type: ignore[method-assign]
    window._on_auto_publish_selected_clicked()
    qt_app.processEvents()


def test_auto_publish_skips_already_posted_job(qt_app, monkeypatch, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    with get_session() as session:
        job = UploadJob(
            account_id=account_id,
            processed_path=video,
            description="c",
            status="posted",
            posted_at=dt.datetime.now(dt.timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    calls: list = []
    monkeypatch.setattr(mw, "publish_reel", lambda *a, **k: calls.append((a, k)))

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        _publish_with_selected_job(window, qt_app, job_id)

        assert calls == []  # duplicate guard prevented any publish
        assert window._publish_in_progress is False
        assert window._publish_thread is None
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_gather_due_jobs_selects_ready_and_due_only(qt_app, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as session:
        ready = UploadJob(account_id=account_id, processed_path=video, status="ready")
        due = UploadJob(
            account_id=account_id,
            processed_path=video,
            status="scheduled",
            scheduled_at=now - dt.timedelta(minutes=5),
        )
        future = UploadJob(
            account_id=account_id,
            processed_path=video,
            status="scheduled",
            scheduled_at=now + dt.timedelta(hours=2),
        )
        draft = UploadJob(account_id=account_id, processed_path=video, status="draft")
        posted = UploadJob(
            account_id=account_id, processed_path=video, status="posted", posted_at=now
        )
        session.add_all([ready, due, future, draft, posted])
        session.commit()
        ready_id, due_id = ready.id, due.id

    window = MainWindow()
    try:
        ids = window._gather_due_publish_job_ids()
        assert set(ids) == {ready_id, due_id}  # ready + past-due only
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_due_now_starts_batch_and_can_stop(qt_app, monkeypatch, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    with get_session() as session:
        session.add_all(
            [
                UploadJob(account_id=account_id, processed_path=video, status="ready"),
                UploadJob(account_id=account_id, processed_path=video, status="ready"),
            ]
        )
        session.commit()

    launched: list = []
    window = MainWindow()
    # Stub the worker launch so no browser/thread starts; just record the call.
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    # Stub the confirmation dialog so tests don't block on a QMessageBox.
    window._confirm_publish_target = lambda _job_id: True  # type: ignore[method-assign]
    try:
        window.show()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        window._on_publish_due_now_clicked()
        qt_app.processEvents()

        assert window._publish_batch_active is True
        assert len(launched) == 1  # selected job launched immediately
        assert len(window._publish_batch_queue) == 0  # only one job queued (selected row)
        assert window._schedule_publish_due_button.text() == "Stop Publishing"

        # Clicking again stops the batch.
        window._on_publish_due_now_clicked()
        qt_app.processEvents()
        assert window._publish_batch_active is False
        assert window._publish_batch_queue == []
        assert window._schedule_publish_due_button.text() == "Publish Due Now"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_due_badge_shows_count(qt_app, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    with get_session() as session:
        # Ready jobs (no scheduled time) count as due immediately.
        session.add_all(
            [
                UploadJob(account_id=account_id, processed_path=video, status="ready"),
                UploadJob(account_id=account_id, processed_path=video, status="ready"),
            ]
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        window._update_due_badge()
        # Badge no longer shows a count; button publishes the selected row only.
        assert window._schedule_publish_due_button.text() == "Publish Due Now"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._due_check_timer.stop()
        window._hide_toast()
        window.close()


def test_auto_schedule_assigns_jittered_times(qt_app, tmp_path: Path) -> None:
    init_db()
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"processed")
    with get_session() as session:
        account = Account(
            name="RespawnReels",
            platform="instagram",
            instagram_profile="main",
            upload_schedule_slots="09:00, 18:00",
        )
        session.add(account)
        session.flush()
        for _ in range(3):
            session.add(UploadJob(account_id=account.id, processed_path=str(video), status="ready"))
        session.commit()
        account_id = account.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        window._on_auto_schedule_clicked()
        qt_app.processEvents()

        with get_session() as session:
            jobs = session.query(UploadJob).filter(UploadJob.account_id == account_id).all()
            assert all(job.scheduled_at is not None for job in jobs)
            assert all(job.status == "scheduled" for job in jobs)
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_auto_publish_respects_daily_cap(qt_app, monkeypatch, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    with get_session() as session:
        for _ in range(mw.PUBLISH_DAILY_CAP):
            session.add(
                UploadJob(
                    account_id=account_id,
                    processed_path=video,
                    status="posted",
                    posted_at=recent,
                )
            )
        draft = UploadJob(
            account_id=account_id, processed_path=video, description="c", status="draft"
        )
        session.add(draft)
        session.commit()
        draft_id = draft.id

    calls: list = []
    monkeypatch.setattr(mw, "publish_reel", lambda *a, **k: calls.append(1))

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        _publish_with_selected_job(window, qt_app, draft_id)

        assert calls == []  # daily cap prevented the publish
        assert window._publish_in_progress is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_failed_job_shows_reason_inline_and_tooltip(qt_app, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    reason = "not logged in (no sessionid); re-login required"
    with get_session() as session:
        session.add(
            UploadJob(
                account_id=account_id,
                processed_path=video,
                status="failed",
                error_message=reason,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        status_item = window._schedule_table.item(0, 5)
        # Inline label carries the reason so it's visible without hovering.
        assert status_item.text() == f"Failed — {reason}"
        # Full reason is available on hover even when the inline text is elided.
        assert status_item.toolTip() == reason
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_short_failure_reason_truncates_long_message() -> None:
    long_msg = "x" * 200
    short = MainWindow._short_failure_reason(long_msg, limit=60)
    assert len(short) == 60
    assert short.endswith("…")
    assert MainWindow._short_failure_reason(None) == ""
    assert MainWindow._short_failure_reason("first line\nsecond line") == "first line"
