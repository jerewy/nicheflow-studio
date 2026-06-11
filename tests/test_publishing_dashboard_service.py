from __future__ import annotations

from pathlib import Path

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
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
