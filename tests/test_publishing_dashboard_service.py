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
