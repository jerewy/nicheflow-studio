# Instagram-First Pivot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pivot NicheFlow Studio from a YouTube-first uploader into an Instagram-first gaming Reels operations tool for `RespawnReels`.

**Architecture:** Keep the current local-first pipeline and database foundation. Rename the user-facing upload surface into a platform-neutral publish queue, add an Instagram manual publishing MVP before Meta API automation, and track performance/monetization readiness so sponsorship work is grounded in real account metrics.

**Tech Stack:** Python 3.11+, PyQt6, SQLite/SQLAlchemy, local MP4 processing, Instagram manual publishing first, Meta/Instagram API later.

---

## Strategic Decisions

- Primary platform: Instagram Reels first.
- Brand/account: `RespawnReels`, handle `@RespawnReelsDaily`.
- Niche: gaming entertainment, focused on funny gaming moments, clutch plays, fails, POV gaming memes, and respawn-worthy clips.
- Avoid broad random memes as the first niche because they are harder to sponsor and less brand-safe.
- Start with manual Instagram publishing before API automation.
- Keep YouTube support as a source intake/download pipeline, not the current publishing target.
- Add Instagram source intake carefully as metadata-first intake. Do not build logged-in browser scraping or bot-style automated collection for Instagram.
- Treat official Meta/Instagram Graph API support as a later integration that requires a Meta developer app, an Instagram Professional account, and permissions/app review.

## MVP Direction After Instagram Pivot

The MVP should work as a daily-use Instagram Reels preparation tool:

1. Select an account/niche such as `RespawnReels`.
2. Collect candidate clip ideas from supported sources.
3. Download/process only the clips selected for reuse.
4. Export Instagram-ready vertical Reels with a black-canvas/title template.
5. Copy caption metadata and manually publish to Instagram.
6. Mark posts as posted and record basic performance metrics manually.

The MVP should not require Instagram API approval, browser automation, or automatic Instagram uploads to be useful.

## Source Intake Policy

Supported for MVP:

- YouTube channel/profile source intake through the existing `yt-dlp`-backed YouTube scraper.
- YouTube/Shorts direct URL intake.
- Instagram manual source intake:
  - pasted Instagram Reel URLs
  - pasted Instagram profile URLs
  - pasted Instagram hashtag/source notes
  - user-entered caption/title/notes when the API cannot provide metadata

Deferred until after the manual workflow is stable:

- Official Instagram Graph API hashtag/business-discovery intake.
- Official Instagram publishing through Meta APIs.
- Any automatic media downloading from Instagram.
- Any logged-in Instagram browser scraping.

Explicitly out of scope for MVP:

- stealth scraping
- bypassing platform limits
- automated reposting of Instagram content without rights
- storing Instagram usernames/passwords

## MVP Milestone Order

### Milestone A: Clean Instagram-First Publish Queue

Goal: The Uploads page should behave as a manual Instagram publishing queue, not a YouTube uploader.

- Rename remaining user-facing upload wording to publish/post wording where appropriate.
- Keep `Copy Caption`, `Open Reel`, and `Mark Posted` as first-class actions.
- Remove or disable the YouTube `Publish Selected` automation path until the platform target is explicit.
- Replace YouTube-only job fields in future schema work with platform-neutral fields such as `remote_post_id` and `posted_url`.
- Keep status values simple: `draft`, `scheduled`, `posted`, `failed`.

Definition of done:

- A processed Reel appears in the Publish Queue, the caption can be copied, the output can be opened, and the user can mark it posted without any external API setup.

### Milestone B: Instagram Manual Source Intake

Goal: The app can track Instagram sources and candidate ideas without risky scraping.

- Add `instagram_reel`, `instagram_profile`, and `instagram_hashtag` source types.
- Allow pasted Instagram URLs/hashtags in the Sources UI when the source platform is Instagram.
- Save Instagram candidate rows with platform-neutral metadata:
  - platform
  - source type
  - permalink/source URL
  - title/caption/notes when available
  - thumbnail URL only when legally/API provided
  - state
- Let users manually add a candidate from an Instagram URL or hashtag note.
- Do not auto-download Instagram media in this milestone.

