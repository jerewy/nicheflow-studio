"""Clip Studio history: which sources were mined, and where each clip ended up.

Clip Studio was write-only before this. Ranking a source downloaded it into an
opaque ``data/clips/<sha1>`` workspace and the cut clips became ordinary
``DownloadItem`` rows, so there was no way to ask "have I already been through
this video?" — and re-pasting a URL looked identical to a fresh one even though
the download was already cached.

Two questions this answers:

* **Which sources have I mined?** :func:`list_sources` — one row per source with
  the clips it produced and when it was last opened.
* **What happened to the clips from this one?** :func:`list_clips` — each cut
  clip and how far it got: still in the library, exported, scheduled, posted.

Recording happens on the *analysis* step (:func:`plan_and_record_url` /
:func:`plan_and_record_file`) rather than when a clip is cut, deliberately: a
source you watched eight previews from and rejected is exactly the one you must
not pay the download for twice.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import func, select

from nicheflow_studio.db.models import Account, ClipSource, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError


class ClipHistoryError(ServiceError):
    """Raised when a history record cannot be read or removed."""


def source_ref_for_url(url: str) -> str:
    """The stable key for a URL source."""
    return (url or "").strip()


def source_ref_for_file(video_path: Path | str) -> str:
    """The stable key for a local-file source.

    ``file://`` prefixed so a path and a URL can never collide in the same
    column, and resolved so the same file reached by two different relative
    paths is one history row rather than two.
    """
    return f"file://{Path(video_path).expanduser().resolve()}"


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def record_source(source_ref: str, *, kind: str, workspace: Path | str, plan: dict) -> dict:
    """Create or refresh the history row for a source, and return ``plan``.

    Returns the plan with ``source_ref`` mixed in so the caller (the preview job)
    hands the UI everything it needs to tag the clips it later cuts.
    """
    reference = (source_ref or "").strip()
    if not reference:
        raise ClipHistoryError("A clip source needs a URL or file path.")

    source = plan.get("source") or {}
    title = plan.get("title") or source.get("title")
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as session:
        row = session.scalar(select(ClipSource).where(ClipSource.source_ref == reference))
        if row is None:
            row = ClipSource(source_ref=reference, created_at=now)
            session.add(row)
        row.kind = kind
        row.last_analyzed_at = now
        row.workspace_path = str(workspace)
        # A re-analysis that found nothing must not wipe the title the first run
        # resolved — yt-dlp does not always report one on a retry.
        if title:
            row.title = str(title)[:512]
        duration = source.get("duration_seconds")
        if duration is not None:
            row.duration_seconds = float(duration)
        row.transcript_available = 1 if plan.get("transcript_available") else 0
        row.preview_count = len(plan.get("previews") or ())
        session.commit()
    return {**plan, "source_ref": reference}


def plan_and_record_url(url: str, workspace: Path, *, top_n: int) -> dict:
    """:func:`clip_studio.plan_and_preview`, with the source written to history."""
    from nicheflow_studio.services import clip_studio

    reference = source_ref_for_url(url)
    plan = clip_studio.plan_and_preview(url, workspace, top_n=top_n)
    return record_source(reference, kind="url", workspace=workspace, plan=plan)


def plan_and_record_file(video_path: Path, workspace: Path, *, top_n: int) -> dict:
    """:func:`clip_studio.plan_local_file`, with the source written to history."""
    from nicheflow_studio.services import clip_studio

    reference = source_ref_for_file(video_path)
    plan = clip_studio.plan_local_file(video_path, workspace, top_n=top_n)
    return record_source(reference, kind="file", workspace=workspace, plan=plan)


def list_sources() -> list[dict]:
    """Every mined source, newest activity first, with its clip count."""
    with get_session() as session:
        counts = dict(
            session.execute(
                select(DownloadItem.clip_source_ref, func.count(DownloadItem.id))
                .where(DownloadItem.clip_source_ref.is_not(None))
                .group_by(DownloadItem.clip_source_ref)
            ).all()
        )
        rows = session.scalars(
            select(ClipSource).order_by(
                ClipSource.last_analyzed_at.desc(), ClipSource.id.desc()
            )
        ).all()
        return [
            {
                "source_ref": row.source_ref,
                "kind": row.kind,
                "title": row.title,
                "workspace_path": row.workspace_path,
                "duration_seconds": row.duration_seconds,
                "transcript_available": bool(row.transcript_available),
                "preview_count": row.preview_count,
                "clip_count": counts.get(row.source_ref, 0),
                "created_at": _iso(row.created_at),
                "last_analyzed_at": _iso(row.last_analyzed_at),
            }
            for row in rows
        ]


# How far a cut clip has travelled. Read in this order, most-advanced first, so a
# reel that was posted does not also report itself as merely "exported".
def _stage(item: DownloadItem, job: UploadJob | None) -> str:
    if job is not None and (job.posted_at is not None or (job.status or "").lower() == "posted"):
        return "posted"
    if job is not None and job.scheduled_at is not None:
        return "scheduled"
    if item.processed_path:
        return "exported"
    return "library"


def list_clips(source_ref: str) -> list[dict]:
    """The clips cut from one source, oldest first, with how far each got."""
    reference = (source_ref or "").strip()
    if not reference:
        raise ClipHistoryError("A clip source needs a URL or file path.")
    with get_session() as session:
        items = session.scalars(
            select(DownloadItem)
            .where(DownloadItem.clip_source_ref == reference)
            .order_by(DownloadItem.id.asc())
        ).all()
        if not items:
            return []
        jobs = session.scalars(
            select(UploadJob)
            .where(UploadJob.download_item_id.in_([item.id for item in items]))
            .order_by(UploadJob.id.asc())
        ).all()
        # The newest job per item wins: re-exporting a reel leaves the earlier
        # job behind, and the schedule the operator cares about is the live one.
        latest: dict[int, UploadJob] = {}
        for job in jobs:
            if job.download_item_id is not None:
                latest[job.download_item_id] = job
        account_names = dict(session.execute(select(Account.id, Account.name)).all())

        clips = []
        for item in items:
            job = latest.get(item.id)
            clips.append(
                {
                    "item_id": item.id,
                    "title": item.title,
                    "created_at": _iso(item.created_at),
                    "file_path": item.file_path,
                    "processed_path": item.processed_path,
                    "review_state": item.review_state,
                    "account_id": item.account_id,
                    "account_name": account_names.get(item.account_id),
                    "stage": _stage(item, job),
                    "job_id": job.id if job else None,
                    "job_status": job.status if job else None,
                    "scheduled_at": _iso(job.scheduled_at) if job else None,
                    "posted_at": _iso(job.posted_at) if job else None,
                    "posted_url": job.posted_url if job else None,
                }
            )
        return clips


def forget_source(source_ref: str, *, delete_workspace: bool = False) -> dict:
    """Drop a source from the history list.

    The clips it produced are left alone — they are library items with their own
    lifecycle, and losing a scheduled reel because its history row was tidied
    away would be indefensible. ``delete_workspace`` additionally removes the
    cached download, which is the only part of this that reclaims real disk.
    """
    import shutil

    from nicheflow_studio.core.paths import data_dir

    reference = (source_ref or "").strip()
    with get_session() as session:
        row = session.scalar(select(ClipSource).where(ClipSource.source_ref == reference))
        if row is None:
            raise ClipHistoryError(f"No clip source recorded for {reference!r}.")
        workspace = row.workspace_path
        session.delete(row)
        session.commit()

    removed = False
    if delete_workspace and workspace:
        target = Path(workspace).expanduser().resolve()
        # Refuse to recurse into anything outside data/clips — this deletes a
        # whole tree, and workspace_path is a stored string.
        clips_root = (data_dir() / "clips").resolve()
        if target.is_dir() and target != clips_root and clips_root in target.parents:
            shutil.rmtree(target, ignore_errors=True)
            removed = not target.exists()
    return {"source_ref": reference, "workspace_removed": removed}
