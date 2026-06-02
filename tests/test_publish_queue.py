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
            job_id=7,
            profile_name="main",
            video_path=Path("x.mp4"),
            cover_image_path=Path("x_cover.jpg"),
            caption="c",
            do_share=True,
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


def test_publish_worker_passes_cover_image_path(qt_app, monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        mw,
        "publish_reel",
        lambda *a, **k: calls.append((a, k)) or PublishResult("dry_run"),
    )
    worker = mw.PublishWorker(
        mw.PublishJobConfig(
            job_id=7,
            profile_name="main",
            video_path=Path("x.mp4"),
            cover_image_path=Path("x_cover.jpg"),
            caption="c",
            do_share=False,
        )
    )

    worker.run()

    assert calls == [
        (
            ("main", Path("x.mp4"), "c"),
            {"cover_image_path": Path("x_cover.jpg"), "do_share": False},
        )
    ]


def test_publish_completion_marks_duplicate_export_rows_posted(qt_app, tmp_path: Path) -> None:
    init_db()
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"processed")
    with get_session() as session:
        account = Account(
            name="Cinema Files Daily", platform="instagram", instagram_profile="cinema"
        )
        session.add(account)
        session.flush()
        primary = UploadJob(account_id=account.id, processed_path=str(video), status="ready")
        duplicate = UploadJob(account_id=account.id, processed_path=str(video), status="draft")
        session.add_all([primary, duplicate])
        session.commit()
        primary_id = primary.id
        duplicate_id = duplicate.id

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        window._on_publish_completed(
            {
                "job_id": primary_id,
                "status": "posted",
                "posted_url": "https://www.instagram.com/reel/current/",
                "error_message": None,
            }
        )

        with get_session() as session:
            jobs = {
                job.id: job
                for job in session.query(UploadJob)
                .filter(UploadJob.id.in_([primary_id, duplicate_id]))
                .all()
            }

        assert jobs[primary_id].status == "posted"
        assert jobs[duplicate_id].status == "posted"
        assert jobs[primary_id].posted_at is not None
        assert jobs[duplicate_id].posted_at is not None
        assert jobs[duplicate_id].posted_url == "https://www.instagram.com/reel/current/"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


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


