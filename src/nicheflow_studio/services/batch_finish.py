"""Finish an imported draft batch: apply the recommended option, then export.

The chat handoff leaves every reel sitting on a draft revision that still needs
three manual steps per clip (pick the option, apply it, export). Across six
accounts that is the bulk of the remaining hand work, so this runs the chain
once for the whole batch as a single cancellable background job.

Scheduling is deliberately NOT re-implemented here: ``export_item`` already
publishes into the account's next safe slot when that account has
``auto_schedule_on_export`` turned on, so the chain honours each account's own
setting instead of overriding it.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import or_, select

from nicheflow_studio.db.models import Account, DownloadItem, UploadJob
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import draft_revisions, export as export_svc, publishing
from nicheflow_studio.services.draft_revisions import DraftRevisionError
from nicheflow_studio.services.errors import ServiceError
from nicheflow_studio.services.jobs import JobCanceled

logger = logging.getLogger(__name__)


class BatchFinishError(ServiceError):
    """Raised for a batch-level problem (nothing to finish, bad input)."""


def _check_cancel(cancel_event: "threading.Event | None") -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise JobCanceled("Finish batch canceled.")


# Job states that mean the reel is committed to actually go out: booked into a
# local slot, handed to the Cloudflare Worker, or already published. A "draft"
# job is only queued, and "failed"/"skipped" are dead ends — none of those block
# a re-finish.
_COMMITTED_JOB_STATUSES = ("scheduled", "cloud", "posted")


def _committed_item_ids(session, item_ids: list[int]) -> set[int]:
    """Items in this batch whose export is already queued to publish.

    A batch's item list is PINNED when its prompt is prepared, and the Batch
    Drafts screen keeps showing that list after a finish. Without this check a
    second click on the stale list re-applies, re-renders (minutes of FFmpeg),
    and re-schedules reels that already went out — the same pinned-list hazard
    the reject check below guards against, on the export side.
    """
    if not item_ids:
        return set()
    return {
        row[0]
        for row in session.execute(
            select(UploadJob.download_item_id)
            .where(UploadJob.download_item_id.in_(item_ids))
            .where(
                or_(
                    UploadJob.status.in_(_COMMITTED_JOB_STATUSES),
                    UploadJob.posted_at.is_not(None),
                )
            )
        ).all()
    }


def plan_batch(item_ids: list[int]) -> list[dict]:
    """What ``finish_batch`` would do, without doing it.

    Lets the UI show which reels are ready, which option each would apply, and
    which are blocked, before the user commits to rendering the whole batch.
    """
    ids = [int(item_id) for item_id in item_ids]
    plan: list[dict] = []
    with get_session() as session:
        # Lazy import: keep this module free of library's downloader import chain.
        from nicheflow_studio.services import cloud_publisher, draft_handoff, library

        # The per-account "#N" the Processing table and the batch prompt both use.
        # Carried here so a results view can name a reel the same way even after
        # the candidate list it came from has been refreshed away.
        seq_by_item = library.account_sequence_map(session)
        # Whether each reel's post will be handed to the Cloudflare Worker. The
        # handoff itself happens later inside queue_for_publish, but which
        # accounts are mapped is known now — so the confirm dialog can say
        # "scheduled in the cloud" instead of leaving the user to guess whether
        # a post depends on this machine staying open.
        cloud_ready = cloud_publisher.is_configured()
        committed = _committed_item_ids(session, ids)
        for item_id in ids:
            item = session.get(DownloadItem, item_id)
            if item is None:
                plan.append({"item_id": item_id, "ready": False, "reason": "item not found"})
                continue
            # A batch's item list is pinned when its prompt is prepared, so a
            # reject made afterwards (from this screen or from Processing) leaves
            # the reel in the list. Without this check the reel was applied,
            # exported, and re-scheduled — and since reject_item CANCELS that
            # clip's publish jobs, finishing the batch silently un-rejected it.
            if (item.review_state or "") in draft_handoff.UNDRAFTABLE_REVIEW_STATES:
                plan.append(
                    {
                        "item_id": item_id,
                        "account_seq": seq_by_item.get(item_id),
                        "ready": False,
                        "reason": f"{item.review_state} — excluded from this batch",
                    }
                )
                continue
            if item_id in committed:
                plan.append(
                    {
                        "item_id": item_id,
                        "account_seq": seq_by_item.get(item_id),
                        "title": item.title,
                        "ready": False,
                        "reason": "already exported and queued to publish",
                    }
                )
                continue
            account = session.get(Account, item.account_id) if item.account_id else None
            entry = {
                "item_id": item_id,
                "account_seq": seq_by_item.get(item_id),
                "title": item.title,
                "account_id": item.account_id,
                "account_name": account.name if account else None,
                "auto_schedules": bool(account and account.auto_schedule_on_export),
                "publishes_via_cloud": bool(
                    cloud_ready
                    and account is not None
                    and cloud_publisher.cloud_account_key_for(account.id)
                ),
            }
            plan.append(entry)

    # latest_revision opens its own session; keep it out of the loop above.
    for entry in plan:
        if entry.get("ready") is False:
            continue
        revision = draft_revisions.latest_revision(entry["item_id"])
        if revision is None or not revision.title_options:
            entry.update(ready=False, reason="no draft revision yet")
            continue
        option = _option_to_apply(revision)
        entry.update(
            ready=True,
            option=option,
            revision_id=revision.id,
            title=revision.title_options[option - 1],
        )
    return plan


def _option_to_apply(revision) -> int:
    """The option number to apply: the model's recommendation, else the first.

    ``draft_guard`` has already moved the recommendation off any option whose
    claim the clip's signals do not support, so following it here inherits that
    protection rather than blindly taking option 1.
    """
    recommended = revision.recommended_title_index
    if recommended and 1 <= recommended <= len(revision.title_options):
        return recommended
    return 1


def finish_batch(
    item_ids: list[int],
    *,
    progress=None,
    cancel_event: "threading.Event | None" = None,
) -> dict:
    """Apply the recommended option and export every ready reel in the batch.

    Renders run one at a time on purpose: FFmpeg saturates the CPU, so running
    a 36-reel batch in parallel would be slower overall and would starve the UI.
    Measured on four real reels (35s/10s/16s/35s) on a 22-thread machine —
    sequential 28.7s, two at a time 33.7s, three 31.6s, four 33.3s. Concurrency
    here costs 10-17%: x264 is already multithreaded, so extra encoders only
    contend. Do not "optimise" this into a thread pool.
    The cloud upload is a different resource, though, so it is pipelined against
    the next render via :func:`publishing.deferred_cloud_handoff` — uploads still
    go one at a time, they just no longer block the CPU stage. That means the job
    can report done while the last reels are still uploading; ``pending_cloud``
    counts them.

    A per-item failure is recorded and the batch continues, because one bad clip
    must not strand the other thirty-five.
    """
    ids = [int(item_id) for item_id in item_ids]
    if not ids:
        raise BatchFinishError("Choose at least one reel to finish.")

    plan = plan_batch(ids)
    ready = [entry for entry in plan if entry.get("ready")]
    if not ready:
        # "Import the reply first" is the wrong instruction when the real cause is
        # a stale list whose reels this batch already exported, so name that case.
        if plan and all(
            entry.get("reason") == "already exported and queued to publish"
            for entry in plan
        ):
            raise BatchFinishError(
                "These reels were already exported and queued to publish. "
                "Refresh the batch to pick up what still needs drafting."
            )
        raise BatchFinishError(
            "None of these reels have a draft revision yet. Import the batch reply first."
        )

    def report(fraction: float, message: str = "") -> None:
        if progress is not None:
            progress(fraction, message)

    applied: list[dict] = []
    exported: list[dict] = []
    scheduled: list[dict] = []
    failed: list[dict] = []
    skipped = [
        {"item_id": entry["item_id"], "reason": entry.get("reason", "not ready")}
        for entry in plan
        if not entry.get("ready")
    ]
    # Reels whose Worker upload was handed to a background thread and may still be
    # in flight when the loop finishes.
    pending_cloud = 0

    total = len(ready)
    for index, entry in enumerate(ready):
        _check_cancel(cancel_event)
        item_id = entry["item_id"]
        label = entry.get("account_name") or f"item {item_id}"
        base = index / total
        report(base, f"{label}: applying option {entry['option']}…")
        try:
            result = draft_revisions.apply_revision(item_id, entry["option"])
            applied.append({"item_id": item_id, "option": entry["option"], **result})
        except DraftRevisionError as exc:
            failed.append({"item_id": item_id, "stage": "apply", "error": str(exc)})
            continue

        _check_cancel(cancel_event)
        report(base + 0.5 / total, f"{label}: rendering…")
        try:
            # Deferred handoff pipelines the stages: this reel's video upload runs
            # on a background thread while the next reel's FFmpeg render owns the
            # CPU. Uploads still go one at a time behind cloud_publisher's request
            # lock, so this adds no upload concurrency, only overlap.
            with publishing.deferred_cloud_handoff():
                export_result = export_svc.export_item(item_id, cancel_event=cancel_event)
        except JobCanceled:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad clip must not strand the batch
            logger.exception("Finish batch export failed for item %s", item_id)
            failed.append({"item_id": item_id, "stage": "export", "error": str(exc)})
            continue

        exported.append({"item_id": item_id, "processed_path": export_result.get("processed_path")})
        # export_item auto-schedules when the account opts in; surface whichever
        # of the two it reported so the UI can say what actually happened.
        if export_result.get("scheduled_publish"):
            schedule = export_result["scheduled_publish"]
            scheduled.append({"item_id": item_id, "schedule": schedule})
            if schedule.get("cloud_handoff") == "deferred":
                pending_cloud += 1
        elif export_result.get("warning"):
            failed.append(
                {"item_id": item_id, "stage": "schedule", "error": export_result["warning"]}
            )

    # The last reels' uploads are still running on their handoff threads when the
    # loop ends, so "Done" would overstate it: those rows read Scheduled until the
    # push lands (or the sync sweep retries a failed one). Say so rather than
    # letting the user read a stale status as a stall.
    report(
        1.0,
        f"Rendered {len(exported)} — {pending_cloud} cloud upload(s) finishing in the background"
        if pending_cloud
        else "Done",
    )
    return {
        "applied": applied,
        "exported": exported,
        "scheduled": scheduled,
        "failed": failed,
        "skipped": skipped,
        "pending_cloud": pending_cloud,
    }
