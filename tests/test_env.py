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


def test_load_dotenv_strips_a_utf8_bom(monkeypatch, tmp_path: Path) -> None:
    """Notepad and PowerShell's Out-File write a UTF-8 BOM.

    Read as plain utf-8 the BOM stays on the first line, so the FIRST key in the
    file is parsed as "﻿GROQ_API_KEY" and every os.environ.get("GROQ_API_KEY")
    misses — the key looks configured but is invisible, with no error anywhere.

    Uses names no production code looks up: load_dotenv writes straight into
    os.environ, which monkeypatch cannot roll back for a var that did not exist
    beforehand, so a real key name here would leak into every later test.
    """
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "NICHEFLOW_TEST_BOM_FIRST=first-key\nNICHEFLOW_TEST_BOM_SECOND=second-key\n",
        encoding="utf-8-sig",  # writes the BOM
    )

    import os

    try:
        load_dotenv(dotenv_path)

        # The first key is the one a BOM corrupts; the second proves the rest of
        # the file was never affected.
        assert os.environ["NICHEFLOW_TEST_BOM_FIRST"] == "first-key"
        assert os.environ["NICHEFLOW_TEST_BOM_SECOND"] == "second-key"
        assert not [key for key in os.environ if key.startswith("﻿")]
    finally:
        os.environ.pop("NICHEFLOW_TEST_BOM_FIRST", None)
        os.environ.pop("NICHEFLOW_TEST_BOM_SECOND", None)


def test_load_dotenv_does_not_override_existing_env(monkeypatch, tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("GROQ_API_KEY=file-value", encoding="utf-8")
    monkeypatch.setenv("GROQ_API_KEY", "existing-value")

    load_dotenv(dotenv_path)

    assert __import__("os").environ["GROQ_API_KEY"] == "existing-value"
