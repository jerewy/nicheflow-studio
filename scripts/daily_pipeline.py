"""Run the daily pool-to-publish chain from the command line.

A thin adapter over the same services the desktop app's buttons call; it holds
no business rules of its own. Three steps, so a person (or a model) can write
the titles in between and a person can review them before anything renders:

    prepare  distribute the niche pool, download the new clips, build the
             contact sheets, and write one batch prompt per account into a
             run folder (the same prompt the Batch Drafts screen copies)
    import   import each account's reply file from that run folder
    finish   apply the recommended option, render, and auto-schedule every
             imported reel, then wait for the cloud uploads to land

Examples (PowerShell):

    .venv\\Scripts\\python.exe scripts\\daily_pipeline.py prepare
    .venv\\Scripts\\python.exe scripts\\daily_pipeline.py import --run <run folder>
    .venv\\Scripts\\python.exe scripts\\daily_pipeline.py finish --run <run folder>

Replies go next to each prompt as ``<handle>.reply.txt`` and must use the plain
paste format (plain-text "Title Option N:" headers, no markdown).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import threading

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nicheflow_studio.core.env import load_dotenv  # noqa: E402
from nicheflow_studio.core.paths import data_dir  # noqa: E402

MANIFEST = "manifest.json"
# Upload threads from finish_batch are daemons, so the process must wait for
# them or exiting would abandon uploads half way (the app's sync sweep would
# retry them, but only while the app is running).
_HANDOFF_THREAD_PREFIX = "nicheflow-cloud-handoff-"
_HANDOFF_WAIT_SECONDS = 1800


def _download_pending(niche: str) -> list[dict]:
    """Fetch footage for every pending-review clip in the niche that has none yet.

    Distribution only starts these downloads on a background thread, which a
    short-lived script would kill; doing them here, synchronously, is what makes
    the clips draftable in the same run.
    """
    from nicheflow_studio.db.models import Account, DownloadItem
    from nicheflow_studio.db.session import get_session
    from nicheflow_studio.services import library

    with get_session() as session:
        ids = [
            item_id
            for (item_id,) in session.query(DownloadItem.id)
            .join(Account, Account.id == DownloadItem.account_id)
            .filter(
                Account.niche == niche,
                DownloadItem.review_state == "pending_review",
                DownloadItem.file_path.is_(None),
            )
            .all()
        ]
    failures = []
    for index, item_id in enumerate(ids, start=1):
        print(f"  download {index}/{len(ids)} (item {item_id})", flush=True)
        try:
            library.ensure_item_downloaded(item_id)
        except Exception as exc:  # noqa: BLE001 - one gone clip must not stop the run
            failures.append({"item_id": item_id, "error": str(exc)[:200]})
    return failures


def cmd_prepare(args: argparse.Namespace) -> int:
    from nicheflow_studio.db.models import Account
    from nicheflow_studio.db.session import get_session
    from nicheflow_studio.services import draft_handoff, pooling

    distributed = pooling.distribute_niche(args.niche)
    print(f"Distributed {distributed['assigned']} clip(s): "
          + ", ".join(f"{a['account_name']} +{a['count']}" for a in distributed["accounts"])
          + (f" ({distributed['reason']})" if distributed.get("reason") else ""))
    failures = _download_pending(args.niche)
    for failure in failures:
        print(f"  download failed for item {failure['item_id']}: {failure['error']}")

    run_dir = data_dir() / "pipeline-runs" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"niche": args.niche, "accounts": []}
    groups = draft_handoff.batch_candidates(niche=args.niche, per_account=args.per_account)
    for group in groups:
        item_ids = [row["id"] for row in group["items"] if not row["has_draft"]]
        if not item_ids:
            continue
        with get_session() as session:
            handle = session.get(Account, group["account_id"]).instagram_handle or str(
                group["account_id"]
            )
        frames = draft_handoff.batch_frames(item_ids)
        prompt = draft_handoff.build_account_batch_chat_prompt(group["account_id"], item_ids, {})
        prompt_path = run_dir / f"{handle}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        manifest["accounts"].append({
            "account_id": group["account_id"],
            "handle": handle,
            "item_ids": item_ids,
            "prompt": str(prompt_path),
            "reply": str(run_dir / f"{handle}.reply.txt"),
            "frames": frames["frames"],
        })
        print(f"{handle}: {len(item_ids)} reel(s), {len(frames['frames'])} contact sheet(s)")
    (run_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Run folder: {run_dir}")
    return 0


def _load_manifest(run: str) -> tuple[pathlib.Path, dict]:
    run_dir = pathlib.Path(run)
    return run_dir, json.loads((run_dir / MANIFEST).read_text(encoding="utf-8"))


def cmd_import(args: argparse.Namespace) -> int:
    from nicheflow_studio.services import draft_handoff, draft_revisions

    run_dir, manifest = _load_manifest(args.run)
    for entry in manifest["accounts"]:
        reply = pathlib.Path(entry["reply"])
        if not reply.exists():
            print(f"{entry['handle']}: no reply file, skipped")
            continue
        result = draft_handoff.import_account_batch_draft(
            reply.read_text(encoding="utf-8"), entry["item_ids"]
        )
        entry["imported"] = result["imported"]
        print(f"{entry['handle']}: imported {len(result['imported'])}, "
              f"failed {len(result['failed'])}, unmatched {len(result['unmatched'])}")
        for failure in result["failed"]:
            print(f"  item {failure['item_id']}: {failure['error']}")
        for item_id in result["imported"]:
            revision = draft_revisions.latest_revision(item_id)
            pick = revision.recommended_title_index or 1
            print(f"  item {item_id} -> [{pick}] {revision.title_options[pick - 1]}")
    (run_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    from nicheflow_studio.services import batch_finish

    _, manifest = _load_manifest(args.run)
    item_ids = [i for entry in manifest["accounts"] for i in entry.get("imported", [])]
    if not item_ids:
        print("Nothing imported in this run; run `import` first.")
        return 1
    result = batch_finish.finish_batch(
        item_ids, progress=lambda _f, message: message and print(f"  {message}", flush=True)
    )
    for thread in threading.enumerate():
        if thread.name.startswith(_HANDOFF_THREAD_PREFIX):
            print(f"  waiting for upload {thread.name}...", flush=True)
            thread.join(timeout=_HANDOFF_WAIT_SECONDS)
    print(json.dumps({k: result.get(k) for k in ("applied", "exported", "scheduled", "failed",
                                                 "skipped")}, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_prepare = sub.add_parser("prepare", help="Distribute, download, and write batch prompts.")
    p_prepare.add_argument("--niche", default="history")
    p_prepare.add_argument("--per-account", type=int, default=6)
    p_import = sub.add_parser("import", help="Import reply files from a run folder.")
    p_import.add_argument("--run", required=True)
    p_finish = sub.add_parser("finish", help="Apply, render, and schedule a run's reels.")
    p_finish.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    return {"prepare": cmd_prepare, "import": cmd_import, "finish": cmd_finish}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
