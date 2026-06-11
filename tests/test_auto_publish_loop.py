from __future__ import annotations

import threading
import time

import pytest

from nicheflow_studio.app import webview_app
from nicheflow_studio.services.auto_publish_loop import AutoPublishLoop


def test_enabled_pref_publishes_once_per_tick() -> None:
    calls: list[str] = []
    loop = AutoPublishLoop(
        enabled=lambda: True,
        publish=lambda: calls.append("published") or {"posted": 0, "failed": 0},
    )

    assert loop.tick() is True

    assert calls == ["published"]


def test_disabled_pref_never_publishes() -> None:
    calls: list[str] = []
    loop = AutoPublishLoop(
        enabled=lambda: False,
        publish=lambda: calls.append("published") or {"posted": 0, "failed": 0},
    )

    assert loop.tick() is True

    assert calls == []


def test_stop_event_terminates_thread_within_one_second() -> None:
    loop = AutoPublishLoop(
        interval_seconds=60,
        enabled=lambda: False,
        publish=lambda: {"posted": 0, "failed": 0},
    )
    loop.start()

    started = time.monotonic()
    loop.stop()

    assert time.monotonic() - started < 1
    assert loop._thread is not None
    assert not loop._thread.is_alive()


def test_tick_while_previous_tick_runs_is_skipped_not_queued() -> None:
    publish_started = threading.Event()
    release_publish = threading.Event()
    calls: list[str] = []

    def publish() -> dict:
        calls.append("published")
        publish_started.set()
        release_publish.wait(1)
        return {"posted": 0, "failed": 0}

    loop = AutoPublishLoop(enabled=lambda: True, publish=publish)
    first_tick = threading.Thread(target=loop.tick)
    first_tick.start()
    assert publish_started.wait(1)

    assert loop.tick() is False
    release_publish.set()
    first_tick.join(1)

    assert calls == ["published"]


def test_webview_app_owns_loop_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeLoop:
        def start(self) -> None:
            calls.append("start")

        def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr(webview_app, "load_dotenv", lambda: None)
    monkeypatch.setattr(webview_app, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(webview_app, "configure_logging", lambda: None)
    monkeypatch.setattr(webview_app, "init_db", lambda: calls.append("db"))
    monkeypatch.setattr(webview_app, "run_startup_backup", lambda: calls.append("backup"))
    monkeypatch.setattr(webview_app, "start_sidecar_update", lambda: calls.append("update"))
    monkeypatch.setattr(webview_app, "AutoPublishLoop", FakeLoop)
    monkeypatch.setattr(webview_app, "_run_started_webview", lambda: calls.append("webview"))

    webview_app.run_webview()

    assert calls == ["db", "backup", "update", "start", "webview", "stop"]
