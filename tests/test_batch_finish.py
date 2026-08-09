from __future__ import annotations

import pytest

from nicheflow_studio.db.models import Account, DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import batch_finish, draft_revisions, export as export_svc
from nicheflow_studio.services.batch_finish import BatchFinishError
from nicheflow_studio.services.jobs import JobCanceled


def _make_account(*, name: str, auto_schedule: bool = False) -> int:
    with get_session() as session:
        account = Account(
            name=name,
            platform="instagram",
            niche="history",
            niche_label="history",
            auto_schedule_on_export=auto_schedule,
        )
        session.add(account)
        session.commit()
        return account.id


def _make_item(account_id: int, *, title: str = "clip") -> int:
    with get_session() as session:
        item = DownloadItem(
            source_url=f"https://instagram.com/reel/{title}",
            title=title,
            file_path=f"C:/clips/{title}.mp4",
            account_id=account_id,
            status="downloaded",
        )
        session.add(item)
        session.commit()
        return item.id


def _give_revision(item_id: int, *, recommended: int | None = 2) -> None:
    draft_revisions.save_revision(
        item_id,
        title_options=["First hook", "Second hook", "Third hook"],
        caption_options=["One shared caption."],
        recommended_title_index=recommended,
        recommended_caption_index=recommended,
    )


def test_plan_batch_reports_recommended_option_and_blockers() -> None:
    account_id = _make_account(name="Plan History", auto_schedule=True)
    ready = _make_item(account_id, title="ready")
    blocked = _make_item(account_id, title="blocked")
    _give_revision(ready, recommended=3)

    plan = batch_finish.plan_batch([ready, blocked])
    by_item = {entry["item_id"]: entry for entry in plan}

    assert by_item[ready]["ready"] is True
    assert by_item[ready]["option"] == 3
    assert by_item[ready]["title"] == "Third hook"
    assert by_item[ready]["auto_schedules"] is True
    assert by_item[blocked]["ready"] is False
    assert "no draft revision" in by_item[blocked]["reason"]


def test_plan_batch_carries_the_per_account_number() -> None:
    """Results views name a reel by the same "#N" as Processing.

    Without it they fell back to the raw DownloadItem id whenever the candidate
    list had been refreshed away, so the same reel appeared under two different
    numbers depending on which screen you were on.
    """
    from nicheflow_studio.db.session import get_session
    from nicheflow_studio.services import library

    account_id = _make_account(name="Seq History")
    first = _make_item(account_id, title="first")
    second = _make_item(account_id, title="second")

    plan = batch_finish.plan_batch([first, second])
    by_item = {entry["item_id"]: entry for entry in plan}

    with get_session() as session:
        expected = library.account_sequence_map(session)
    assert by_item[first]["account_seq"] == expected[first]
    assert by_item[second]["account_seq"] == expected[second]


def test_plan_batch_flags_which_reels_publish_via_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirm dialog has to say WHERE a scheduled post runs from.

    "Scheduled to post" alone read as local-only, which is the opposite of what
    a cloud-mapped account does — and the difference matters, because a local
    schedule only fires while the app is running.
    """
    from nicheflow_studio.services import cloud_publisher

    cloud_account = _make_account(name="Cloud History", auto_schedule=True)
    local_account = _make_account(name="Local History", auto_schedule=True)
    cloud_item = _make_item(cloud_account, title="cloud")
    local_item = _make_item(local_account, title="local")
    _give_revision(cloud_item)
    _give_revision(local_item)

    monkeypatch.setattr(cloud_publisher, "is_configured", lambda: True)
    monkeypatch.setattr(
        cloud_publisher,
        "cloud_account_key_for",
        lambda account_id: "mapped" if account_id == cloud_account else None,
    )

    by_item = {e["item_id"]: e for e in batch_finish.plan_batch([cloud_item, local_item])}
    assert by_item[cloud_item]["publishes_via_cloud"] is True
    assert by_item[local_item]["publishes_via_cloud"] is False


def test_plan_batch_never_claims_cloud_when_the_worker_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale account map must not promise a cloud post that cannot happen."""
    from nicheflow_studio.services import cloud_publisher

    account_id = _make_account(name="Unconfigured History", auto_schedule=True)
    item_id = _make_item(account_id, title="reel")
    _give_revision(item_id)

    monkeypatch.setattr(cloud_publisher, "is_configured", lambda: False)
    monkeypatch.setattr(cloud_publisher, "cloud_account_key_for", lambda account_id: "mapped")

    plan = batch_finish.plan_batch([item_id])
    assert plan[0]["publishes_via_cloud"] is False


def test_plan_batch_falls_back_to_option_one_without_a_recommendation() -> None:
    account_id = _make_account(name="Fallback History")
    item_id = _make_item(account_id, title="norec")
    _give_revision(item_id, recommended=None)

    (entry,) = batch_finish.plan_batch([item_id])

    assert entry["option"] == 1


def test_finish_batch_applies_then_exports_each_ready_reel(monkeypatch) -> None:
    account_id = _make_account(name="Finish History")
    first = _make_item(account_id, title="one")
    second = _make_item(account_id, title="two")
    _give_revision(first, recommended=2)
    _give_revision(second, recommended=1)

    exported: list[int] = []

    def fake_export(item_id: int, **kwargs):
        exported.append(item_id)
        return {"item_id": item_id, "processed_path": f"C:/out/{item_id}.mp4"}

    monkeypatch.setattr(export_svc, "export_item", fake_export)

    result = batch_finish.finish_batch([first, second])

    assert exported == [first, second]
    assert [row["item_id"] for row in result["exported"]] == [first, second]
    assert result["failed"] == []
    with get_session() as session:
        assert session.get(DownloadItem, first).title_draft == "Second hook"
        assert session.get(DownloadItem, second).title_draft == "First hook"


