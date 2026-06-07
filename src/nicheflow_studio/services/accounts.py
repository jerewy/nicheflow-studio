"""Account management service (UI-independent).

UI-free extraction of the PyQt account CRUD (MainWindow._on_save_account_clicked
/ _on_delete_account_clicked) so the React Account Manager and any other caller
share one implementation. Exposes list/get/create/update/delete over the
:class:`Account` model.

Delete mirrors the desktop behavior: an account's download items are unassigned
(kept), while its upload jobs, scrape candidates, scrape runs, and sources are
removed, then the account row itself.
"""

from __future__ import annotations

from sqlalchemy import func, select

from nicheflow_studio.db.models import (
    Account,
    DownloadItem,
    ScrapeCandidate,
    ScrapeRun,
    Source,
    UploadJob,
)
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services.errors import ServiceError

# Free-text fields the editor can set directly (empty string -> NULL).
_TEXT_FIELDS = (
    "niche_label",
    "niche",
    "login_identifier",
    "instagram_profile",
    "credential_blob",
    "writing_tone",
    "target_audience",
    "hook_style",
    "banned_phrases",
    "title_style_notes",
    "caption_style_notes",
    "upload_timezone",
    "upload_default_privacy",
    "upload_schedule_slots",
)


class AccountError(ServiceError):
    """Raised for invalid account operations (missing name, unknown id…)."""


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _account_summary(account: Account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "platform": account.platform,
        "niche_label": account.niche_label,
        "niche": account.niche,
        "instagram_handle": account.instagram_handle,
    }


def _account_detail(session, account: Account) -> dict:
    detail = _account_summary(account)
    detail.update(
        {
            "login_identifier": account.login_identifier,
            "instagram_profile": account.instagram_profile,
            "credential_blob": account.credential_blob,
            "writing_tone": account.writing_tone,
            "target_audience": account.target_audience,
            "hook_style": account.hook_style,
            "banned_phrases": account.banned_phrases,
            "title_style_notes": account.title_style_notes,
            "caption_style_notes": account.caption_style_notes,
            "upload_timezone": account.upload_timezone,
            "upload_default_privacy": account.upload_default_privacy,
            "upload_schedule_slots": account.upload_schedule_slots,
            "download_item_count": session.scalar(
                select(func.count(DownloadItem.id)).where(DownloadItem.account_id == account.id)
            )
            or 0,
            "upload_job_count": session.scalar(
                select(func.count(UploadJob.id)).where(UploadJob.account_id == account.id)
            )
            or 0,
        }
    )
    return detail


def _apply_payload(account: Account, payload: dict, *, partial: bool) -> None:
    """Copy editable fields from ``payload`` onto ``account``.

    When ``partial`` is True (update), only keys present in the payload are
    touched, so omitted fields (e.g. credential_blob) are preserved.
    """
    if "name" in payload or not partial:
        name = _clean(payload.get("name"))
        if not name:
            raise AccountError("Account name is required.")
        account.name = name
    if "platform" in payload or not partial:
        account.platform = _clean(payload.get("platform")) or "instagram"
    if "instagram_handle" in payload or not partial:
        handle = _clean(payload.get("instagram_handle"))
        account.instagram_handle = handle.lstrip("@") if handle else None
    for field in _TEXT_FIELDS:
        if field in payload or not partial:
            setattr(account, field, _clean(payload.get(field)))


def list_accounts() -> list[dict]:
    """All accounts, newest first."""
    with get_session() as session:
        rows = session.scalars(select(Account).order_by(Account.id.desc())).all()
        return [_account_summary(row) for row in rows]


def get_account(account_id: int) -> dict:
    """Full editable detail for one account, with dependent-row counts."""
    with get_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise AccountError(f"No account with id {account_id}.")
        return _account_detail(session, account)


def create_account(payload: dict | None = None) -> dict:
    """Create an account from the editor payload (``name`` required)."""
    payload = payload or {}
    with get_session() as session:
        account = Account(name="placeholder")
        _apply_payload(account, payload, partial=False)
        session.add(account)
        session.commit()
        return _account_detail(session, account)


def update_account(account_id: int, payload: dict | None = None) -> dict:
    """Apply a partial update to an existing account."""
    payload = payload or {}
    with get_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise AccountError(f"No account with id {account_id}.")
        _apply_payload(account, payload, partial=True)
        session.commit()
        return _account_detail(session, account)


def delete_account(account_id: int) -> dict:
    """Delete an account, unassigning its download items and removing its
    upload jobs, scrape candidates, scrape runs, and sources (mirrors PyQt)."""
    with get_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise AccountError(f"No account with id {account_id}.")

        unassigned = 0
        for item in session.scalars(
            select(DownloadItem).where(DownloadItem.account_id == account_id)
        ).all():
            item.account_id = None
            unassigned += 1
        removed_jobs = 0
        for job in session.scalars(
            select(UploadJob).where(UploadJob.account_id == account_id)
        ).all():
            session.delete(job)
            removed_jobs += 1
        for candidate in session.scalars(
            select(ScrapeCandidate).where(ScrapeCandidate.account_id == account_id)
        ).all():
            session.delete(candidate)
        for run in session.scalars(
            select(ScrapeRun).where(ScrapeRun.account_id == account_id)
        ).all():
            session.delete(run)
        for source in session.scalars(select(Source).where(Source.account_id == account_id)).all():
            session.delete(source)
        session.delete(account)
        session.commit()
        return {
            "deleted_account_id": account_id,
            "unassigned_download_items": unassigned,
            "removed_upload_jobs": removed_jobs,
        }
