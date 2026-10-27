from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nicheflow_studio.core import env as env_module
from nicheflow_studio.core.paths import settings_env_path
from nicheflow_studio.services import app_settings
from nicheflow_studio.services.app_settings import AppSettingsError

_KEYS = [field.key for field in app_settings.FIELDS] + ["NICHEFLOW_DATA_DIR"]


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv-then-delenv makes monkeypatch remember each key as "absent", so
    # anything the code under test writes into os.environ is removed at
    # teardown instead of leaking into later tests.
    for key in _KEYS:
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    monkeypatch.setattr(env_module, "_PREEXISTING_KEYS", frozenset())


def _field(status: dict, key: str) -> dict:
    return next(field for field in status["fields"] if field["key"] == key)


def test_saving_a_secret_stores_it_applies_it_and_masks_it_in_status() -> None:
    status = app_settings.save_settings({"APIFY_TOKEN": "apify_api_abcdefghijklmnop1234"})

    field = _field(status, "APIFY_TOKEN")
    assert field["is_set"] is True
    assert field["source"] == "app"
    assert field["value"] is None
    assert field["hint"] == "…1234"
    assert os.environ["APIFY_TOKEN"] == "apify_api_abcdefghijklmnop1234"
    assert "APIFY_TOKEN=apify_api_abcdefghijklmnop1234" in settings_env_path().read_text("utf-8")


def test_status_never_contains_a_secret_value() -> None:
    app_settings.save_settings({"CLOUDFLARE_PUBLISHER_API_KEY": "super-secret-worker-key-9876"})

    assert "super-secret-worker-key" not in json.dumps(app_settings.settings_status())


def test_short_secret_gets_no_partial_hint() -> None:
    status = app_settings.save_settings({"GROQ_API_KEY": "short"})

    assert _field(status, "GROQ_API_KEY")["hint"] == "set"


def test_fields_left_out_of_a_save_are_untouched() -> None:
    app_settings.save_settings({"APIFY_TOKEN": "first-token-value-0001"})

    app_settings.save_settings({"CLOUDFLARE_PUBLISHER_URL": "https://worker.example.workers.dev/"})

    stored = env_module.read_env_file(settings_env_path())
    assert stored["APIFY_TOKEN"] == "first-token-value-0001"
    assert stored["CLOUDFLARE_PUBLISHER_URL"] == "https://worker.example.workers.dev"


def test_clearing_a_key_falls_back_to_the_repo_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APIFY_TOKEN=repo-env-token-value\n", encoding="utf-8")
    app_settings.save_settings({"APIFY_TOKEN": "app-token-value-5555"})

    status = app_settings.save_settings({"APIFY_TOKEN": ""})

    assert "APIFY_TOKEN" not in env_module.read_env_file(settings_env_path())
    assert os.environ["APIFY_TOKEN"] == "repo-env-token-value"
    assert _field(status, "APIFY_TOKEN")["source"] == "env_file"


def test_clearing_a_key_with_no_fallback_unsets_it() -> None:
    app_settings.save_settings({"APIFY_TOKEN": "app-token-value-5555"})

    status = app_settings.save_settings({"APIFY_TOKEN": ""})

    assert "APIFY_TOKEN" not in os.environ
    assert _field(status, "APIFY_TOKEN")["is_set"] is False


def test_a_real_environment_variable_wins_over_a_saved_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "system-token-value-0000")
    monkeypatch.setattr(env_module, "_PREEXISTING_KEYS", frozenset({"APIFY_TOKEN"}))

    status = app_settings.save_settings({"APIFY_TOKEN": "app-token-value-5555"})

    assert os.environ["APIFY_TOKEN"] == "system-token-value-0000"
    assert _field(status, "APIFY_TOKEN")["source"] == "system"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"NOT_A_SETTING": "x"}, "Unknown setting"),
        ({"APIFY_TOKEN": "has a space"}, "cannot contain spaces"),
        ({"APIFY_TOKEN": "line\nbreak"}, "single line"),
        ({"CLOUDFLARE_PUBLISHER_URL": "worker.example.dev"}, "https://"),
        ({"CLOUDFLARE_PUBLISHER_URL": "http://worker.example.dev"}, "https://"),
        ({"CLOUDFLARE_PUBLISH_ACCOUNTS": "[1, 2]"}, "JSON object"),
        ({"CLOUDFLARE_PUBLISH_ACCOUNTS": '{"seven": "acct"}'}, "must be a number"),
        ({"CLOUDFLARE_PUBLISH_ACCOUNTS": '{"7": "bad key"}'}, "lowercase letters"),
        # The Worker's normalizeAccountKey rejects dots, unlike Instagram handles.
        ({"CLOUDFLARE_PUBLISH_ACCOUNTS": '{"7": "dotted.key"}'}, "lowercase letters"),
        ({"NICHEFLOW_IG_SOURCING_PROFILES": "good, bad handle"}, "letters, digits"),
    ],
)
def test_invalid_values_are_rejected_and_nothing_is_written(values: dict, message: str) -> None:
    with pytest.raises(AppSettingsError, match=message):
        app_settings.save_settings(values)

    assert not settings_env_path().exists()