def test_finish_batch_records_the_schedule_when_the_account_opts_in(monkeypatch) -> None:
    # export_item auto-schedules for opted-in accounts; finish_batch must report
    # that rather than scheduling a second time itself.
    account_id = _make_account(name="Scheduling History", auto_schedule=True)
    item_id = _make_item(account_id, title="sched")
    _give_revision(item_id)

    monkeypatch.setattr(
        export_svc,
        "export_item",
        lambda item_id, **kwargs: {
            "item_id": item_id,
            "processed_path": "C:/out/x.mp4",
            "scheduled_publish": {"scheduled_at": "2026-08-04T09:00:00+00:00"},
        },
    )

    result = batch_finish.finish_batch([item_id])

    assert [row["item_id"] for row in result["scheduled"]] == [item_id]
    assert result["failed"] == []


def test_finish_batch_surfaces_a_scheduling_warning_as_a_failure(monkeypatch) -> None:
    account_id = _make_account(name="Warned History", auto_schedule=True)
    item_id = _make_item(account_id, title="warned")
    _give_revision(item_id)

    monkeypatch.setattr(
        export_svc,
        "export_item",
        lambda item_id, **kwargs: {
            "item_id": item_id,
            "processed_path": "C:/out/x.mp4",
            "warning": "No open slot within the safety window.",
        },
    )

    result = batch_finish.finish_batch([item_id])

    assert result["exported"]  # the render itself succeeded
    assert result["failed"][0]["stage"] == "schedule"
    assert "No open slot" in result["failed"][0]["error"]


def test_finish_batch_keeps_going_after_one_export_fails(monkeypatch) -> None:
    # One bad clip must not strand the other thirty-five.
    account_id = _make_account(name="Resilient History")
    first = _make_item(account_id, title="bad")
    second = _make_item(account_id, title="good")
    _give_revision(first)
    _give_revision(second)

    def flaky_export(item_id: int, **kwargs):
        if item_id == first:
            raise RuntimeError("ffmpeg blew up")
        return {"item_id": item_id, "processed_path": "C:/out/ok.mp4"}

    monkeypatch.setattr(export_svc, "export_item", flaky_export)

    result = batch_finish.finish_batch([first, second])

    assert [row["item_id"] for row in result["exported"]] == [second]
    assert result["failed"][0]["item_id"] == first
    assert result["failed"][0]["stage"] == "export"


def test_finish_batch_propagates_cancellation(monkeypatch) -> None:
    import threading

    account_id = _make_account(name="Canceled History")
    first = _make_item(account_id, title="c1")
    second = _make_item(account_id, title="c2")
    _give_revision(first)
    _give_revision(second)

    cancel_event = threading.Event()

    def cancel_after_first(item_id: int, **kwargs):
        cancel_event.set()
        return {"item_id": item_id, "processed_path": "C:/out/x.mp4"}

    monkeypatch.setattr(export_svc, "export_item", cancel_after_first)

    with pytest.raises(JobCanceled):
        batch_finish.finish_batch([first, second], cancel_event=cancel_event)


def test_finish_batch_defers_the_cloud_upload_so_the_next_render_can_start(
    monkeypatch,
) -> None:
    # The pipeline: each reel's Worker upload runs on a background thread while
    # the next reel's FFmpeg render owns the CPU. export_item must therefore see
    # the deferred flag set, and it must be cleared once the batch is done.
    from nicheflow_studio.services import publishing

    account_id = _make_account(name="Pipelined History", auto_schedule=True)
    first = _make_item(account_id, title="one")
    second = _make_item(account_id, title="two")
    _give_revision(first)
    _give_revision(second)

    seen: list[bool] = []

    def fake_export(item_id: int, **kwargs):
        seen.append(publishing._DEFER_CLOUD_HANDOFF.get())
        return {
            "item_id": item_id,
            "processed_path": f"C:/out/{item_id}.mp4",
            "scheduled_publish": {
                "scheduled_at": "2026-08-04T09:00:00+00:00",
                "cloud_handoff": "deferred",
            },
        }

    monkeypatch.setattr(export_svc, "export_item", fake_export)

    result = batch_finish.finish_batch([first, second])

    assert seen == [True, True]
    assert publishing._DEFER_CLOUD_HANDOFF.get() is False
    assert result["pending_cloud"] == 2


def test_finish_batch_reports_no_pending_uploads_for_local_only_accounts(
    monkeypatch,
) -> None:
    account_id = _make_account(name="Local History", auto_schedule=True)
    item_id = _make_item(account_id, title="local")
    _give_revision(item_id)

    monkeypatch.setattr(
        export_svc,
        "export_item",
        lambda item_id, **kwargs: {
            "item_id": item_id,
            "processed_path": "C:/out/x.mp4",
            # No cloud mapping -> queue_for_publish never defers anything.
            "scheduled_publish": {"scheduled_at": "2026-08-04T09:00:00+00:00"},
        },
    )

    result = batch_finish.finish_batch([item_id])

    assert result["pending_cloud"] == 0


def test_finish_batch_refuses_a_batch_with_no_revisions() -> None:
    account_id = _make_account(name="Empty History")
    item_id = _make_item(account_id, title="undrafted")

    with pytest.raises(BatchFinishError) as excinfo:
        batch_finish.finish_batch([item_id])

    assert "Import the batch reply first" in str(excinfo.value)