Definition of done:

- A user can add Instagram sources/candidate links, review them alongside YouTube candidates, and use them as planning references for Reels work.

### Milestone C: Processing Quality For Reels

Goal: Exported videos should be ready to post as Instagram Reels.

- Keep the black background/no-blur vertical template as the default.
- Keep title rendering clean and remove old-title leftovers where possible.
- Validate output aspect ratio and file existence before adding to Publish Queue.
- Keep captions as editable metadata, not baked into video.
- Add an Instagram-ready validation label for processed outputs.

Definition of done:

- A user can export a vertical MP4, add it to the Publish Queue, copy its caption, and post it manually without additional editing.

### Milestone D: Manual Performance Tracking

Goal: The user can learn which Reel types work before building automation.

- Keep manual fields for views, likes, comments, shares, posted URL, posted time, and content type.
- Add a compact way to edit these values after posting.
- Add simple table/filter support for posted versus draft jobs.
- Avoid analytics dashboards until there is real posted data.

Definition of done:

- After posting, the user can enter the Instagram post URL and basic metrics so future sourcing decisions are grounded in results.

### Milestone E: Official API Integrations Later

Goal: Add automation only after the manual MVP proves useful.

- Meta/Instagram Graph API intake for approved hashtag/business discovery use cases.
- Instagram publishing through official Meta APIs for eligible Professional accounts.
- YouTube publishing only if YouTube becomes a target platform again.

Definition of done:

- API work is isolated behind platform-specific modules and does not block the manual MVP.

## Monetization Sources To Track Later

- Instagram Creator Marketplace
- TikTok Creator Marketplace
- Collabstr
- Passionfroot
- Shopify Collabs
- impact.com
- PartnerStack
- game affiliate programs
- direct outreach to indie game developers
- direct outreach to gaming gear brands
- mobile game publishers

## Files To Touch

- Modify: `src/nicheflow_studio/app/main_window.py`
  - Rename Schedule copy to Publish Queue copy.
  - Add manual Instagram posting controls.
  - Add performance tracking controls.
- Modify: `src/nicheflow_studio/db/models.py`
  - Add platform-neutral publish fields to `UploadJob` or introduce `PublishJob` only if the rename is worth the migration cost.
- Modify: `src/nicheflow_studio/db/session.py`
  - Add lightweight compatibility migration columns.
- Modify: `tests/test_main_window.py`
  - Cover UI labels, manual-post actions, copied caption behavior, posted state, and performance fields.
- Optional create: `src/nicheflow_studio/publishing/instagram_manual.py`
  - Keep caption formatting and manual-post helper behavior outside the UI if it grows.
- Optional create: `tests/test_instagram_manual_publishing.py`
  - Unit-test caption/hashtag formatting if extracted.

---

### Task 1: Reframe Schedule As Publish Queue

**Files:**
- Modify: `src/nicheflow_studio/app/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write failing UI label test**

Add a test near the existing Schedule tests:

```python
def test_publish_queue_uses_instagram_first_copy(qt_app) -> None:
    init_db()
    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        assert window._module_buttons["uploads"].toolTip() == "Publish"
        assert window._schedule_title_label.text() == "Publish Queue"
        assert "Instagram-ready Reels" in window._schedule_message_label.text()
        assert window._schedule_upload_selected_button.text() == "Publish Selected"
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_window.py::test_publish_queue_uses_instagram_first_copy -q
```

Expected: FAIL because current copy still says Schedule / Upload.

- [ ] **Step 3: Implement minimal label changes**

Change:

```python
("uploads", "Schedule", "check")
```

to:

```python
("uploads", "Publish", "check")
```

In `_make_schedule_page`, change the title/message/button text to:

```python
title_label = QLabel("Publish Queue")
message_label = QLabel(
    "Review Instagram-ready Reels, copy captions, and track manual publishing."
)
self._schedule_upload_selected_button = QPushButton("Publish Selected")
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_window.py::test_publish_queue_uses_instagram_first_copy -q
```

Expected: PASS.

---

### Task 2: Add Manual Instagram Posting Actions

**Files:**
- Modify: `src/nicheflow_studio/app/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write failing test for manual post action controls**