def test_selected_future_scheduled_job_does_not_publish(qt_app, monkeypatch, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    future_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    with get_session() as session:
        job = UploadJob(
            account_id=account_id,
            processed_path=video,
            status="scheduled",
            scheduled_at=future_at,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    launched: list = []
    window = MainWindow()
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    window._confirm_publish_target = lambda _job_id: True  # type: ignore[method-assign]
    try:
        window.show()
        window._selected_schedule_job_id = lambda: job_id  # type: ignore[method-assign]

        window._on_publish_due_now_clicked()
        qt_app.processEvents()

        assert launched == []
        assert window._publish_batch_active is False
        with get_session() as session:
            saved = session.get(UploadJob, job_id)
            assert saved.status == "scheduled"
            assert saved.posted_at is None
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_all_due_batches_across_accounts(qt_app, tmp_path: Path) -> None:
    """'Publish All Due' queues due reels from MULTIPLE accounts and runs them
    sequentially: the first launches now, the rest wait in the queue."""
    init_db()
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"processed")
    with get_session() as session:
        acc_a = Account(name="History A", platform="instagram", instagram_profile="hista")
        acc_b = Account(name="History B", platform="instagram", instagram_profile="histb")
        session.add_all([acc_a, acc_b])
        session.flush()
        session.add_all(
            [
                UploadJob(account_id=acc_a.id, processed_path=str(video), status="ready"),
                UploadJob(account_id=acc_b.id, processed_path=str(video), status="ready"),
            ]
        )
        session.commit()

    launched: list = []
    window = MainWindow()
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    # Stub the confirmation dialog so the test doesn't block on a QMessageBox.
    window._confirm_publish_batch = lambda _ids: True  # type: ignore[method-assign]
    try:
        window.show()
        window._set_current_page("uploads")
        qt_app.processEvents()

        window._on_publish_all_due_clicked()
        qt_app.processEvents()

        assert window._publish_batch_active is True
        assert len(launched) == 1  # first account's reel launched immediately
        assert len(window._publish_batch_queue) == 1  # second account's reel waits for the gap
        assert window._schedule_publish_due_button.text() == "Stop Publishing"

        # The launched job and the queued job belong to different accounts.
        launched_profile = launched[0][1]
        assert launched_profile in {"hista", "histb"}

        window._on_publish_completed(
            {
                "job_id": launched[0][0],
                "status": "posted",
                "posted_url": "https://www.instagram.com/reel/test/",
                "error_message": None,
            }
        )
        assert window._publish_batch_gap_timer.isActive() is True
        assert window._publish_batch_countdown_timer.isActive() is True
        assert window._publish_batch_next_at is not None
        assert "Cooldown: next post in" in window._schedule_summary_label.text()

        window._stop_publish_batch(user_cancelled=True)
        assert window._publish_batch_gap_timer.isActive() is False
        assert window._publish_batch_countdown_timer.isActive() is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_all_due_uses_dashboard_daily_cap_bypass(qt_app, tmp_path: Path) -> None:
    init_db()
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"processed")
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    with get_session() as session:
        account = Account(name="Cinema Files Daily", platform="instagram", instagram_profile="cinema")
        session.add(account)
        session.flush()
        for _ in range(mw.PUBLISH_DAILY_CAP):
            session.add(
                UploadJob(
                    account_id=account.id,
                    processed_path=str(video),
                    status="posted",
                    posted_at=recent,
                )
            )
        session.add(UploadJob(account_id=account.id, processed_path=str(video), status="ready"))
        session.commit()

    launched: list = []
    window = MainWindow()
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    window._confirm_publish_batch = lambda _ids: True  # type: ignore[method-assign]
    try:
        window.show()
        window._open_session_health_dialog()
        window._publishing_dashboard_tabs.setCurrentIndex(1)
        qt_app.processEvents()

        window._global_publish_bypass_daily_cap_checkbox.setChecked(True)
        window._on_publish_all_due_clicked()
        qt_app.processEvents()

        assert len(launched) == 1
        assert window._publish_batch_active is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_publish_all_due_with_nothing_due_is_a_noop(qt_app, tmp_path: Path) -> None:
    init_db()
    _make_account_and_video(tmp_path)  # account exists but no due jobs
    launched: list = []
    window = MainWindow()
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    window._confirm_publish_batch = lambda _ids: True  # type: ignore[method-assign]
    try:
        window.show()
        qt_app.processEvents()
        window._on_publish_all_due_clicked()
        qt_app.processEvents()
        assert window._publish_batch_active is False
        assert launched == []
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_auto_publish_tick_is_noop_when_disabled(qt_app, tmp_path: Path) -> None:
    """With the toggle off, the due-check tick must never start a batch."""
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    with get_session() as session:
        session.add(UploadJob(account_id=account_id, processed_path=video, status="ready"))
        session.commit()

    window = MainWindow()
    window._confirm_publish_batch = lambda _ids: True  # type: ignore[method-assign]
    try:
        window.show()
        qt_app.processEvents()
        window._auto_publish_enabled_checkbox.setChecked(False)

        window._auto_publish_due_tick()
        assert window._publish_batch_active is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._due_check_timer.stop()
        window._hide_toast()
        window.close()


def test_auto_publish_tick_posts_due_reels_when_enabled(qt_app, tmp_path: Path) -> None:
    """One due reel + toggle on + nothing risky -> auto-starts the batch, no prompt."""
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    with get_session() as session:
        session.add(UploadJob(account_id=account_id, processed_path=video, status="ready"))
        session.commit()

    launched: list = []
    prompted: list = []
    window = MainWindow()
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    window._confirm_publish_batch = lambda _ids: prompted.append(_ids) or True  # type: ignore[method-assign]
    try:
        window.show()
        qt_app.processEvents()
        window._auto_publish_enabled_checkbox.setChecked(True)

        window._auto_publish_due_tick()
        qt_app.processEvents()

        assert window._publish_batch_active is True
        assert len(launched) == 1  # posted automatically
        assert prompted == []  # a single, well-formed reel is not "risky"
    finally:
        window._stop_publish_batch(user_cancelled=True)
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._due_check_timer.stop()
        window._hide_toast()
        window.close()


