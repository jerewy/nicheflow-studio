"""Chrome/Edge Native Messaging host for one-click NicheFlow pool capture."""

from __future__ import annotations

import json
import os
import pathlib
import struct
import sys
from typing import BinaryIO

from nicheflow_studio.core.env import load_dotenv

CONFIG_PATH = (
    pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home()))
    / "NicheFlow Studio"
    / "capture-host.json"
)


def _load_host_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    parsed = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _prepare_runtime() -> None:
    config = _load_host_config()
    data_dir = config.get("data_dir")
    dotenv_path = config.get("dotenv_path")
    if isinstance(data_dir, str) and data_dir.strip():
        os.environ["NICHEFLOW_DATA_DIR"] = data_dir
    if isinstance(dotenv_path, str) and dotenv_path.strip():
        load_dotenv(pathlib.Path(dotenv_path))


def read_native_message(stream: BinaryIO) -> dict | None:
    raw_length = stream.read(4)
    if not raw_length:
        return None
    if len(raw_length) != 4:
        raise ValueError("Incomplete Native Messaging message length.")
    length = struct.unpack("<I", raw_length)[0]
    raw_payload = stream.read(length)
    if len(raw_payload) != length:
        raise ValueError("Incomplete Native Messaging message payload.")
    parsed = json.loads(raw_payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Native Messaging payload must be an object.")
    return parsed


def write_native_message(stream: BinaryIO, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(encoded)))
    stream.write(encoded)
    stream.flush()


def handle_message(message: dict) -> dict:
    action = message.get("action")
    if action == "get_dashboard":
        try:
            from nicheflow_studio.services.pool_capture import capture_dashboard  # noqa: E501

            return {"ok": True, "dashboard": capture_dashboard()}
        except Exception as exc:  # noqa: BLE001,E501 - extension needs clean response
            return {"ok": False, "error": str(exc)}
    if action == "capture_batch":
        items = message.get("items")
        if not isinstance(items, list):
            return {"ok": False, "error": "Capture batch must contain an items list."}  # noqa: E501
        try:
            from nicheflow_studio.services.pool_capture import (
                capture_instagram_reels_to_pool,
            )

            return {"ok": True, "batch": capture_instagram_reels_to_pool(items)}  # noqa: E501
        except Exception as exc:  # noqa: BLE001,E501 - extension needs clean response
            return {"ok": False, "error": str(exc)}
    if action != "capture_to_pool":
        return {"ok": False, "error": "Unsupported capture action."}
    url = message.get("url")
    niche = message.get("niche", "history")
    pinned_account_id = message.get("pinned_account_id")
    if not isinstance(url, str) or not url.strip():
        return {
            "ok": False,
            "error": "The active tab has no URL to capture.",
        }
    if not isinstance(niche, str):
        return {"ok": False, "error": "Invalid pool niche."}

    try:
        from nicheflow_studio.services.pool_capture import (
            capture_instagram_reel_to_pool,
        )

        result = capture_instagram_reel_to_pool(
            url, niche=niche, pinned_account_id=pinned_account_id
        )
    except Exception as exc:  # noqa: BLE001,E501 - extension needs a clean error response
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": result}


def main() -> int:
    _prepare_runtime()
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    while message := read_native_message(input_stream):
        write_native_message(output_stream, handle_message(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
