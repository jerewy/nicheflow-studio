from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(dotenv_path: Path | None = None) -> None:
    target_path = dotenv_path or Path.cwd() / ".env"
    if not target_path.exists():
        return

    for raw_line in target_path.read_text(encoding="utf-8").splitlines():
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
