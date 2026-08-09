from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(dotenv_path: Path | None = None) -> None:
    target_path = dotenv_path or Path.cwd() / ".env"
    if not target_path.exists():
        return

    # utf-8-sig, not utf-8: Notepad and PowerShell's Out-File write a UTF-8 BOM,
    # and plain "utf-8" keeps it as a character on the first line — so the FIRST
    # key in the file silently becomes "﻿GROQ_API_KEY" and every lookup of
    # "GROQ_API_KEY" misses. Decoding as utf-8-sig drops the BOM if present and
    # is a no-op otherwise.
    for raw_line in target_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key or normalized_key in os.environ:
            continue

        normalized_value = value.strip()
        if len(normalized_value) >= 2 and normalized_value[0] == normalized_value[-1] and normalized_value[0] in {"'", '"'}:
            normalized_value = normalized_value[1:-1]
        os.environ[normalized_key] = normalized_value
