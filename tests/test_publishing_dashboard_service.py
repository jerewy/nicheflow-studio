from __future__ import annotations

import datetime as dt
from pathlib import Path

from nicheflow_studio.db.models import Account, Assignment, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import publishing_dashboard


def _seed_job(tmp_path: Path, *, status: str = "draft") -> int:
    output = tmp_path / "reel.mp4"
    output.write_bytes(b"video")
    with get_session() as session:
        account = Account(
            name="Cinema Files Daily",
            platform="instagram",
            instagram_profile="cinema",
        )
        item = DownloadItem(source_url="https://instagram.com/reel/x", title="Video title")
        session.add_all([account, item])
        session.commit()
        job = UploadJob(
            account_id=account.id,
            download_item_id=item.id,
            processed_path=str(output),
            title="Publish title",
            status=status,
        )
        session.add(job)
        session.commit()
        return job.id


def test_global_publish_jobs_and_mark_ready(tmp_path: Path) -> None:
    job_id = _seed_job(tmp_path)

    result = publishing_dashboard.list_global_publish_jobs()
    row = next(row for row in result["jobs"] if row["id"] == job_id)
    assert row["video"] == "Video title"
    assert row["status"] == "draft"

    assert publishing_dashboard.mark_ready([job_id]) == {"updated": 1}
    updated = publishing_dashboard.list_global_publish_jobs()
    assert next(row for row in updated["jobs"] if row["id"] == job_id)["status"] == "ready"


def test_account_readiness_lists_accounts_and_due_work(tmp_path: Path) -> None:
    _seed_job(tmp_path, status="ready")

    result = publishing_dashboard.account_readiness()

    assert result["totals"]["account_count"] == 1
    assert result["totals"]["total_due_now"] == 1
    assert result["rows"][0]["account_name"] == "Cinema Files Daily"
    assert result["rows"][0]["due_now"] == 1


def test_top_posts_orders_measured_posted_jobs_for_one_account_only() -> None:
    with get_session() as session:
        history = Account(name="History", platform="instagram", niche="history")
        movies = Account(name="Movies", platform="instagram", niche="movie")
        session.add_all([history, movies])
        session.flush()
        session.add_all(
            [
                UploadJob(
                    account_id=history.id,
                    processed_path="C:/history-best.mp4",
                    title="History best",
                    status="posted",
                    posted_likes=100,
                    posted_comments=20,
                    posted_shares=10,
                ),
                UploadJob(
                    account_id=history.id,
                    processed_path="C:/history-second.mp4",
                    title="History second",
                    status="posted",
                    posted_likes=80,
                    posted_comments=10,
                    posted_shares=5,
                ),
                UploadJob(
                    account_id=history.id,
                    processed_path="C:/history-no-metrics.mp4",
                    title="No metrics",
                    status="posted",
                ),
                UploadJob(
                    account_id=history.id,
                    processed_path="C:/history-draft.mp4",
                    title="Not posted",
                    status="draft",
                    posted_likes=999,
                ),
                UploadJob(
                    account_id=movies.id,
                    processed_path="C:/movie.mp4",
                    title="Other account winner",
                    status="posted",
                    posted_likes=9999,
                ),
            ]
        )
        session.commit()
        history_id = history.id

    rows = publishing_dashboard.top_posts(history_id)

    assert [row["title"] for row in rows] == ["History best", "History second"]
    assert all(row["account_id"] == history_id for row in rows)
    assert all(row["engagement"] > 0 for row in rows)


def test_top_post_titles_excludes_untitled_measured_jobs() -> None:
    with get_session() as session:
        account = Account(name="History", platform="instagram", niche="history")
        session.add(account)
        session.flush()
        session.add_all(
            [
                UploadJob(
                    account_id=account.id,
                    processed_path="C:/untitled.mp4",
                    status="posted",
                    posted_likes=500,
                ),
                UploadJob(
                    account_id=account.id,
                    processed_path="C:/titled.mp4",
                    title="Measured winner",
                    status="posted",
                    posted_likes=100,
                ),
            ]
        )
        session.commit()
        account_id = account.id

    assert publishing_dashboard.top_post_titles(account_id) == ["Measured winner"]


