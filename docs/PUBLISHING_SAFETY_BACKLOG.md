# Publishing Safety & Automation Backlog

Findings from the 2026-06-10 publishing-safety review (multi-account scale-up).
Planned, not yet executed. Ordered by priority.

## P0 — fix before scaling to 10 accounts

1. **Failed jobs retry forever (retry hammer).**
   `publish_now._post_and_record` records `error_message` on failure but leaves
   status `"scheduled"` with a past `scheduled_at`, so the auto-publish loop
   re-attempts every pass — including on **checkpointed** accounts, where
   repeated attempts escalate flags into bans.
   Fix: on failure set status `failed` (one retry max), and on checkpoint mark
   the **account** needs-attention so nothing else posts on it until re-login.

2. **No gap between consecutive posts in a batch.**
   `publish_due_jobs` posts up to 3 jobs back-to-back; different accounts can
   post from the same IP within ~3 minutes.
   Fix: randomized inter-post delay (2–6 min) inside the batch loop.

## P1

3. **Move the auto-publish loop into the Python backend** (background thread).
   Today it lives in the React Processing screen and only runs while that
   screen is mounted with the toggle on. Prerequisite for any remote/always-on
   posting setup.

4. **Cross-account slot stagger.** Slot scheduling is per-account; ten accounts
   with "09:00, 18:00" all become due simultaneously.
   Interim: set different slot times per account by hand (works today).
   Later: scheduler avoids assigning two accounts slots within ~15 min.

## P2

5. **Single-instance guard.** The publish lock is in-process; two app instances
   could drive two browsers concurrently. File-lock or single-instance check.

6. **Fingerprint/IP correlation — defer proxies.** All accounts share one
   machine/IP/viewport. Behavioral mitigations above are the right spend;
   revisit per-profile proxies only if correlated checkpoints appear.

7. **Distribution default 4 posts/day is aggressive for young accounts.**
   Consider default 2–3 (`DEFAULT_DAILY_POSTS_PER_ACCOUNT` in
   `core/distribution.py`); let aged accounts earn 4.

## P3

8. **Legacy cookie-based scraping path** (instaloader/yt-dlp with session
   cookies in `scraper/instagram.py`) still exists. Sourcing is Apify-only now;
   verify no UI flow reaches the legacy path, then delete it.

9. **Cookies unencrypted under `data/browser-profiles/`.** Acceptable for a
   personal desktop MVP; do not sync that folder to cloud storage.

## Already solid (no action)

- Manual login, cookie-only sessions, no stored credentials.
- Headed real Chrome, no fake stealth flags; checkpoint detection pre/post share.
- Slot scheduling with randomized jitter; serialized posting (module lock).
- Apify-only sourcing with monthly usage caps; anonymous downloads.
- Global pool dedup → one asset never posts to two accounts.
