"""Downloads / Library service (UI-independent).

Read + light-management view over downloaded items, extracted from the PyQt
Downloads page so the React Library screen can list items, (re)assign them to an
account, and remove them. Heavy acquisition (the download queue/retry) stays in
the PyQt app for now; this slice covers the library management actions.

Remove mirrors the PyQt cleanup (reset linked scrape candidates) and also clears
this item's draft revisions and unlinks its publish-queue rows so nothing dangles
at the new draft-revision foreign key.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from nicheflow_studio.db.models import (
    Account,
    DownloadItem,
    DraftRevision,
    ScrapeCandidate,
    UploadJob,
)
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

_LIST_LIMIT = 100


class LibraryError(ServiceError):
    """Raised for invalid library operations (unknown item/account)."""


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def list_items(limit: int = _LIST_LIMIT) -> list[dict]:
    """Recent library items (newest first), with account name and state flags."""
    with get_session() as session:
        names = {a.id: a.name for a in session.scalars(select(Account)).all()}
        rows = session.scalars(
            select(DownloadItem).order_by(DownloadItem.id.desc()).limit(limit)
        ).all()
        return [
            {
                "id": row.id,
                "title": row.title,
                "source_url": row.source_url,
                "status": row.status,
                "file_path": row.file_path,
                "has_file": bool(row.file_path),
                "has_processed": bool(row.processed_path),
                "has_draft": bool(row.title_draft or row.caption_draft),
                "account_id": row.account_id,
                "account_name": names.get(row.account_id) if row.account_id else None,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]


def assign_account(item_id: int, account_id: int | None) -> dict:
    """Assign (or clear, with ``None``) the account for a download item."""
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise LibraryError(f"No download item with id {item_id}.")
        account_name = None
        if account_id is not None:
            account = session.get(Account, account_id)
            if account is None:
                raise LibraryError(f"No account with id {account_id}.")
            account_name = account.name
        item.account_id = account_id
        session.commit()
        return {"item_id": item_id, "account_id": account_id, "account_name": account_name}


def remove_item(item_id: int) -> dict:
    """Remove a library item and tidy up its dependents.

    Resets linked scrape candidates (so they return to the candidate pool),
    deletes this item's draft revisions, and unlinks its publish-queue rows.
    """
    with get_session() as session:
        item = session.get(DownloadItem, item_id)
        if item is None:
            raise LibraryError(f"No download item with id {item_id}.")

        for candidate in session.scalars(
            select(ScrapeCandidate).where(ScrapeCandidate.queued_download_item_id == item_id)
        ).all():
            candidate.queued_download_item_id = None
            if candidate.state in {"queued", "downloaded"}:
                candidate.state = "candidate"

        revisions = 0
        for revision in session.scalars(
            select(DraftRevision).where(DraftRevision.download_item_id == item_id)
        ).all():
            session.delete(revision)
            revisions += 1

        for job in session.scalars(
            select(UploadJob).where(UploadJob.download_item_id == item_id)
        ).all():
            job.download_item_id = None

        session.delete(item)
        session.commit()
        return {"removed_item_id": item_id, "deleted_revisions": revisions}