Add:

```python
def test_publish_queue_has_manual_instagram_actions(qt_app, tmp_path: Path) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description="That ending was wild #gaming #reels",
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        qt_app.processEvents()

        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        assert window._schedule_copy_caption_button.isEnabled() is True
        assert window._schedule_open_output_button.isEnabled() is True
        assert window._schedule_mark_posted_button.isEnabled() is True
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_window.py::test_publish_queue_has_manual_instagram_actions -q
```

Expected: FAIL because the buttons do not exist.

- [ ] **Step 3: Add buttons and selection state**

In `_make_schedule_page`, add buttons:

```python
self._schedule_copy_caption_button = QPushButton("Copy Caption")
self._schedule_open_output_button = QPushButton("Open Reel")
self._schedule_mark_posted_button = QPushButton("Mark Posted")
for button in (
    self._schedule_copy_caption_button,
    self._schedule_open_output_button,
    self._schedule_mark_posted_button,
):
    button.setObjectName("downloadToolbarButton")
    button.setEnabled(False)
```

Add them to the action row before the stretch.

Update `_on_schedule_selection_changed`:

```python
has_selection = self._selected_schedule_job_id() is not None
self._schedule_upload_selected_button.setEnabled(has_selection)
self._schedule_copy_caption_button.setEnabled(has_selection)
self._schedule_open_output_button.setEnabled(has_selection)
self._schedule_mark_posted_button.setEnabled(has_selection)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_window.py::test_publish_queue_has_manual_instagram_actions -q
```

Expected: PASS.

---

### Task 3: Implement Copy Caption And Open Reel

