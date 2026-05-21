from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from instagrapi import Client
from instagrapi import exceptions as ig_errors


DEFAULT_SESSION_DIR = Path.home() / ".instagram_scraper"


def default_settings_path(username: str) -> Path:
    return DEFAULT_SESSION_DIR / f"instagrapi-{username}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Log in with instagrapi, save a session, and probe one Instagram profile."
    )
    parser.add_argument("--username", required=True, help="Instagram username to log in with.")
    parser.add_argument(
        "--target",
        default="meme.ig",
        help="Profile username to probe after login. Default: meme.ig",
    )
    parser.add_argument("--limit", type=int, default=1, help="Number of reels/media to print.")
    parser.add_argument(
        "--settings",
        type=Path,
        help="Session JSON path. Defaults to ~/.instagram_scraper/instagrapi-USERNAME.json",
    )
    parser.add_argument(
        "--verification-code",
        default="",
        help="2FA verification code if Instagram asks for one.",
    )
    parser.add_argument(
        "--download-url",
        help="Optional direct Reel/post URL to download after login.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data") / "downloads" / "instagram-instagrapi-test",
        help="Folder for optional direct URL downloads.",
    )
    return parser


def login_client(
    *,
    username: str,
    settings_path: Path,
    verification_code: str,
) -> Client:
    client = Client()
    if settings_path.exists():
        client.load_settings(settings_path)
        print(f"Loaded settings: {settings_path}")
    else:
        print(f"No settings file yet: {settings_path}")

    password = getpass.getpass(f"Instagram password for {username}: ")
    if not password:
        raise ValueError("Password is required.")

    logged_in = client.login(username, password, verification_code=verification_code)
    if not logged_in:
        raise RuntimeError("instagrapi login returned False.")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    client.dump_settings(settings_path)
    print(f"Saved settings: {settings_path}")
    return client


def print_profile_probe(client: Client, *, target: str, limit: int) -> None:
    user_id = client.user_id_from_username(target)
    user = client.user_info(user_id)
    print(f"Target: @{target}")
    print(f"User id: {user_id}")
    print(f"Followers: {getattr(user, 'follower_count', None)}")

    medias = client.user_clips(user_id, amount=limit)
    if not medias:
        medias = client.user_medias(user_id, amount=limit)

    print(f"Media count: {len(medias)}")
    for media in medias:
        print("---")
        print(f"pk: {media.pk}")
        print(f"id: {media.id}")
        print(f"code: {media.code}")
        print(f"type: {media.media_type}")
        print(f"permalink: https://www.instagram.com/p/{media.code}/")
        print(f"video_url: {media.video_url or ''}")
        print(f"thumbnail_url: {media.thumbnail_url or ''}")
        print(f"caption: {(media.caption_text or '')[:160]}")
        for index, resource in enumerate(media.resources or [], start=1):
            print(f"resource_{index}_type: {resource.media_type}")
            print(f"resource_{index}_video_url: {resource.video_url or ''}")
            print(f"resource_{index}_thumbnail_url: {resource.thumbnail_url or ''}")


def download_url(client: Client, *, url: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if "/reel/" in url:
        downloaded = client.clip_download_by_url(url, folder=output_dir)
    else:
        media_pk = client.media_pk_from_url(url)
        downloaded = client.video_download(media_pk, folder=output_dir)
    print(f"Downloaded: {downloaded}")


def main() -> int:
    args = build_parser().parse_args()
    settings_path = args.settings or default_settings_path(args.username)
    try:
        client = login_client(
            username=args.username,
            settings_path=settings_path,
            verification_code=args.verification_code,
        )
        print_profile_probe(client, target=args.target, limit=max(args.limit, 1))
        if args.download_url:
            download_url(client, url=args.download_url, output_dir=args.output)
    except ig_errors.TwoFactorRequired:
        print("FAILED: Two-factor authentication is required. Rerun with --verification-code CODE.")
        return 2
    except ig_errors.ChallengeRequired as exc:
        print(
            "FAILED: Instagram returned challenge_required. Complete the challenge in Instagram first."
        )
        print(str(exc)[:500])
        return 3
    except ig_errors.PleaseWaitFewMinutes as exc:
        print("FAILED: Instagram rate-limited the login/request. Wait before retrying.")
        print(str(exc)[:500])
        return 4
    except ig_errors.FeedbackRequired as exc:
        print("FAILED: Instagram returned feedback_required.")
        print(str(exc)[:500])
        return 5
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc.__class__.__name__}")
        print(str(exc)[:1000])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
