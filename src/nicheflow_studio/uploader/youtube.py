from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nicheflow_studio.core.paths import data_dir
from nicheflow_studio.db.models import UploadJob


YOUTUBE_UPLOAD_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


@dataclass(frozen=True)
class YouTubeUploadPayload:
    file_path: Path
    title: str
    description: str = ""
    privacy_status: str = "private"
    made_for_kids: bool = False
    tags: tuple[str, ...] = ()
    category_id: str = "22"


def upload_scheduled_job(job: UploadJob) -> str:
    oauth_dir = data_dir() / "youtube_oauth"
    oauth_dir.mkdir(parents=True, exist_ok=True)
    return upload_youtube_video(
        YouTubeUploadPayload(
            file_path=Path(job.processed_path),
            title=job.title or Path(job.processed_path).stem,
            description=job.description or "",
            privacy_status=job.privacy_status or "private",
            made_for_kids=bool(job.made_for_kids),
            tags=tuple(_parse_tags(job.tags)),
        ),
        client_secrets_path=data_dir() / "youtube_client_secret.json",
        token_path=oauth_dir / f"account_{job.account_id}_token.json",
    )


def upload_youtube_video(
    payload: YouTubeUploadPayload,
    *,
    youtube_service: Any | None = None,
    media_upload_factory: Callable[..., Any] | None = None,
    client_secrets_path: Path | None = None,
    token_path: Path | None = None,
) -> str:
    if not payload.file_path.exists():
        raise FileNotFoundError(f"Processed output is missing: {payload.file_path}")

    service = youtube_service or _authenticated_youtube_service(
        client_secrets_path=client_secrets_path,
        token_path=token_path,
    )
    media_factory = media_upload_factory or _media_file_upload_factory()
    body = {
        "snippet": {
            "title": payload.title,
            "description": payload.description,
            "tags": list(payload.tags),
            "categoryId": payload.category_id,
        },
        "status": {
            "privacyStatus": payload.privacy_status,
            "selfDeclaredMadeForKids": payload.made_for_kids,
        },
    }
    request = service.videos().insert(
        part=",".join(["snippet", "status"]),
        body=body,
        media_body=media_factory(str(payload.file_path), chunksize=-1, resumable=True),
    )

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id") if isinstance(response, dict) else None
    if not video_id:
        raise RuntimeError("YouTube upload completed without returning a video id.")
    return str(video_id)


def _authenticated_youtube_service(
    *,
    client_secrets_path: Path | None,
    token_path: Path | None,
) -> Any:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "YouTube upload support requires google-api-python-client, "
            "google-auth-oauthlib, and google-auth-httplib2."
        ) from exc

    client_secrets = client_secrets_path or data_dir() / "youtube_client_secret.json"
    token_file = token_path or data_dir() / "youtube_oauth_token.json"
    if not client_secrets.exists():
        raise FileNotFoundError(
            f"Missing YouTube OAuth client secrets file: {client_secrets}. "
            "Create an OAuth desktop client in Google Cloud and save it there."
        )

    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), YOUTUBE_UPLOAD_SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets), YOUTUBE_UPLOAD_SCOPES
            )
            credentials = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=credentials)


def _media_file_upload_factory() -> Callable[..., Any]:
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("YouTube upload support requires google-api-python-client.") from exc
    return MediaFileUpload


def _parse_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []
    try:
        parsed = json.loads(raw_tags)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(tag).strip() for tag in parsed if str(tag).strip()]
    normalized = raw_tags.replace("\n", ",").replace(";", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]
