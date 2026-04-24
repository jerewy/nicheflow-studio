from __future__ import annotations

from pathlib import Path

from nicheflow_studio.core.env import load_dotenv


def test_load_dotenv_reads_groq_api_key(monkeypatch, tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                "# comment",
                "GROQ_API_KEY=test-groq-key",
                "OTHER_VALUE=\"quoted value\"",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OTHER_VALUE", raising=False)

    load_dotenv(dotenv_path)

    assert __import__("os").environ["GROQ_API_KEY"] == "test-groq-key"
    assert __import__("os").environ["OTHER_VALUE"] == "quoted value"


def test_load_dotenv_does_not_override_existing_env(monkeypatch, tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("GROQ_API_KEY=file-value", encoding="utf-8")
    monkeypatch.setenv("GROQ_API_KEY", "existing-value")

    load_dotenv(dotenv_path)

    assert __import__("os").environ["GROQ_API_KEY"] == "existing-value"
