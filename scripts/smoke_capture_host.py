"""Verify the packaged capture host exposes the current dashboard contract."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path


def request_dashboard(host_exe: Path) -> dict:
    payload = json.dumps({"action": "get_dashboard"}).encode("utf-8")
    result = subprocess.run(
        [str(host_exe)],
        input=struct.pack("<I", len(payload)) + payload,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    if len(result.stdout) < 4:
        raise RuntimeError("Capture host returned no Native Messaging response.")
    length = struct.unpack("<I", result.stdout[:4])[0]
    response = json.loads(result.stdout[4 : 4 + length].decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("Capture host response must be an object.")
    return response


def validate_dashboard_response(response: dict) -> None:
    if response.get("ok") is not True:
        raise RuntimeError(str(response.get("error") or "Capture host dashboard request failed."))
    pools = response.get("dashboard", {}).get("pools", {})
    if not isinstance(pools, dict) or not pools:
        raise RuntimeError("Capture host dashboard contains no pools.")
    missing_accounts = [
        niche
        for niche, pool in pools.items()
        if not isinstance(pool, dict) or not isinstance(pool.get("accounts"), list)
    ]
    if missing_accounts:
        raise RuntimeError(
            "Capture host dashboard is missing account targets for: "
            + ", ".join(sorted(missing_accounts))
        )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_capture_host.py <NicheFlowCaptureHost.exe>")
    response = request_dashboard(Path(sys.argv[1]))
    validate_dashboard_response(response)
    print("Capture host smoke check passed: pool account targets are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
