from __future__ import annotations

import json
import sys
from pathlib import Path

from nicheflow_studio.core import instagram_session
from scripts.instagram_discover_playwright import (
    count_new_urls,
    effective_resume_limit,
    normalize_instagram_media_url,
    normalize_instagram_reel_url,
    profile_url,
    save_candidates,
    should_fast_forward_through_cache,
)
from scripts.instagram_discover_playwright import merge_urls, read_url_file, write_urls
from scripts.instagram_inject_cookies import normalize_cookie_editor_export
from scripts.instagram_save_cookies import save_cookie_export
from scripts.instagram_scrape_urls import (
    _extract_instagram_metadata,
    _filter_new_urls_for_account,
    _read_urls,
    main,
)
from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.scraper.instagram import InstagramRateLimitError, InstagramScrapeStats


def test_normalize_instagram_media_url_accepts_profile_prefixed_reel() -> None:
    assert (
        normalize_instagram_media_url("https://www.instagram.com/meme.ig/reel/DYd2ApxOjyx/")
        == "https://www.instagram.com/reel/DYd2ApxOjyx/"
    )


def test_normalize_instagram_media_url_rejects_profile_root() -> None:
    assert normalize_instagram_media_url("https://www.instagram.com/meme.ig/") is None


def test_normalize_instagram_reel_url_rejects_posts() -> None:
    assert normalize_instagram_reel_url("https://www.instagram.com/p/DYfgIZvRato/") is None
    assert (
        normalize_instagram_reel_url("https://www.instagram.com/meme.ig/reel/DYd2ApxOjyx/")
        == "https://www.instagram.com/reel/DYd2ApxOjyx/"
    )


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
        "https://www.instagram.com/reel/DYd2ApxOjyx/",
        "https://www.instagram.com/reel/DYdxGRpO7Am/",
    ]


def test_discovery_resume_filters_cached_posts_to_reels(tmp_path) -> None:
    url_file = tmp_path / "urls.json"
    write_urls(
        url_file,
        [
            "https://www.instagram.com/reel/DYd2ApxOjyx/",
            "https://www.instagram.com/p/DYfgIZvRato/",
        ],
    )

    assert read_url_file(url_file) == ["https://www.instagram.com/reel/DYd2ApxOjyx/"]


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


def test_resume_limit_adds_capacity_beyond_cached_urls() -> None:
    assert effective_resume_limit(requested_limit=160, initial_count=120) == 280
    assert effective_resume_limit(requested_limit=160, initial_count=0) == 160


