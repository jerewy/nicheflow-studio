from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from nicheflow_studio.db.models import Account, ClipSource, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import clip_history
from nicheflow_studio.services.clip_history import ClipHistoryError


def _plan(**overrides) -> dict:
    plan = {
        "url": "https://youtu.be/abc",
        "transcript_available": True,
        "previews": [{"index": 0}, {"index": 1}],
        "source": {"title": "A long documentary", "duration_seconds": 5220.0},
    }
    plan.update(overrides)
    return plan


def _make_account(name: str = "History") -> int:
    with get_session() as session:
        account = Account(name=name, platform="instagram")
        session.add(account)
        session.commit()
        return account.id


def _make_clip(source_ref: str, *, account_id: int, **overrides) -> int:
    with get_session() as session:
        item = DownloadItem(
            source_url="local://cut.mp4",
            clip_source_ref=source_ref,
            title=overrides.pop("title", "A cut"),
            file_path="C:/cut.mp4",
            account_id=account_id,
            status="downloaded",
            **overrides,
        )
        session.add(item)
        session.commit()
        return item.id


def test_record_source_creates_a_row_and_tags_the_plan(tmp_path: Path) -> None:
    result = clip_history.record_source(
        "https://youtu.be/abc", kind="url", workspace=tmp_path, plan=_plan()
    )

    assert result["source_ref"] == "https://youtu.be/abc"
    # The plan itself must survive: the UI reads previews off the same object.
    assert len(result["previews"]) == 2

    sources = clip_history.list_sources()
    assert len(sources) == 1
    assert sources[0]["title"] == "A long documentary"
    assert sources[0]["duration_seconds"] == 5220.0
    assert sources[0]["preview_count"] == 2
    assert sources[0]["transcript_available"] is True
    assert sources[0]["clip_count"] == 0


def test_reanalyzing_a_source_updates_the_same_row(tmp_path: Path) -> None:
    clip_history.record_source(
        "https://youtu.be/abc", kind="url", workspace=tmp_path, plan=_plan()
    )
    clip_history.record_source(
        "https://youtu.be/abc",
        kind="url",
        workspace=tmp_path,
        plan=_plan(previews=[{"index": 0}]),
    )

    sources = clip_history.list_sources()
    assert len(sources) == 1
    assert sources[0]["preview_count"] == 1


def test_reanalysis_without_a_title_keeps_the_one_already_resolved(tmp_path: Path) -> None:
    clip_history.record_source(
        "https://youtu.be/abc", kind="url", workspace=tmp_path, plan=_plan()
    )
    clip_history.record_source(
        "https://youtu.be/abc", kind="url", workspace=tmp_path, plan=_plan(source={})
    )

    assert clip_history.list_sources()[0]["title"] == "A long documentary"


def test_list_sources_counts_the_clips_cut_from_each(tmp_path: Path) -> None:
    account_id = _make_account()
    clip_history.record_source(
        "https://youtu.be/abc", kind="url", workspace=tmp_path, plan=_plan()
    )
    _make_clip("https://youtu.be/abc", account_id=account_id)
    _make_clip("https://youtu.be/abc", account_id=account_id)
    # A clip from somewhere else must not be counted against this source.
    _make_clip("https://youtu.be/other", account_id=account_id)

    sources = clip_history.list_sources()
    assert [row["clip_count"] for row in sources if row["source_ref"].endswith("abc")] == [2]


def test_list_sources_orders_by_most_recently_analyzed(tmp_path: Path) -> None:
    clip_history.record_source("https://youtu.be/one", kind="url", workspace=tmp_path, plan=_plan())
    clip_history.record_source("https://youtu.be/two", kind="url", workspace=tmp_path, plan=_plan())
    # Force a gap: two records inside the same clock tick would tie.
    with get_session() as session:
        row = session.query(ClipSource).filter_by(source_ref="https://youtu.be/two").one()
        row.last_analyzed_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
        session.commit()

    assert clip_history.list_sources()[0]["source_ref"] == "https://youtu.be/two"


