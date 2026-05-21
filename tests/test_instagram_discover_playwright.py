from __future__ import annotations

import json

from scripts.instagram_discover_playwright import count_new_urls, normalize_instagram_media_url, profile_url
from scripts.instagram_discover_playwright import merge_urls, read_url_file, write_urls
from scripts.instagram_inject_cookies import normalize_cookie_editor_export
from scripts.instagram_save_cookies import save_cookie_export
from scripts.instagram_scrape_urls import _filter_new_urls_for_account, _read_urls
from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.session import get_session, init_db


def test_normalize_instagram_media_url_accepts_profile_prefixed_reel() -> None:
    assert (
        normalize_instagram_media_url("https://www.instagram.com/meme.ig/reel/DYd2ApxOjyx/")
        == "https://www.instagram.com/reel/DYd2ApxOjyx/"
    )


def test_normalize_instagram_media_url_rejects_profile_root() -> None:
    assert normalize_instagram_media_url("https://www.instagram.com/meme.ig/") is None


def test_profile_url_normalizes_username() -> None:
    assert profile_url("@meme.ig/") == "https://www.instagram.com/meme.ig/"


def test_metadata_script_reads_json_url_lists(tmp_path) -> None:
    url_file = tmp_path / "urls.json"
    url_file.write_text(
        json.dumps(["https://www.instagram.com/reel/DYd2ApxOjyx/"]), encoding="utf-8"
    )

    assert _read_urls(urls=[], file_path=str(url_file)) == [
        "https://www.instagram.com/reel/DYd2ApxOjyx/"
    ]


def test_metadata_script_filters_urls_already_saved_for_account() -> None:
    init_db()
    with get_session() as session:
        account = Account(name="meme.ig", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            ScrapeCandidate(
                account_id=account.id,
                scrape_source_url="https://www.instagram.com/reel/existing123/",
                source_url="https://www.instagram.com/reel/existing123/",
                extractor="instagram",
                video_id="existing123",
            )
        )
        session.commit()

    assert _filter_new_urls_for_account(
        urls=[
            "https://www.instagram.com/reel/existing123/",
            "https://www.instagram.com/reel/new123/",
        ],
        account_name="meme.ig",
    ) == ["https://www.instagram.com/reel/new123/"]


def test_discovery_resume_reads_existing_json_urls(tmp_path) -> None:
    url_file = tmp_path / "urls.json"
    write_urls(
        url_file,
        [
            "https://www.instagram.com/meme.ig/reel/DYd2ApxOjyx/",
            "https://www.instagram.com/reel/DYdxGRpO7Am/",
        ],
    )

    assert read_url_file(url_file) == [
        "https://www.instagram.com/meme.ig/reel/DYd2ApxOjyx/",
        "https://www.instagram.com/reel/DYdxGRpO7Am/",
    ]


def test_merge_urls_dedupes_by_normalized_media_url() -> None:
    assert merge_urls(
        ["https://www.instagram.com/meme.ig/reel/DYd2ApxOjyx/"],
        {"https://www.instagram.com/reel/DYd2ApxOjyx/"},
    ) == ["https://www.instagram.com/reel/DYd2ApxOjyx/"]


def test_count_new_urls_uses_normalized_media_urls() -> None:
    assert (
        count_new_urls(
            ["https://www.instagram.com/meme.ig/reel/DYd2ApxOjyx/"],
            [
                "https://www.instagram.com/reel/DYd2ApxOjyx/",
                "https://www.instagram.com/reel/DYdxGRpO7Am/",
            ],
        )
        == 1
    )


def test_cookie_editor_export_normalizes_for_playwright() -> None:
    cookies = normalize_cookie_editor_export(
        [
            {
                "name": "sessionid",
                "value": "secret",
                "domain": "instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "no_restriction",
                "expirationDate": 1800000000.1,
            }
        ]
    )

    assert cookies == [
        {
            "name": "sessionid",
            "value": "secret",
            "domain": ".instagram.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
            "expires": 1800000000,
        }
    ]


def test_save_cookie_export_requires_sessionid(tmp_path) -> None:
    try:
        save_cookie_export(
            raw_json=json.dumps([{"name": "csrftoken", "value": "token"}]),
            output_path=tmp_path / "cookies.json",
        )
    except ValueError as exc:
        assert "sessionid" in str(exc)
    else:
        raise AssertionError("Expected missing sessionid to fail")