def test_fast_forward_only_while_cache_has_no_new_urls() -> None:
    assert (
        should_fast_forward_through_cache(
            baseline_count=120,
            new_this_run=0,
            previous_new_this_run=0,
        )
        is True
    )
    assert (
        should_fast_forward_through_cache(
            baseline_count=120,
            new_this_run=1,
            previous_new_this_run=0,
        )
        is False
    )
    assert (
        should_fast_forward_through_cache(
            baseline_count=0,
            new_this_run=0,
            previous_new_this_run=0,
        )
        is False
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


def test_cookie_editor_export_converts_to_yt_dlp_cookiefile(tmp_path, monkeypatch) -> None:
    source = tmp_path / "instagram-cookies.json"
    dest = tmp_path / "instagram-cookies.txt"
    source.write_text(
        json.dumps(
            [
                {
                    "name": "sessionid",
                    "value": "secret",
                    "domain": "instagram.com",
                    "path": "/",
                    "secure": True,
                    "expirationDate": 1800000000.1,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(instagram_session, "_COOKIES_JSON_PATH", source)
    monkeypatch.setattr(instagram_session, "_COOKIES_TXT_PATH", dest)

    cookiefile = instagram_session.get_instagram_yt_dlp_cookiefile()

    assert cookiefile == str(dest)
    assert dest.read_text(encoding="utf-8") == (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1800000000\tsessionid\tsecret\n"
    )
    status = instagram_session.instagram_yt_dlp_cookie_status()
    assert status.cookiefile == str(dest)
    assert status.has_sessionid is True


def test_cookie_status_rejects_cookiefile_without_sessionid(tmp_path, monkeypatch) -> None:
    source = tmp_path / "instagram-cookies.json"
    dest = tmp_path / "instagram-cookies.txt"
    source.write_text(
        json.dumps(
            [
                {
                    "name": "csrftoken",
                    "value": "token",
                    "domain": "instagram.com",
                    "path": "/",
                    "secure": True,
                    "expirationDate": 1800000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(instagram_session, "_COOKIES_JSON_PATH", source)
    monkeypatch.setattr(instagram_session, "_COOKIES_TXT_PATH", dest)

    status = instagram_session.instagram_yt_dlp_cookie_status()

    assert status.cookiefile == str(dest)
    assert status.has_sessionid is False


def _write_storage_state(
    root: Path,
    profile: str,
    *,
    sessionid: str | None = "sid",
    ds_user_id: str | None = None,
) -> None:
    """Create a minimal Playwright storage-state.json for a profile under ``root``."""
    cookies: list[dict] = []
    if sessionid is not None:
        cookies.append(
            {
                "name": "sessionid",
                "value": sessionid,
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "expires": 1800000000.0,
            }
        )
    if ds_user_id is not None:
        cookies.append(
            {
                "name": "ds_user_id",
                "value": ds_user_id,
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "expires": 1800000000.0,
            }
        )
    cookies.append(
        {
            "name": "csrftoken",
            "value": "tok",
            "domain": ".instagram.com",
            "path": "/",
            "secure": True,
            "expires": 1800000000.0,
        }
    )
    path = root / profile / "storage-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")


def test_per_profile_cookiefile_built_from_storage_state(tmp_path, monkeypatch) -> None:
    root = tmp_path / "instagram"
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", root)
    _write_storage_state(root, "acct1", sessionid="abc")

    cookiefile = instagram_session.get_instagram_yt_dlp_cookiefile_for_profile("acct1")

    assert cookiefile is not None
    assert "sessionid\tabc" in Path(cookiefile).read_text(encoding="utf-8")


def test_per_profile_cookiefile_none_without_sessionid(tmp_path, monkeypatch) -> None:
    root = tmp_path / "instagram"
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", root)
    _write_storage_state(root, "acct1", sessionid=None)

    assert instagram_session.get_instagram_yt_dlp_cookiefile_for_profile("acct1") is None


def test_best_cookiefile_uses_sourcing_account_never_the_requested(tmp_path, monkeypatch) -> None:
    """Account-safety: downloads must use a designated sourcing account, never the
    clip's own publishing account — even when it's passed as preferred_profile."""
    root = tmp_path / "instagram"
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", root)
    monkeypatch.setenv("NICHEFLOW_IG_SOURCING_PROFILES", "sourcing_acct")
    _write_storage_state(root, "publish_acct", sessionid="PUB")
    _write_storage_state(root, "sourcing_acct", sessionid="SRC")

    cookiefile = instagram_session.best_instagram_yt_dlp_cookiefile(preferred_profile="publish_acct")

    assert cookiefile is not None
    text = Path(cookiefile).read_text(encoding="utf-8")
    assert "sessionid\tSRC" in text  # used the sourcing account
    assert "sessionid\tPUB" not in text  # never the requested publishing account


def test_best_cookiefile_never_borrows_a_non_sourcing_account(tmp_path, monkeypatch) -> None:
    """If no sourcing account is available, downloads must NOT silently borrow some
    other logged-in (publishing) account — that's the automation risk we removed.
    With no shared export either, it returns None rather than risk an account."""
    root = tmp_path / "instagram"
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", root)
    monkeypatch.setenv("NICHEFLOW_IG_SOURCING_PROFILES", "sourcing_acct")
    # Only a non-sourcing publishing account is logged in; no sourcing account.
    _write_storage_state(root, "publish_acct", sessionid="PUB")
    monkeypatch.setattr(instagram_session, "_COOKIES_JSON_PATH", tmp_path / "missing.json")

    cookiefile = instagram_session.best_instagram_yt_dlp_cookiefile(preferred_profile="publish_acct")

    assert cookiefile is None  # refused to authenticate as the publishing account


def test_best_cookiefile_falls_back_to_shared_export(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", tmp_path / "no_profiles")
    source = tmp_path / "instagram-cookies.json"
    dest = tmp_path / "instagram-cookies.txt"
    source.write_text(
        json.dumps(
            [
                {
                    "name": "sessionid",
                    "value": "shared",
                    "domain": "instagram.com",
                    "path": "/",
                    "secure": True,
                    "expirationDate": 1800000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(instagram_session, "_COOKIES_JSON_PATH", source)
    monkeypatch.setattr(instagram_session, "_COOKIES_TXT_PATH", dest)

    cookiefile = instagram_session.best_instagram_yt_dlp_cookiefile(preferred_profile="missing")

    assert cookiefile == str(dest)


def test_best_cookiefile_refuses_legacy_export_of_a_publishing_account(
    tmp_path, monkeypatch
) -> None:
    """If the hand-refreshed Cookie-Editor export carries a logged-in PUBLISHING
    account's session (matched by ds_user_id), the fallback must refuse it —
    silently downloading as that account is the automation-flag risk."""
    root = tmp_path / "instagram"
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", root)
    monkeypatch.setenv("NICHEFLOW_IG_SOURCING_PROFILES", "sourcing_acct")
    # Only a publishing profile is logged in; its user id is 111.
    _write_storage_state(root, "publish_acct", sessionid="PUB", ds_user_id="111")

    source = tmp_path / "instagram-cookies.json"
    dest = tmp_path / "instagram-cookies.txt"
    source.write_text(
        json.dumps(
            [
                {"name": "sessionid", "value": "PUB", "domain": ".instagram.com"},
                {"name": "ds_user_id", "value": "111", "domain": ".instagram.com"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(instagram_session, "_COOKIES_JSON_PATH", source)
    monkeypatch.setattr(instagram_session, "_COOKIES_TXT_PATH", dest)

    assert instagram_session.best_instagram_yt_dlp_cookiefile() is None


def test_best_cookiefile_accepts_legacy_export_of_an_unknown_account(
    tmp_path, monkeypatch
) -> None:
    """A legacy export whose ds_user_id matches no logged-in publishing profile
    (e.g. a dedicated throwaway sourcing login) is still accepted."""
    root = tmp_path / "instagram"
    monkeypatch.setattr(instagram_session, "_INSTAGRAM_PROFILES_ROOT", root)
    monkeypatch.setenv("NICHEFLOW_IG_SOURCING_PROFILES", "sourcing_acct")
    _write_storage_state(root, "publish_acct", sessionid="PUB", ds_user_id="111")

    source = tmp_path / "instagram-cookies.json"
    dest = tmp_path / "instagram-cookies.txt"
    source.write_text(
        json.dumps(
            [
                {"name": "sessionid", "value": "OTHER", "domain": ".instagram.com"},
                {"name": "ds_user_id", "value": "999", "domain": ".instagram.com"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(instagram_session, "_COOKIES_JSON_PATH", source)
    monkeypatch.setattr(instagram_session, "_COOKIES_TXT_PATH", dest)

    assert instagram_session.best_instagram_yt_dlp_cookiefile() == str(dest)


def test_metadata_script_rate_limit_without_candidates_exits_cleanly(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    url_file = tmp_path / "urls.json"
    url_file.write_text(
        json.dumps(["https://www.instagram.com/reel/rateLimited/"]),
        encoding="utf-8",
    )

    def raise_rate_limit(*args, **kwargs):  # noqa: ANN001, ARG001
        raise InstagramRateLimitError(
            "Instagram rate limit hit. Wait 15-30 min before running again.",
            [],
            InstagramScrapeStats(input_urls=1, extraction_limit=1, attempted=1, failed_rate_limited=1),
        )

    monkeypatch.setattr(
        "scripts.instagram_scrape_urls.scrape_instagram_urls_instaloader",
        raise_rate_limit,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "instagram_scrape_urls.py",
            "--file",
            str(url_file),
            "--save-account",
            "meme.ig",
            "--metadata-extractor",
            "instaloader",
        ],
    )

    assert main() == 0
    output = capsys.readouterr().out
    assert "WARNING: Instagram rate limit hit. Wait 15-30 min before running again." in output
    assert "Metadata funnel:" in output
    assert "- attempted: 1" in output
    assert "- stopped by rate limit: yes" in output
    assert "No candidates collected before rate limit. Nothing to save." in output


def test_metadata_extraction_defaults_to_apify(monkeypatch, capsys) -> None:
    expected_candidates = [object()]

    def fake_apify(urls, *, results_limit):  # noqa: ANN001
        assert urls == ["https://www.instagram.com/reel/abc/"]
        assert results_limit == 1
        return expected_candidates

    def fail_instaloader(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("instaloader should not run by default")

    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_apify", fake_apify)
    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_instaloader", fail_instaloader)

    candidates, stats, extractor_name, used_fallback = _extract_instagram_metadata(
        ["https://www.instagram.com/reel/abc/"],
        extraction_limit=1,
        max_age_days=None,
    )

    assert candidates == expected_candidates
    assert stats.input_urls == 1
    assert stats.extraction_limit == 1
    assert stats.attempted == 1
    assert stats.extracted == 1
    assert extractor_name == "apify"
    assert used_fallback is False
    output = capsys.readouterr().out
    assert "metadata extractor: apify" in output


def test_metadata_extraction_can_use_instaloader_explicitly(monkeypatch, capsys) -> None:
    expected_stats = InstagramScrapeStats(input_urls=1, extraction_limit=1, attempted=1, extracted=1)
    expected_candidates = [object()]

    def fake_instaloader(*args, **kwargs):  # noqa: ANN001, ARG001
        return expected_candidates, expected_stats, True

    def fail_yt_dlp(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("yt-dlp fallback should not run when instaloader succeeds")

    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_instaloader", fake_instaloader)
    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_with_stats", fail_yt_dlp)

    candidates, stats, extractor_name, used_fallback = _extract_instagram_metadata(
        ["https://www.instagram.com/reel/abc/"],
        extraction_limit=1,
        max_age_days=None,
        metadata_extractor="instaloader",
    )

    assert candidates == expected_candidates
    assert stats == expected_stats
    assert extractor_name == "instaloader"
    assert used_fallback is False
    output = capsys.readouterr().out
    assert "metadata extractor: instaloader" in output
    assert "instaloader Playwright sessionid injected: yes" in output


def test_metadata_extraction_falls_back_to_yt_dlp(monkeypatch, capsys) -> None:
    expected_stats = InstagramScrapeStats(input_urls=1, extraction_limit=1, attempted=1, extracted=1)
    expected_candidates = [object()]

    def fail_instaloader(*args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("instaloader unavailable")

    def fake_yt_dlp(*args, **kwargs):  # noqa: ANN001, ARG001
        return expected_candidates, expected_stats

    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_instaloader", fail_instaloader)
    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_with_stats", fake_yt_dlp)

    candidates, stats, extractor_name, used_fallback = _extract_instagram_metadata(
        ["https://www.instagram.com/reel/abc/"],
        extraction_limit=1,
        max_age_days=None,
        metadata_extractor="instaloader",
    )

    assert candidates == expected_candidates
    assert stats == expected_stats
    assert extractor_name == "yt-dlp"
    assert used_fallback is True
    output = capsys.readouterr().out
    assert "falling back to yt-dlp" in output
    assert "metadata extractor: yt-dlp fallback" in output


def test_metadata_extraction_can_use_apify(monkeypatch, capsys) -> None:
    expected_candidates = [object()]

    def fake_apify(urls, *, results_limit):  # noqa: ANN001
        assert urls == ["https://www.instagram.com/reel/abc/"]
        assert results_limit == 1
        return expected_candidates

    def fail_instaloader(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("instaloader should not run when apify is selected")

    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_apify", fake_apify)
    monkeypatch.setattr("scripts.instagram_scrape_urls.scrape_instagram_urls_instaloader", fail_instaloader)

    candidates, stats, extractor_name, used_fallback = _extract_instagram_metadata(
        ["https://www.instagram.com/reel/abc/"],
        extraction_limit=1,
        max_age_days=None,
        metadata_extractor="apify",
    )

    assert candidates == expected_candidates
    assert stats.input_urls == 1
    assert stats.extraction_limit == 1
    assert stats.attempted == 1
    assert stats.extracted == 1
    assert extractor_name == "apify"
    assert used_fallback is False
    output = capsys.readouterr().out
    assert "metadata extractor: apify" in output


def test_discovery_save_candidates_passes_metadata_extractor(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_run(command, *, check):  # noqa: ANN001
        calls.append((command, check))

    monkeypatch.setattr("scripts.instagram_discover_playwright.subprocess.run", fake_run)
    url_file = tmp_path / "urls.json"

    save_candidates(
        url_file=url_file,
        limit=5,
        account_name="pastmomentsdaily",
        metadata_extractor="apify",
    )

    assert len(calls) == 1
    command, check = calls[0]
    assert check is True
    assert "--metadata-extractor" in command
    assert command[command.index("--metadata-extractor") + 1] == "apify"


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
