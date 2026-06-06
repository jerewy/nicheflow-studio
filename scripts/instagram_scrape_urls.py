from __future__ import annotations

import argparse
import datetime as dt
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
from nicheflow_studio.db.sources import advance_source_newest_date
from nicheflow_studio.downloader.instagram import instagram_shortcode_from_url
from nicheflow_studio.core.instagram_session import (
    DEFAULT_PROFILE_NAME,
    instagram_yt_dlp_cookie_status,
)
from nicheflow_studio.core.instagram_profile_pool import (
    AUTO_PROFILE,
    NoAvailableProfilesError,
    ProfilePool,
)
from nicheflow_studio.core.env import load_dotenv
from nicheflow_studio.scraper.instagram_apify import scrape_instagram_urls_apify
from nicheflow_studio.scraper.instagram import (
    InstagramRateLimitError,
    InstagramScrapeStats,
    scrape_instagram_urls_instaloader,
    scrape_instagram_urls_with_stats,
)

DEFAULT_RETRY_QUEUE_PATH = Path("data") / "discovered" / "retry_queue.json"
DEFAULT_PER_PROFILE_BUDGET = 35

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

        # Advance this source's newest-content cursor to the newest post date we
        # now hold, so a later incremental scrape (onlyPostsNewerThan) resumes
        # from real content rather than wall-clock time. The source URL is the
        # account's profile page; manual single-URL imports and bulk scrapes for
        # the same handle therefore share one cursor.
        session.flush()
        source = advance_source_newest_date(
            session,
            account=account,
            source_url=f"https://www.instagram.com/{account_name}/",
            label=account_name,
        )
        newest_at = source.last_scraped_at
        newest_shortcode = source.last_seen_external_id
        session.commit()

    newest_label = newest_at.date().isoformat() if newest_at is not None else "none"
    print(
        f"source cursor for {account_name!r}: newest post {newest_label} "
        f"(shortcode {newest_shortcode or '-'})"
    )
    return (saved, refreshed)


def _print_cookie_status() -> None:
    status = instagram_yt_dlp_cookie_status()
    if status.cookiefile is None:
        print("yt-dlp cookiefile: not found")
        print("yt-dlp sessionid: no")
        return
    print(f"yt-dlp cookiefile: found {status.cookiefile}")
    print(f"yt-dlp sessionid: {'yes' if status.has_sessionid else 'no'}")


def _print_funnel(
    *,
    initial_count: int,
    filtered_count: int,
    stats: InstagramScrapeStats,
    saved: int | None = None,
    refreshed: int | None = None,
    rate_limit_hit: bool = False,
) -> None:
    print("Metadata funnel:")
    print(f"- input URLs: {initial_count}")
    print(f"- new to account after duplicate filter: {filtered_count}")
    print(f"- extraction limit this run: {stats.extraction_limit}")
    print(f"- attempted: {stats.attempted}")
    print(f"- extracted candidates: {stats.extracted}")
    print(f"- skipped old/missing date: {stats.skipped_old}")
    print(f"- failed no video: {stats.failed_no_video}")
    print(f"- failed unavailable/private: {stats.failed_unavailable}")
    print(f"- failed other: {stats.failed_other}")
    print(f"- stopped by rate limit: {'yes' if rate_limit_hit else 'no'}")
    if saved is not None and refreshed is not None:
        print(f"- saved: {saved}")
        print(f"- refreshed: {refreshed}")


def _resolve_profiles(cookie_profile: str) -> list[str]:
    """Resolve the CLI flag to a concrete ordered list of profiles to rotate through."""
    if cookie_profile != AUTO_PROFILE:
        return [cookie_profile]
    pool = ProfilePool.load()
    try:
        first = pool.pick()
    except NoAvailableProfilesError as exc:
        raise SystemExit(f"error: {exc}") from exc
    # Build a rotation order starting from the LRU pick, then the rest by LRU as well.
    available = pool.available()
    available.sort(
        key=lambda p: (
            p.name != first.name,
            p.last_used or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        )
    )
    return [p.name for p in available]