def test_local_http_worker_url_is_allowed_for_wrangler_dev() -> None:
    status = app_settings.save_settings({"CLOUDFLARE_PUBLISHER_URL": "http://127.0.0.1:8787"})

    assert _field(status, "CLOUDFLARE_PUBLISHER_URL")["value"] == "http://127.0.0.1:8787"


def test_account_map_is_normalized_and_empty_rows_dropped() -> None:
    app_settings.save_settings(
        {"CLOUDFLARE_PUBLISH_ACCOUNTS": '{"12": "secondacct", "7": " FirstAcct ", "9": ""}'}
    )

    assert json.loads(os.environ["CLOUDFLARE_PUBLISH_ACCOUNTS"]) == {
        "12": "secondacct",
        "7": "firstacct",
    }


def test_sourcing_profiles_are_trimmed_and_lose_the_at_sign() -> None:
    app_settings.save_settings({"NICHEFLOW_IG_SOURCING_PROFILES": " @first ,second,, "})

    assert os.environ["NICHEFLOW_IG_SOURCING_PROFILES"] == "first,second"


def test_load_app_env_prefers_system_then_settings_file_then_repo_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "APIFY_TOKEN=repo\nGROQ_API_KEY=repo\nCLOUDFLARE_PUBLISHER_URL=repo\n",
        encoding="utf-8",
    )
    settings = settings_env_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("APIFY_TOKEN=app\nGROQ_API_KEY=app\n", encoding="utf-8")
    monkeypatch.setenv("APIFY_TOKEN", "system")

    env_module.load_app_env()

    assert os.environ["APIFY_TOKEN"] == "system"
    assert os.environ["GROQ_API_KEY"] == "app"
    assert os.environ["CLOUDFLARE_PUBLISHER_URL"] == "repo"
    assert "APIFY_TOKEN" in env_module.preexisting_env_keys()


def test_load_app_env_reads_the_settings_file_from_a_data_dir_set_in_repo_env(
    tmp_path: Path,
) -> None:
    custom_data = tmp_path / "elsewhere"
    custom_data.mkdir()
    (custom_data / "settings.env").write_text("GROQ_API_KEY=from-custom-dir\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"NICHEFLOW_DATA_DIR={custom_data}\n", encoding="utf-8")

    env_module.load_app_env()

    assert os.environ["GROQ_API_KEY"] == "from-custom-dir"


def test_cloudflare_check_without_keys_reports_failure_instead_of_raising() -> None:
    result = app_settings.check_connection("cloudflare")

    assert result == {"ok": False, "message": "Add the Worker URL and API key first."}


def test_cloudflare_check_calls_the_worker_usage_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    app_settings.save_settings(
        {
            "CLOUDFLARE_PUBLISHER_URL": "https://worker.example.workers.dev",
            "CLOUDFLARE_PUBLISHER_API_KEY": "worker-key-value-1234",
        }
    )
    monkeypatch.setattr(app_settings.cloud_publisher, "get_usage", lambda: {"active_jobs": 3})

    result = app_settings.check_connection("cloudflare")

    assert result["ok"] is True
    assert "3 active job(s)" in result["message"]


def test_cloudflare_check_surfaces_worker_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    app_settings.save_settings(
        {
            "CLOUDFLARE_PUBLISHER_URL": "https://worker.example.workers.dev",
            "CLOUDFLARE_PUBLISHER_API_KEY": "wrong-key-value-1234",
        }
    )

    def reject() -> dict:
        raise app_settings.cloud_publisher.CloudPublisherError("401 unauthorized")

    monkeypatch.setattr(app_settings.cloud_publisher, "get_usage", reject)

    assert app_settings.check_connection("cloudflare") == {"ok": False, "message": "401 unauthorized"}


def test_apify_check_without_token_reports_failure() -> None:
    assert app_settings.check_connection("apify") == {"ok": False, "message": "Add an Apify token first."}


def test_unknown_service_check_is_an_error() -> None:
    with pytest.raises(AppSettingsError):
        app_settings.check_connection("instagram")