def test_auto_publish_tick_prompts_when_account_has_no_profile(qt_app, tmp_path: Path) -> None:
    """A due reel on a profile-less account is risky -> confirmation popup first."""
    init_db()
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"processed")
    with get_session() as session:
        account = Account(name="No Profile", platform="instagram", instagram_profile=None)
        session.add(account)
        session.flush()
        session.add(UploadJob(account_id=account.id, processed_path=str(video), status="ready"))
        session.commit()

    prompted: list = []
    window = MainWindow()
    # Decline the prompt so nothing posts; we only assert that it asked.
    window._confirm_publish_batch = lambda ids: prompted.append(ids) or False  # type: ignore[method-assign]
    try:
        window.show()
        qt_app.processEvents()
        window._auto_publish_enabled_checkbox.setChecked(True)

        window._auto_publish_due_tick()
        qt_app.processEvents()

        assert len(prompted) == 1  # risky run asked for confirmation
        assert window._publish_batch_active is False  # declined -> nothing posted

        # Same unchanged due set must not re-prompt on the next tick.
        window._auto_publish_due_tick()
        assert len(prompted) == 1
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
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
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


def test_daily_cap_resets_at_account_midnight(qt_app, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    yesterday = MainWindow._publish_day_start_utc("Asia/Bangkok") - dt.timedelta(minutes=1)
    with get_session() as session:
        account = session.get(Account, account_id)
        account.upload_timezone = "Asia/Bangkok"
        for _ in range(mw.PUBLISH_DAILY_CAP):
            session.add(
                UploadJob(
                    account_id=account_id,
                    processed_path=video,
                    status="posted",
                    posted_at=yesterday,
                )
            )
        draft = UploadJob(
            account_id=account_id, processed_path=video, description="c", status="draft"
        )
        session.add(draft)
        session.commit()
        draft_id = draft.id

    launched: list = []
    window = MainWindow()
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    try:
        window.show()
        qt_app.processEvents()
        _publish_with_selected_job(window, qt_app, draft_id)

        assert len(launched) == 1
        assert window._publish_in_progress is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_daily_cap_bypass_allows_manual_publish(qt_app, tmp_path: Path) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
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

    launched: list = []
    window = MainWindow()
    window._launch_publish_worker = lambda *a, **k: launched.append(a)  # type: ignore[method-assign]
    try:
        window.show()
        qt_app.processEvents()
        window._set_current_page("uploads")
        window._schedule_bypass_daily_cap_checkbox.setChecked(True)
        _publish_with_selected_job(window, qt_app, draft_id)

        assert len(launched) == 1
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


def test_apply_schedule_time_sets_time_and_promotes_status(qt_app, tmp_path: Path) -> None:
    """Setting a time on draft/ready reels flips them to 'scheduled'."""
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    with get_session() as session:
        session.add_all(
            [
                UploadJob(account_id=account_id, processed_path=video, status="draft"),
                UploadJob(account_id=account_id, processed_path=video, status="ready"),
            ]
        )
        session.commit()
        ids = [job.id for job in session.query(UploadJob).all()]

    when = dt.datetime(2026, 6, 2, 9, 0, tzinfo=dt.timezone.utc)
    window = MainWindow()
    try:
        updated = window._apply_schedule_time_to_jobs(ids, when)
        assert updated == 2
        with get_session() as session:
            for job in session.query(UploadJob).all():
                assert job.status == "scheduled"
                # SQLite stores naive datetimes; the wall-clock UTC value matches.
                stored = job.scheduled_at
                if stored.tzinfo is None:
                    stored = stored.replace(tzinfo=dt.timezone.utc)
                assert stored == when
    finally:
        window._refresh_timer.stop()
        window._due_check_timer.stop()
        window.close()


def test_clear_schedule_time_reverts_scheduled_to_draft(qt_app, tmp_path: Path) -> None:
    """Clearing a scheduled reel's time drops it back to draft and unschedules it."""
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    when = dt.datetime(2026, 6, 2, 9, 0, tzinfo=dt.timezone.utc)
    with get_session() as session:
        session.add(
            UploadJob(
                account_id=account_id, processed_path=video, status="scheduled", scheduled_at=when
            )
        )
        session.commit()
        job_id = session.query(UploadJob).one().id

    window = MainWindow()
    try:
        updated = window._clear_schedule_time_for_jobs([job_id])
        assert updated == 1
        with get_session() as session:
            job = session.get(UploadJob, job_id)
            assert job.scheduled_at is None
            assert job.status == "draft"
    finally:
        window._refresh_timer.stop()
        window._due_check_timer.stop()
        window.close()


def test_global_edit_schedule_button_enables_on_selection(qt_app, tmp_path: Path) -> None:
    """The new Edit Schedule button is disabled until a global row is selected."""
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    with get_session() as session:
        session.add(UploadJob(account_id=account_id, processed_path=video, status="ready"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("session_health")  # Publishing Dashboard
        window._refresh_global_publish_queue()
        qt_app.processEvents()
        assert window._global_publish_edit_schedule_button.isEnabled() is False

        window._global_publish_table.selectRow(0)
        qt_app.processEvents()
        assert window._global_publish_edit_schedule_button.isEnabled() is True
    finally:
        window._refresh_timer.stop()
        window._due_check_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_global_open_video_button_opens_selected_export(
    qt_app, monkeypatch, tmp_path: Path
) -> None:
    init_db()
    account_id, video = _make_account_and_video(tmp_path)
    opened_paths: list[str] = []
    monkeypatch.setattr("nicheflow_studio.app.main_window.os.startfile", opened_paths.append)
    with get_session() as session:
        session.add(UploadJob(account_id=account_id, processed_path=video, status="ready"))
        session.commit()

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("session_health")
        window._refresh_global_publish_queue()
        qt_app.processEvents()

        assert window._global_publish_open_output_button.isEnabled() is False
        window._global_publish_table.selectRow(0)
        qt_app.processEvents()

        assert window._global_publish_open_output_button.isEnabled() is True
        window._on_global_open_output_clicked()

        assert opened_paths == [video]
    finally:
        window._refresh_timer.stop()
        window._due_check_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_global_refresh_preserves_row_selection(qt_app, tmp_path: Path) -> None:
    """A periodic refresh must not clear the user's selected row(s)."""
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

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("session_health")
        window._refresh_global_publish_queue()
        qt_app.processEvents()

        window._global_publish_table.selectRow(0)
        qt_app.processEvents()
        selected_before = window._selected_global_publish_job_ids()
        assert selected_before  # sanity: something is selected

        # Simulate the periodic tick rebuilding the table.
        window._refresh_global_publish_queue()
        qt_app.processEvents()

        assert window._selected_global_publish_job_ids() == selected_before
    finally:
        window._refresh_timer.stop()
        window._due_check_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()


def test_global_table_height_is_bounded_when_empty(qt_app, tmp_path: Path) -> None:
    """An empty queue must not balloon the table into a full-page void."""
    init_db()
    _make_account_and_video(tmp_path)  # account exists, no publish jobs

    window = MainWindow()
    try:
        window.show()
        window._set_current_page("session_health")
        window._refresh_global_publish_queue()
        qt_app.processEvents()

        # setFixedHeight pins min == max == target; target is capped at 460.
        table = window._global_publish_table
        assert table.maximumHeight() <= 460
        assert table.minimumHeight() >= 120
    finally:
        window._refresh_timer.stop()
        window._due_check_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()
