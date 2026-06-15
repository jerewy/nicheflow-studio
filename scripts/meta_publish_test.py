"""One-shot Instagram Graph API publish spike (Phase 0).

Proves the Graph API publish path works for one account WITHOUT wiring anything
into the app yet:

  1. Serve a local reel over HTTP on --port (so ngrok can expose it publicly).
  2. Create a Reels media container (Meta fetches the public video_url).
  3. Poll the container until status_code == FINISHED.
  4. ONLY with --publish: call media_publish to post it LIVE.

Dry run by default: it stops after the container reaches FINISHED, so NOTHING is
posted. Reaching FINISHED already proves the token, IG user id, public fetch, and
video format are all good. Add --publish only when you actually want it live.

Prerequisites:
  - ngrok forwarding your static domain to the SAME --port, e.g.:
        ngrok http --url=gopher-gentleman-modular.ngrok-free.dev 8723
  - .env with <ACCOUNT>_IG_USER_ID and <ACCOUNT>_IG_TOKEN.

Example (dry run):
  .venv\\Scripts\\python.exe scripts\\meta_publish_test.py ^
      --account pastmomentsdaily ^
      --video "data\\downloads\\instagram\\some_cropped.mp4" ^
      --ngrok-url https://gopher-gentleman-modular.ngrok-free.dev

Add --publish to the same command to post for real.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import pathlib
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
from nicheflow_studio.core.env import load_dotenv  # noqa: E402

# Instagram Login flow uses graph.instagram.com. If your token was minted via the
# older Facebook-Login flow, pass --host graph.facebook.com instead.
DEFAULT_HOST = "graph.instagram.com"
DEFAULT_VERSION = "v21.0"
POLL_TIMEOUT_S = 180
POLL_INTERVAL_S = 4


def _request(url: str, params: dict, method: str = "GET") -> dict:
    """Minimal Graph API call via urllib (no extra deps). Token is never logged."""
    if method == "POST":
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(params).encode("utf-8"), method="POST"
        )
    else:
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"\nHTTP {exc.code} from {url.split('?')[0]}\n{body}\n")


def _serve_video(video_path: pathlib.Path, port: int) -> tuple[socketserver.TCPServer, str, str]:
    """Serve ONLY the chosen video from a temp dir on 127.0.0.1:<port>."""
    tmp_dir = tempfile.mkdtemp(prefix="nf_meta_test_")
    shutil.copy(video_path, tmp_dir)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp_dir)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, video_path.name, tmp_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", required=True, help="env prefix, e.g. pastmomentsdaily")
    parser.add_argument("--video", required=True, help="local path to a 9:16 mp4 reel")
    parser.add_argument("--ngrok-url", required=True, help="https://<your>.ngrok-free.dev")
    parser.add_argument("--port", type=int, default=8723, help="must match the ngrok forward port")
    parser.add_argument(
        "--caption", default="NicheFlow Graph API test", help="used only on --publish"
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--publish", action="store_true", help="ACTUALLY post it live")
    args = parser.parse_args()

    load_dotenv()
    prefix = args.account.upper()
    user_id = os.environ.get(f"{prefix}_IG_USER_ID")
    token = os.environ.get(f"{prefix}_IG_TOKEN")
    if not user_id or not token:
        raise SystemExit(f"Missing {prefix}_IG_USER_ID or {prefix}_IG_TOKEN in .env")

    video_path = pathlib.Path(args.video)
    if not video_path.is_file():
        raise SystemExit(f"Video not found: {video_path}")

    base = f"https://{args.host}/{args.version}"
    httpd, filename, tmp_dir = _serve_video(video_path, args.port)
    video_url = f"{args.ngrok_url.rstrip('/')}/{urllib.parse.quote(filename)}"
    print(f"Serving {filename} at {video_url}")

    try:
        print("\n[1/3] Creating Reels container...")
        created = _request(
            f"{base}/{user_id}/media",
            {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": args.caption,
                "access_token": token,
            },
            method="POST",
        )
        container_id = created.get("id")
        print(f"  container id: {container_id}")
        if not container_id:
            raise SystemExit(f"  no container id returned: {created}")

        print("\n[2/3] Polling container status (Meta downloads + checks the video)...")
        deadline = time.monotonic() + POLL_TIMEOUT_S
        status = None
        while time.monotonic() < deadline:
            snap = _request(
                f"{base}/{container_id}",
                {"fields": "status_code,status", "access_token": token},
            )
            status = snap.get("status_code")
            print(f"  status: {status} ({snap.get('status', '')})")
            if status in {"FINISHED", "ERROR", "EXPIRED"}:
                break
            time.sleep(POLL_INTERVAL_S)

        if status != "FINISHED":
            raise SystemExit(f"\nContainer did not finish (last status: {status}).")
        print("\n[ok] Container FINISHED - token, user id, public fetch, and format all work.")

        if not args.publish:
            print("\n[3/3] Dry run - skipping media_publish. Nothing was posted.")
            print("    Re-run with --publish to post it live.")
            return 0

        print("\n[3/3] Publishing LIVE...")
        published = _request(
            f"{base}/{user_id}/media_publish",
            {"creation_id": container_id, "access_token": token},
            method="POST",
        )
        print(f"  published media id: {published.get('id')}")
        print("\n[ok] Posted live. Check the account.")
        return 0
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