def test_list_clips_reports_how_far_each_clip_got(tmp_path: Path) -> None:
    account_id = _make_account()
    reference = "https://youtu.be/abc"
    clip_history.record_source(reference, kind="url", workspace=tmp_path, plan=_plan())

    in_library = _make_clip(reference, account_id=account_id, title="Still in the library")
    exported = _make_clip(
        reference, account_id=account_id, title="Exported", processed_path="C:/out.mp4"
    )
    scheduled = _make_clip(
        reference, account_id=account_id, title="Scheduled", processed_path="C:/out2.mp4"
    )
    posted = _make_clip(
        reference, account_id=account_id, title="Posted", processed_path="C:/out3.mp4"
    )
    with get_session() as session:
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=scheduled,
                processed_path="C:/out2.mp4",
                status="scheduled",
                scheduled_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=posted,
                processed_path="C:/out3.mp4",
                status="posted",
                posted_at=dt.datetime.now(dt.timezone.utc),
                posted_url="https://instagram.com/p/xyz",
            )
        )
        session.commit()

    clips = clip_history.list_clips(reference)
    stages = {clip["item_id"]: clip["stage"] for clip in clips}
    assert stages[in_library] == "library"
    assert stages[exported] == "exported"
    assert stages[scheduled] == "scheduled"
    assert stages[posted] == "posted"
    assert all(clip["account_name"] == "History" for clip in clips)


def test_list_clips_uses_the_newest_job_for_a_re_exported_clip(tmp_path: Path) -> None:
    account_id = _make_account()
    reference = "https://youtu.be/abc"
    item_id = _make_clip(reference, account_id=account_id, processed_path="C:/out.mp4")
    with get_session() as session:
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=item_id,
                processed_path="C:/old.mp4",
                status="draft",
            )
        )
        session.commit()
        session.add(
            UploadJob(
                account_id=account_id,
                download_item_id=item_id,
                processed_path="C:/out.mp4",
                status="scheduled",
                scheduled_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        session.commit()

    assert clip_history.list_clips(reference)[0]["stage"] == "scheduled"


def test_list_clips_is_empty_for_an_unmined_source() -> None:
    assert clip_history.list_clips("https://youtu.be/nothing") == []


def test_forget_source_keeps_the_clips_it_produced(tmp_path: Path) -> None:
    account_id = _make_account()
    reference = "https://youtu.be/abc"
    clip_history.record_source(reference, kind="url", workspace=tmp_path, plan=_plan())
    item_id = _make_clip(reference, account_id=account_id)

    clip_history.forget_source(reference)

    assert clip_history.list_sources() == []
    with get_session() as session:
        assert session.get(DownloadItem, item_id) is not None


def test_forget_source_refuses_to_delete_a_workspace_outside_the_clips_folder(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete me")
    clip_history.record_source(
        "https://youtu.be/abc", kind="url", workspace=outside, plan=_plan()
    )

    result = clip_history.forget_source("https://youtu.be/abc", delete_workspace=True)

    assert result["workspace_removed"] is False
    assert outside.exists()


def test_forget_source_deletes_a_real_clips_workspace() -> None:
    from nicheflow_studio.core.paths import data_dir

    workspace = data_dir() / "clips" / "abc123"
    workspace.mkdir(parents=True)
    (workspace / "source.mp4").write_bytes(b"cached download")
    clip_history.record_source(
        "https://youtu.be/abc", kind="url", workspace=workspace, plan=_plan()
    )

    result = clip_history.forget_source("https://youtu.be/abc", delete_workspace=True)

    assert result["workspace_removed"] is True
    assert not workspace.exists()


def test_forget_unknown_source_raises() -> None:
    with pytest.raises(ClipHistoryError):
        clip_history.forget_source("https://youtu.be/never-seen")


def test_file_sources_get_a_distinct_resolved_key(tmp_path: Path) -> None:
    nested = tmp_path / "packs"
    nested.mkdir()
    video = nested / "pack.mp4"
    video.write_bytes(b"file")

    reference = clip_history.source_ref_for_file(video)

    assert reference.startswith("file://")
    # The same file reached by a scenic route must be one history row, not two.
    assert clip_history.source_ref_for_file(nested / ".." / "packs" / "pack.mp4") == reference