def _extract_instagram_metadata(
    urls: list[str],
    *,
    extraction_limit: int,
    max_age_days: int | None,
    metadata_extractor: str = "apify",
    cookie_profile: str = DEFAULT_PROFILE_NAME,
    per_profile_budget: int | None = DEFAULT_PER_PROFILE_BUDGET,
    retry_queue_path: Path | None = DEFAULT_RETRY_QUEUE_PATH,
) -> tuple[list[object], InstagramScrapeStats, str, bool]:
    if metadata_extractor == "apify":
        url_batch = urls[:extraction_limit]
        candidates = scrape_instagram_urls_apify(url_batch, results_limit=extraction_limit)
        stats = InstagramScrapeStats(
            input_urls=len(urls),
            extraction_limit=extraction_limit,
            attempted=len(url_batch),
        )
        if max_age_days is not None:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
            filtered_candidates = []
            for candidate in candidates:
                if candidate.published_at is None or candidate.published_at < cutoff:
                    stats.skipped_old += 1
                    continue
                filtered_candidates.append(candidate)
            candidates = filtered_candidates
        stats.extracted = len(candidates)
        print("metadata extractor: apify")
        return candidates, stats, "apify", False

    if metadata_extractor == "yt-dlp":
        candidates, stats = scrape_instagram_urls_with_stats(
            urls,
            max_items=extraction_limit,
            max_age_days=max_age_days,
        )
        print("metadata extractor: yt-dlp")
        return candidates, stats, "yt-dlp", False

    profile_names = _resolve_profiles(cookie_profile)
    pool = ProfilePool.load() if cookie_profile == AUTO_PROFILE else None
    print(f"scraper: profile rotation = {profile_names}")
    try:
        candidates, stats, session_used = scrape_instagram_urls_instaloader(
            urls,
            max_items=extraction_limit,
            max_age_days=max_age_days,
            profile_names=profile_names,
            pool=pool,
            per_profile_budget=per_profile_budget,
            retry_queue_path=retry_queue_path,
        )
        print("metadata extractor: instaloader")
        print(f"instaloader Playwright sessionid injected: {'yes' if session_used else 'no'}")
        return candidates, stats, "instaloader", False
    except InstagramRateLimitError:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"metadata extractor: instaloader failed ({exc}); falling back to yt-dlp")

    candidates, stats = scrape_instagram_urls_with_stats(
        urls,
        max_items=extraction_limit,
        max_age_days=max_age_days,
    )
    print("metadata extractor: yt-dlp fallback")
    return candidates, stats, "yt-dlp", True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract Instagram Reel metadata from one or many URLs."
    )
    parser.add_argument("--url", action="append", default=[], help="Instagram Reel URL")
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
    parser.add_argument(
        "--cookie-profile",
        default=DEFAULT_PROFILE_NAME,
        help=(
            f"Named login profile (default: {DEFAULT_PROFILE_NAME}). "
            f"Use '{AUTO_PROFILE}' to rotate across all logged-in profiles."
        ),
    )
    parser.add_argument(
        "--per-profile-budget",
        type=int,
        default=DEFAULT_PER_PROFILE_BUDGET,
        help=(
            f"Soft cap of successful extractions per profile before rotating "
            f"(default: {DEFAULT_PER_PROFILE_BUDGET}). Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--retry-queue",
        type=Path,
        default=DEFAULT_RETRY_QUEUE_PATH,
        help=f"Where to write URLs that failed for later retry (default: {DEFAULT_RETRY_QUEUE_PATH})",
    )
    parser.add_argument(
        "--metadata-extractor",
        choices=("instaloader", "apify", "yt-dlp"),
        default="apify",
        help=(
            "Metadata backend to use. Default uses Apify to avoid Instagram account automation; "
            "choose 'instaloader' only for explicit legacy debugging."
        ),
    )
    args = parser.parse_args()
    load_dotenv()

    urls = _read_urls(urls=args.url, file_path=args.file)
    if not urls:
        parser.error("Provide at least one --url or --file.")
    initial_count = len(urls)
    if args.only_new_for_account:
        urls = _filter_new_urls_for_account(urls=urls, account_name=args.only_new_for_account)
        if not urls:
            print(f"No new Instagram URLs for account {args.only_new_for_account!r}.")
            print("[]")
            return 0
    filtered_count = len(urls)
    extraction_limit = min(args.limit or filtered_count, filtered_count)
    print(f"Metadata input URLs: {initial_count}")
    print(f"New to account after duplicate filter: {filtered_count}")
    print(f"Extraction limit this run: {extraction_limit}")
    _print_cookie_status()

    rate_limit_hit = False
    stats = InstagramScrapeStats(input_urls=filtered_count, extraction_limit=extraction_limit)
    extractor_name = "apify"
    used_fallback = False
    per_profile_budget = args.per_profile_budget if args.per_profile_budget > 0 else None
    try:
        candidates, stats, extractor_name, used_fallback = _extract_instagram_metadata(
            urls,
            extraction_limit=extraction_limit,
            max_age_days=args.max_age_days,
            metadata_extractor=args.metadata_extractor,
            cookie_profile=args.cookie_profile,
            per_profile_budget=per_profile_budget,
            retry_queue_path=args.retry_queue,
        )
    except InstagramRateLimitError as exc:
        print(f"WARNING: {exc}")
        candidates = exc.partial_candidates
        stats = exc.stats
        rate_limit_hit = True

    if rate_limit_hit and not candidates:
        _print_funnel(
            initial_count=initial_count,
            filtered_count=filtered_count,
            stats=stats,
            rate_limit_hit=True,
        )
        print("No candidates collected before rate limit. Nothing to save.")
        return 0

    saved: int | None = None
    refreshed: int | None = None
    if args.save_account:
        saved, refreshed = _persist_candidates(account_name=args.save_account, candidates=candidates)
        print(
            f"Saved {saved} new candidates and refreshed {refreshed} existing "
            f"candidate(s) in Instagram account {args.save_account!r}."
        )
    _print_funnel(
        initial_count=initial_count,
        filtered_count=filtered_count,
        stats=stats,
        saved=saved,
        refreshed=refreshed,
        rate_limit_hit=rate_limit_hit,
    )
    print(f"metadata extractor used: {extractor_name}")
    print(f"metadata fallback used: {'yes' if used_fallback else 'no'}")

    if args.print_json or not args.save_account:
        payload = [asdict(candidate) for candidate in candidates]
        print(json.dumps(payload, indent=2 if args.pretty else None, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