def test_account_stats_boundaries_queue_runway_and_next_post() -> None:
    now = dt.datetime(2026, 6, 11, 1, 0, tzinfo=dt.timezone.utc)
    with get_session() as session:
        account = Account(
            name="Bangkok History",
            platform="instagram",
            niche="history",
            upload_timezone="Asia/Bangkok",
            daily_posts_target=2,
        )
        other_niche = Account(name="Movie", platform="instagram", niche="movie")
        session.add_all([account, other_niche])
        session.flush()
        session.add_all(
            [
                # Bangkok local day starts at 17:00 UTC on the prior date.
                UploadJob(account_id=account.id, processed_path="today.mp4", status="posted", posted_at=dt.datetime(2026, 6, 10, 17, 0)),
                UploadJob(account_id=account.id, processed_path="before-day.mp4", status="posted", posted_at=dt.datetime(2026, 6, 10, 16, 59, 59)),
                UploadJob(account_id=account.id, processed_path="week-edge.mp4", status="posted", posted_at=dt.datetime(2026, 6, 4, 1, 0)),
                UploadJob(account_id=account.id, processed_path="before-week.mp4", status="posted", posted_at=dt.datetime(2026, 6, 4, 0, 59, 59)),
                UploadJob(account_id=account.id, processed_path="past.mp4", status="scheduled", scheduled_at=dt.datetime(2026, 6, 11, 0, 0)),
                UploadJob(account_id=account.id, processed_path="next.mp4", status="scheduled", scheduled_at=dt.datetime(2026, 6, 11, 2, 0)),
                UploadJob(account_id=account.id, processed_path="later.mp4", status="scheduled", scheduled_at=dt.datetime(2026, 6, 11, 3, 0)),
            ]
        )
        session.add_all(
            [
                Assignment(pool_item_id=index, account_id=account.id, niche="history", status=status)
                for index, status in enumerate(
                    ["assigned"] * 6 + ["posted", "rejected"], start=1
                )
            ]
        )
        session.commit()
        account_id = account.id

    result = publishing_dashboard.account_stats(account_id, now=now)

    assert result["niche"] == "history"
    assert len(result["accounts"]) == 1
    row = result["accounts"][0]
    assert row["today"] == 1
    assert row["daily_target"] == 2
    assert row["week"] == 3
    assert row["all_time"] == 4
    assert row["in_queue"] == 6
    assert row["scheduled"] == 2
    assert row["runway_days"] == 3.0
    assert row["runway_status"] == "green"
    assert row["next_post_at"] == "2026-06-11T09:00:00+07:00"


def test_account_stats_runway_thresholds_and_zero_data_accounts() -> None:
    now = dt.datetime(2026, 6, 11, 1, 0, tzinfo=dt.timezone.utc)
    with get_session() as session:
        accounts = [
            Account(name="Red", platform="instagram", niche="history", daily_posts_target=4),
            Account(name="Amber", platform="instagram", niche="history", daily_posts_target=4),
            Account(name="Green", platform="instagram", niche="history", daily_posts_target=4),
            Account(name="Zero", platform="instagram", niche="history", daily_posts_target=None),
        ]
        session.add_all(accounts)
        session.flush()
        for account, count in zip(accounts[:3], [3, 4, 12], strict=True):
            session.add_all(
                [
                    Assignment(
                        pool_item_id=(account.id * 100) + index,
                        account_id=account.id,
                        niche="history",
                        status="assigned",
                    )
                    for index in range(count)
                ]
            )
        session.commit()
        active_id = accounts[0].id

    rows = {
        row["account_name"]: row
        for row in publishing_dashboard.account_stats(active_id, now=now)["accounts"]
    }

    assert rows["Red"]["runway_status"] == "red"
    assert rows["Amber"]["runway_status"] == "amber"
    assert rows["Green"]["runway_status"] == "green"
    assert rows["Zero"] == {
        "account_id": rows["Zero"]["account_id"],
        "account_name": "Zero",
        "today": 0,
        "daily_target": 4,
        "week": 0,
        "all_time": 0,
        "in_queue": 0,
        "scheduled": 0,
        "runway_days": 0.0,
        "runway_status": "red",
        "next_post_at": None,
    }
