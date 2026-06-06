from __future__ import annotations

import subprocess

from nicheflow_studio.publisher import instagram_web


def test_launch_instagram_upload_assist_requests_minimized_by_default(monkeypatch) -> None:
    commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, **_kwargs) -> None:  # noqa: ANN001
            commands.append(command)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    instagram_web.launch_instagram_upload_assist(profile_name="alt1")

    assert commands
    assert "--profile" in commands[0]
    assert "alt1" in commands[0]
    assert "--minimized" in commands[0]


def test_launch_instagram_upload_assist_can_open_foreground(monkeypatch) -> None:
    commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, **_kwargs) -> None:  # noqa: ANN001
            commands.append(command)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    instagram_web.launch_instagram_upload_assist(profile_name="alt1", minimized=False)

    assert commands
    assert "--minimized" not in commands[0]