**Files:**
- Modify: `src/nicheflow_studio/app/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Write failing tests**

Add:

```python
def test_copy_caption_copies_selected_job_description(qt_app, tmp_path: Path) -> None:
    init_db()
    output_path = tmp_path / "reel.mp4"
    output_path.write_bytes(b"processed")
    caption = "That ending was wild #gaming #reels"

    with get_session() as session:
        account = Account(name="RespawnReels", platform="instagram")
        session.add(account)
        session.flush()
        session.add(
            UploadJob(
                account_id=account.id,
                processed_path=str(output_path),
                title="This respawn was personal",
                description=caption,
                privacy_status="public",
                status="draft",
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        window._set_current_page("uploads")
        window._schedule_table.selectRow(0)
        qt_app.processEvents()

        window._schedule_copy_caption_button.click()
        qt_app.processEvents()

        assert qt_app.clipboard().text() == caption
        assert window._toast_label.text() == "Copied caption."
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_window.py::test_copy_caption_copies_selected_job_description -q
```

Expected: FAIL because click handler is missing.

- [ ] **Step 3: Implement copy caption**

Connect:

```python
self._schedule_copy_caption_button.clicked.connect(self._on_copy_schedule_caption_clicked)
```

Add helper:

```python
def _selected_schedule_job(self) -> UploadJob | None:
    job_id = self._selected_schedule_job_id()
    if job_id is None:
        return None
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is None:
            return None
        session.expunge(job)
        return job
```

Add action:

```python
def _on_copy_schedule_caption_clicked(self) -> None:
    job = self._selected_schedule_job()
    if job is None:
        self._notify("Select a publish job first.", Tone.WARNING)
        return
    caption = job.description or job.title or ""
    QApplication.clipboard().setText(caption)
    self._notify("Copied caption.", Tone.SUCCESS)
```

Also import `QApplication` from `PyQt6.QtWidgets`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_main_window.py::test_copy_caption_copies_selected_job_description -q
```

Expected: PASS.

---

### Task 4: Add Manual Posted State And URL

**Files:**
- Modify: `src/nicheflow_studio/db/models.py`
- Modify: `src/nicheflow_studio/db/session.py`
- Modify: `src/nicheflow_studio/app/main_window.py`
- Test: `tests/test_main_window.py`, `tests/test_paths_and_db.py`

- [ ] **Step 1: Write failing database compatibility test**

Add to `tests/test_paths_and_db.py`:

```python
def test_init_db_adds_manual_publish_columns() -> None:
    init_db()
    db_path = data_dir() / "nicheflow.db"

    with sqlite3.connect(db_path) as connection:
        upload_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(upload_jobs)").fetchall()
        }

    assert "posted_url" in upload_columns
    assert "posted_at" in upload_columns
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_paths_and_db.py::test_init_db_adds_manual_publish_columns -q
```

Expected: FAIL because columns do not exist.

- [ ] **Step 3: Add model and migration fields**

In `UploadJob`, add:

```python
posted_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

In `_ensure_compatibility`, after upload table creation compatibility, add:

```python
upload_columns = {column["name"] for column in inspect(connection).get_columns("upload_jobs")}
if "posted_url" not in upload_columns:
    connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_url VARCHAR(2048)"))
if "posted_at" not in upload_columns:
    connection.execute(text("ALTER TABLE upload_jobs ADD COLUMN posted_at DATETIME"))
```

- [ ] **Step 4: Add Mark Posted behavior**

Button handler:

```python
def _on_mark_schedule_posted_clicked(self) -> None:
    job_id = self._selected_schedule_job_id()
    if job_id is None:
        self._notify("Select a publish job first.", Tone.WARNING)
        return
    with get_session() as session:
        job = session.get(UploadJob, job_id)
        if job is None:
            self._notify("The selected publish job no longer exists.", Tone.WARNING)
            return
        job.status = "uploaded"
        job.posted_at = dt.datetime.now(dt.timezone.utc)
        job.error_message = None
        session.commit()
    self._refresh_schedule_page()
    self._notify("Marked as posted.", Tone.SUCCESS)
```

Connect:

```python
self._schedule_mark_posted_button.clicked.connect(self._on_mark_schedule_posted_clicked)
```

- [ ] **Step 5: Run targeted tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_paths_and_db.py::test_init_db_adds_manual_publish_columns tests/test_main_window.py::test_schedule_page_upload_selected_marks_job_uploaded -q
```

Expected: PASS.

---

### Task 5: Add Performance Tracking Fields

**Files:**
- Modify: `src/nicheflow_studio/db/models.py`
- Modify: `src/nicheflow_studio/db/session.py`
- Modify: `src/nicheflow_studio/app/main_window.py`
- Test: `tests/test_paths_and_db.py`, `tests/test_main_window.py`

- [ ] **Step 1: Write failing DB test**

Add:

```python
def test_init_db_adds_publish_metric_columns() -> None:
    init_db()
    db_path = data_dir() / "nicheflow.db"

    with sqlite3.connect(db_path) as connection:
        upload_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(upload_jobs)").fetchall()
        }

    assert "posted_views" in upload_columns
    assert "posted_likes" in upload_columns
    assert "posted_comments" in upload_columns
    assert "posted_shares" in upload_columns
    assert "content_type" in upload_columns
```

- [ ] **Step 2: Add columns**

In `UploadJob`, add integer fields:

```python
posted_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
posted_likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
posted_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
posted_shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

In `_ensure_compatibility`, add `ALTER TABLE` statements for each column.

- [ ] **Step 3: Keep UI minimal**

Do not build a full analytics dashboard yet. Add columns to the publish table only if they do not crowd the UI. Prefer later editable detail panel.

- [ ] **Step 4: Run DB test**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_paths_and_db.py::test_init_db_adds_publish_metric_columns -q
```

Expected: PASS.

---

## Next Phase After This Plan

After manual Instagram publishing works inside the app:

1. Post 30 Reels manually using `RespawnReels`.
2. Track views/likes/comments/shares.
3. Identify winning content types.
4. Only then add Meta/Instagram API publishing.
5. Only after consistent traction, start sponsor/campaign outreach using the saved source list.

## Self-Review

- Spec coverage: plan covers Instagram pivot, sponsor-source note, manual publishing MVP, tracking, and later API/sponsor sequence.
- Placeholder scan: no implementation step uses vague placeholders for code-changing tasks.
- Type consistency: existing `UploadJob` is intentionally reused for minimal migration cost; later rename to `PublishJob` can be a separate refactor.
