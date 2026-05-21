from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nicheflow_studio.db.models import Account, ScrapeCandidate
from nicheflow_studio.db.session import get_session, init_db
from nicheflow_studio.downloader.instagram import instagram_shortcode_from_url
from nicheflow_studio.scraper.instagram import scrape_instagram_urls

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _read_urls(*, urls: list[str], file_path: str | None) -> list[str]:
    collected = [url.strip().strip("\ufeff") for url in urls if url.strip()]
    if file_path:
        path = Path(file_path)
        raw_text = path.read_text(encoding="utf-8").strip().strip("\ufeff")
        if path.suffix.lower() == ".json":
            loaded = json.loads(raw_text or "[]")
            if not isinstance(loaded, list):
                raise ValueError("Instagram URL JSON file must contain a list of URLs.")
            for item in loaded:
                if isinstance(item, str) and item.strip():
                    collected.append(item.strip().strip("\ufeff"))
            return collected

        for line in raw_text.splitlines():
            cleaned = line.strip().strip("\ufeff")
            if cleaned and not cleaned.startswith("#"):
                collected.append(cleaned)
    return collected


def _candidate_key(candidate: object) -> str:
    video_id = getattr(candidate, "video_id", None)
    if isinstance(video_id, str) and video_id:
        return f"instagram:{video_id}"
    return getattr(candidate, "source_url")


def _url_key(url: str) -> str:
    shortcode = instagram_shortcode_from_url(url)
    if shortcode is not None:
        return f"instagram:{shortcode}"
    return url


def _filter_new_urls_for_account(*, urls: list[str], account_name: str) -> list[str]:
    init_db()
    with get_session() as session:
        account = (
            session.query(Account)
            .filter(Account.name == account_name, Account.platform == "instagram")
            .first()
        )
        if account is None:
            return urls

        existing = {
            _candidate_key(candidate)
            for candidate in session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.account_id == account.id)
            .all()
        }

    return [url for url in urls if _url_key(url) not in existing]


def _persist_candidates(*, account_name: str, candidates: list[object]) -> tuple[int, int]:
    init_db()
    saved = 0
    refreshed = 0
    with get_session() as session:
        account = (
            session.query(Account)
            .filter(Account.name == account_name, Account.platform == "instagram")
            .first()
        )
        if account is None:
            account = Account(name=account_name, platform="instagram")
            session.add(account)
            session.flush()

        existing = {
            _candidate_key(candidate): candidate
            for candidate in session.query(ScrapeCandidate)
            .filter(ScrapeCandidate.account_id == account.id)
            .all()
        }
        for candidate in candidates:
            key = _candidate_key(candidate)
            existing_candidate = existing.get(key)
            if existing_candidate is not None:
                existing_candidate.scrape_source_url = candidate.scrape_source_url
                existing_candidate.source_url = candidate.source_url
                existing_candidate.extractor = candidate.extractor
                existing_candidate.video_id = candidate.video_id
                existing_candidate.title = candidate.title
                existing_candidate.channel_name = candidate.channel_name
                existing_candidate.published_at = candidate.published_at
                existing_candidate.description = candidate.description
                existing_candidate.view_count = candidate.view_count
                existing_candidate.like_count = candidate.like_count
                existing_candidate.comment_count = candidate.comment_count
                existing_candidate.duration_seconds = candidate.duration_seconds
                existing_candidate.thumbnail_url = candidate.thumbnail_url
                existing_candidate.discovery_query = candidate.discovery_query
                existing_candidate.match_reason = candidate.match_reason
                existing_candidate.ranking_score = candidate.ranking_score
                refreshed += 1
                continue

            candidate_row = ScrapeCandidate(
                account_id=account.id,
                scrape_source_url=candidate.scrape_source_url,
                source_url=candidate.source_url,
                extractor=candidate.extractor,
                video_id=candidate.video_id,
                title=candidate.title,
                channel_name=candidate.channel_name,
                published_at=candidate.published_at,
                description=candidate.description,
                view_count=candidate.view_count,
                like_count=candidate.like_count,
                comment_count=candidate.comment_count,
                duration_seconds=candidate.duration_seconds,
                thumbnail_url=candidate.thumbnail_url,
                discovery_query=candidate.discovery_query,
                match_reason=candidate.match_reason,
                ranking_score=candidate.ranking_score,
            )
            session.add(candidate_row)
            existing[key] = candidate_row
            saved += 1
        session.commit()
    return (saved, refreshed)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract Instagram post/Reel metadata from one or many URLs."
    )
    parser.add_argument("--url", action="append", default=[], help="Instagram post/Reel URL")
    parser.add_argument("--file", help="Text file with one Instagram URL per line")
    parser.add_argument("--limit", type=int, help="Maximum URLs to process")
    parser.add_argument("--max-age-days", type=int, help="Skip posts older than this many days")
    parser.add_argument(
        "--save-account",
        help="Persist results into this Instagram account name as scrape candidates",
    )
    parser.add_argument(
        "--only-new-for-account",
        help="Before extracting metadata, skip URLs already saved for this Instagram account",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print extracted candidate JSON even when saving to the app database",
    )
    args = parser.parse_args()

    urls = _read_urls(urls=args.url, file_path=args.file)
    if not urls:
        parser.error("Provide at least one --url or --file.")
    if args.only_new_for_account:
        urls = _filter_new_urls_for_account(urls=urls, account_name=args.only_new_for_account)
        if not urls:
            print(f"No new Instagram URLs for account {args.only_new_for_account!r}.")
            print("[]")
            return 0

    candidates = scrape_instagram_urls(
        urls,
        max_items=args.limit,
        max_age_days=args.max_age_days,
    )
    if args.save_account:
        saved, refreshed = _persist_candidates(account_name=args.save_account, candidates=candidates)
        print(
            f"Saved {saved} new candidates and refreshed {refreshed} existing "
            f"candidate(s) in Instagram account {args.save_account!r}."
        )

    if args.print_json or not args.save_account:
        payload = [asdict(candidate) for candidate in candidates]
        print(json.dumps(payload, indent=2 if args.pretty else None, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
