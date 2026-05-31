from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import zipfile
import av
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import QDate, QDateTime, QObject, QSize, QThread, QTime, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QApplication,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import joinedload

from nicheflow_studio.core.paths import (
    backups_dir,
    data_dir,
    downloads_dir,
    logs_dir,
    processed_dir,
)
from nicheflow_studio.db.assignments import (
    assignment_counts_by_account,
    distribute_niche,
)
from nicheflow_studio.db.media_library import (
    find_media_asset,
    find_or_register_media_asset,
    mark_media_asset_downloaded,
)
from nicheflow_studio.db.models import (
    Account,
    DownloadItem,
    PoolItem,
    ScrapeCandidate,
    ScrapeRun,
    Source,
    UploadJob,
)
from nicheflow_studio.db.pools import accept_into_pool, pool_size
from nicheflow_studio.db.session import get_session, init_db, reset_db_state
from nicheflow_studio.processing.video import (
    CropSuggestion,
    CropSettings,
    PreprocessingOcrDiagnostics,
    VideoProbe,
    diagnose_preprocessing_ocr,
    suggest_crop_settings,
    export_cropped_video,
    output_dimensions,
    probe_video,
    processed_output_path,
    suggest_title_replacement_crop,
)
from nicheflow_studio.processing.watermark import replace_detected_watermark
from nicheflow_studio.core.account_health import (
    HealthState,
    SessionHealth,
    live_health,
    local_health,
)
from nicheflow_studio.core.instagram_profile_pool import ProfilePool
from nicheflow_studio.core.publishing_dashboard import (
    AccountDashboardRow,
    PublishJobView,
    build_dashboard_row,
    summarize_dashboard,
)
from nicheflow_studio.core.scheduling import upcoming_slot_times
from nicheflow_studio.publisher.instagram_publisher import publish_reel
from nicheflow_studio.publisher.instagram_web import (
    launch_instagram_login,
    launch_instagram_upload_assist,
)
from nicheflow_studio.processing.transcription import generate_transcript_draft_in_subprocess
from nicheflow_studio.processing.smart_drafts import (
    SMART_DRAFT_OPTION_COUNT,
    SmartDrafts,
    _caption_hashtag_target,
    _caption_paragraph_rule,
    _caption_style_line,
    _caption_word_target,
    _groq_limit_profile,
    _profile_style_block,
    can_generate_smart_drafts,
    effective_title_rules,
    generate_smart_drafts,
)
from nicheflow_studio.queue import QueueManager
from nicheflow_studio.scraper.youtube import (
    DiscoveryWeights,
    ScrapedVideoCandidate,
    infer_youtube_source_type,
    normalize_youtube_source_url,
    rank_candidate,
    scrape_youtube_source,
)
from nicheflow_studio.scraper.instagram import (
    infer_instagram_source_type,
    instagram_source_label,
    normalize_instagram_source_url,
)
from nicheflow_studio.scraper.instagram_apify import (
    ApifyConfigError,
    ApifyScrapeError,
    scrape_instagram_source_apify,
    scrape_instagram_urls_apify,
)
from nicheflow_studio.downloader.instagram import (
    instagram_shortcode_from_url,
    validate_instagram_media_url,
)


class NoScrollComboBox(QComboBox):
    """QComboBox that ignores mouse wheel events.

    The processing/accounts/scheduling pages live inside a scroll area
    packed with combos and spinboxes. With Qt's default behavior, rolling
    the wheel over a combo silently changes the selection — so users
    scrolling the page accidentally flip the Template, Title Style, or
    Font without realising it. Ignoring the wheel event here lets the
    event bubble up to the scroll area, which is what the user expects.
    The combo still opens normally on click and accepts keyboard input
    when focused.
    """

    def wheelEvent(self, event) -> None:  # noqa: ANN001 — Qt event type
        event.ignore()


class NoScrollSpinBox(QSpinBox):
    """QSpinBox that ignores mouse wheel events for the same reason as
    NoScrollComboBox. Click + type or use the up/down arrows to change."""

    def wheelEvent(self, event) -> None:  # noqa: ANN001 — Qt event type
        event.ignore()


APP_STYLESHEET = """
QWidget {
    background: #0f1318;
    color: #e6edf3;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QFrame#panel {
    background: #141a21;
    border: 1px solid #242f3d;
    border-radius: 12px;
}
QFrame#sidebar {
    background: #0f151c;
    border: 1px solid #253142;
    border-radius: 18px;
}
QFrame#pageHeader {
    background: #11161d;
    border: 1px solid #243041;
    border-radius: 14px;
}
QFrame#downloadQueuePanel {
    background: #141a21;
    border: 1px solid #242f3d;
    border-radius: 12px;
}
QWidget#downloadToolbar {
    background: #141a21;
}
QLabel#downloadQueueTitle {
    background: transparent;
    color: #f4f7fb;
    font-size: 10pt;
    font-weight: 700;
}
QLabel#downloadQueueSummary {
    background: transparent;
    color: #8aa0b8;
    font-size: 8.5pt;
    font-weight: 600;
}
QLineEdit#downloadSearchInput, QComboBox#downloadFilter {
    background: #0f151c;
    border: 1px solid #263446;
    border-radius: 9px;
    color: #edf3f9;
    padding: 8px 11px;
}
QPushButton#downloadToolbarButton {
    background: #121922;
    border: 1px solid #253142;
    border-radius: 9px;
    color: #c6d2df;
    padding: 7px 11px;
    font-weight: 600;
}
QPushButton#downloadToolbarButton:hover {
    background: #192637;
    border-color: #355a80;
}
QTableWidget#downloadQueueTable {
    background: #11161d;
    alternate-background-color: #151c25;
    border: none;
    border-radius: 0;
    color: #e6edf3;
    gridline-color: #1b2532;
    selection-background-color: #223349;
    selection-color: #edf3f9;
}
QTableWidget#downloadQueueTable QHeaderView::section {
    background: #141a21;
    color: #8aa0b8;
    border: none;
    border-bottom: 1px solid #242f3d;
    padding: 8px 10px;
    font-weight: 700;
}
QTableWidget#downloadQueueTable::item {
    padding: 7px 8px;
    border-bottom: 1px solid #202a37;
}
QProgressBar#queueStatusBar {
    background: #273244;
    border: none;
    border-radius: 4px;
    color: #d7e0ea;
    font-size: 8pt;
    font-weight: 700;
    max-height: 11px;
    text-align: center;
}
QProgressBar#queueStatusBar::chunk {
    background: #4f7bd9;
    border-radius: 4px;
}
QProgressBar#queueStatusBar[status="downloaded"]::chunk {
    background: #4f7bd9;
}
QProgressBar#queueStatusBar[status="downloading"]::chunk {
    background: #f0a94a;
}
QProgressBar#queueStatusBar[status="queued"]::chunk {
    background: #8aa0b8;
}
QProgressBar#queueStatusBar[status="failed"]::chunk {
    background: #dc5b61;
}
QLabel#downloadDropZone {
    background: transparent;
    color: #8aa0b8;
    border: 2px dashed #2c3a4c;
    border-radius: 10px;
    padding: 22px;
    font-size: 10pt;
    font-weight: 600;
}
QLabel#eyebrow {
    color: #8aa0b8;
    font-size: 8pt;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
QLabel#headline {
    color: #f4f7fb;
    font-size: 13pt;
    font-weight: 600;
}
QLabel#sectionTitle {
    color: #d7e0ea;
    font-size: 11pt;
    font-weight: 600;
}
QLabel#subtleLabel {
    color: #8aa0b8;
    font-size: 8.5pt;
}
QLabel#metaLabel {
    color: #89a0b8;
    font-size: 9pt;
    font-weight: 600;
    text-transform: uppercase;
}
QLabel#metaValue {
    color: #edf3f9;
    background: #10161d;
    border: none;
    border-bottom: 1px solid #223042;
    padding: 7px 4px 10px 4px;
}
QLabel#videoPreview {
    background: #10161d;
    border: none;
    padding: 0px;
}
QLabel#statusLabel {
    background: #111827;
    border: 1px solid #273244;
    border-radius: 10px;
    color: #b8c7d9;
    padding: 8px 10px;
}
QLabel#statusLabel[tone="success"] {
    color: #8ee6b1;
    border-color: #1d5f3b;
}
QLabel#statusLabel[tone="warning"] {
    color: #f5cd79;
    border-color: #72531c;
}
QLabel#statusLabel[tone="error"] {
    color: #ff9c9c;
    border-color: #7a2f36;
}
QFrame#activityBar {
    background: #101820;
    border: 1px solid #263446;
    border-radius: 10px;
}
QFrame#activityBar[tone="success"] {
    border-color: #2d7d5a;
}
QFrame#activityBar[tone="warning"] {
    border-color: #9f7a2d;
}
QFrame#activityBar[tone="error"] {
    border-color: #8f3038;
}
QLabel#activityStatus {
    background: transparent;
    color: #c6d2df;
    font-size: 9pt;
    font-weight: 600;
}
QProgressBar#activityProgress {
    background: #0b1118;
    border: 1px solid #263446;
    border-radius: 4px;
    color: #c6d2df;
    font-size: 8pt;
    font-weight: 600;
    max-height: 8px;
    text-align: center;
}
QProgressBar#activityProgress::chunk {
    background: #4f7bd9;
    border-radius: 4px;
}
QLabel#toast {
    background: #0f1720;
    border: 1px solid #273244;
    border-radius: 12px;
    color: #e6edf3;
    padding: 12px 14px;
}
QLabel#toast[tone="success"] {
    border-color: #1d5f3b;
    background: #102317;
}
QLabel#toast[tone="info"] {
    border-color: #284766;
    background: #101b28;
}
QLabel#toast[tone="warning"] {
    border-color: #72531c;
    background: #2a1f0f;
}
QLabel#toast[tone="error"] {
    border-color: #7a2f36;
    background: #2a1417;
}
QLineEdit, QComboBox {
    background: #0f151c;
    border: 1px solid #263446;
    border-radius: 10px;
    color: #edf3f9;
    padding: 9px 12px;
    selection-background-color: #365880;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #4b88c7;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: #11161d;
    border: 1px solid #273244;
    color: #edf3f9;
    selection-background-color: #223349;
}
QComboBox#tableCombo {
    background: #141c25;
    border: 1px solid #2a3a4d;
    border-radius: 8px;
    color: #e6edf3;
    padding: 6px 10px;
    min-height: 24px;
}
QComboBox#tableCombo:focus {
    border: 1px solid #4b88c7;
}
QTabWidget::pane {
    background: #11161d;
    border: 1px solid #273244;
    border-radius: 12px;
    margin-top: 8px;
    padding: 12px;
}
QTabBar::tab {
    background: #141d27;
    color: #9fb1c6;
    border: 1px solid #273244;
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    padding: 8px 14px;
    margin-right: 6px;
    min-width: 110px;
}
QTabBar::tab:selected {
    background: #223349;
    color: #edf3f9;
    border-color: #4b88c7;
}
QTabBar::tab:hover:!selected {
    background: #1a2734;
    color: #d7e0ea;
}
QProgressBar {
    background: #10161d;
    border: 1px solid #273244;
    border-radius: 10px;
    color: #dfe8f2;
    min-height: 18px;
    text-align: center;
}
QProgressBar::chunk {
    background: #3f6ea1;
    border-radius: 8px;
}
QProgressBar#thinProgress {
    min-height: 6px;
    max-height: 6px;
    border-radius: 4px;
}
QProgressBar#thinProgress::chunk {
    border-radius: 4px;
}
QPushButton {
    background: #223349;
    border: 1px solid #345170;
    border-radius: 10px;
    color: #eff6ff;
    font-weight: 600;
    padding: 6px 10px;
    min-height: 32px;
}
QPushButton:hover {
    background: #2a425d;
}
QPushButton:pressed {
    background: #203246;
}
QPushButton:disabled {
    background: #18212d;
    border-color: #273244;
    color: #728295;
}
QTableWidget {
    background: #11161d;
    alternate-background-color: #131a22;
    border: 1px solid #222d3a;
    border-radius: 10px;
    gridline-color: #1b2532;
    selection-background-color: #223349;
    selection-color: #edf3f9;
}
QHeaderView::section {
    background: #131b24;
    color: #9fb1c6;
    border: none;
    border-bottom: 1px solid #222d3a;
    padding: 9px 10px;
    font-weight: 600;
}
QScrollBar:vertical {
    background: #0f1720;
    width: 14px;
    margin: 8px 4px 8px 4px;
    border-radius: 7px;
}
QScrollBar::handle:vertical {
    background: #36506f;
    min-height: 36px;
    border-radius: 7px;
}
QScrollBar::handle:vertical:hover {
    background: #46709b;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    background: transparent;
    border: none;
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
    border-radius: 7px;
}
QScrollBar:horizontal {
    background: #0f1720;
    height: 14px;
    margin: 4px 8px 4px 8px;
    border-radius: 7px;
}
QScrollBar::handle:horizontal {
    background: #36506f;
    min-width: 36px;
    border-radius: 7px;
}
QScrollBar::handle:horizontal:hover {
    background: #46709b;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    background: transparent;
    border: none;
    width: 0px;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
    border-radius: 7px;
}
QPushButton#ghostButton {
    background: #121922;
    border: 1px solid #253142;
    color: #c6d2df;
}
QPushButton#ghostButton:hover {
    background: #1a2734;
}
QPushButton#smartOptionCard {
    background: #141d27;
    border: 1px solid #273244;
    border-radius: 12px;
    color: #d7e0ea;
    padding: 12px;
    min-height: 78px;
    text-align: left;
}
QPushButton#smartOptionCard:hover {
    background: #1a2734;
    border-color: #4b88c7;
}
QPushButton#smartOptionCard:checked {
    background: #223349;
    border-color: #4b88c7;
    color: #eff6ff;
}
QTextEdit#smartOptionEdit {
    background: #0f1720;
    border: 1px solid #2c3a4c;
    border-radius: 10px;
    color: #edf3f9;
    padding: 8px 10px;
}
QPushButton#dangerButton {
    background: #2a1417;
    border: 1px solid #7a2f36;
    color: #ffd6d6;
}
QPushButton#dangerButton:hover {
    background: #34171b;
}
QPushButton#sidebarToggle {
    background: transparent;
    border: 1px solid transparent;
    color: #c9d7e8;
    padding: 0;
    min-height: 38px;
    min-width: 38px;
    max-height: 38px;
    max-width: 38px;
    border-radius: 12px;
    text-align: center;
    font-weight: 600;
}
QComboBox#sidebarAccountCombo {
    background: #0c1117;
    border: 1px solid #2a3b50;
    border-radius: 9px;
    color: #edf3f9;
    padding: 7px 9px;
    min-height: 30px;
}
QLabel#sidebarAccountLabel {
    background: transparent;
    border: none;
    color: #8aa0b8;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.08em;
    padding: 6px 2px 0 2px;
}
QPushButton#sidebarToggle:hover {
    background: #161f2a;
    border-color: #253142;
    color: #e6edf3;
}
QPushButton#sidebarToggle[selected="true"] {
    background: #192637;
    border-color: #355a80;
    color: #f4f8fc;
}
QPushButton#sidebarToggle:checked {
    background: #192637;
    border-color: #355a80;
    color: #f4f8fc;
}
"""


ICON_DIR = Path(__file__).resolve().parents[3] / "assets" / "icons"


REVIEW_STATE_OPTIONS = [
    ("Ready to review", "new"),
    ("Kept in library", "kept"),
    ("Ignored from library", "rejected"),
]
CANDIDATE_STATES = ["candidate", "queued", "downloaded", "ignored"]
DISCOVERY_MODES = {
    "review_only": "Review Only",
    "auto_queue": "Auto Queue Top Results",
}
UPLOAD_PRIVACY_OPTIONS = {
    "private": "Private",
    "unlisted": "Unlisted",
    "public": "Public",
}
MODULE_PAGES = ("scraping", "downloads", "processing", "uploads", "session_health", "pooling")
INSTAGRAM_MAX_RESULT_LIMIT = 1000
TITLE_STYLE_PRESETS: dict[str, dict[str, object]] = {
    "clean_hook": {
        "label": "Clean Hook",
        "font_size": 60,
        "font_name": "arial_bold",
        "text_color": "#FFFFFF",
        "background": "none",
    },
    "boxed_banner": {
        "label": "Boxed Banner",
        "font_size": 50,
        "font_name": "arial_bold",
        "text_color": "#F8FAFC",
        "background": "dark",
    },
    "stacked_bold": {
        "label": "Stacked Bold",
        "font_size": 60,
        "font_name": "impact",
        "text_color": "#FFF2BF",
        "background": "none",
    },
    "editorial_label": {
        "label": "Editorial Label",
        "font_size": 48,
        "font_name": "bahnschrift",
        "text_color": "#F8FAFC",
        "background": "dark",
    },
    "cinematic_serif": {
        "label": "Cinematic Serif",
        "font_size": 44,
        "font_name": "georgia_italic",
        "text_color": "#F2E8D0",
        "background": "none",
    },
    "cinematic_soft_italic": {
        "label": "Cinematic Soft Italic",
        "font_size": 58,
        "font_name": "comic_italic",
        "text_color": "#F7F3EA",
        "background": "none",
    },
    "cinema_bold_rounded": {
        "label": "Cinema Bold Rounded",
        "font_size": 56,
        "font_name": "arial_rounded_bold",
        "text_color": "#FFFFFF",
        "background": "none",
    },
    "cinema_georgia_clean": {
        "label": "Cinema Georgia Clean",
        "font_size": 54,
        "font_name": "georgia",
        "text_color": "#F2E8D0",
        "background": "none",
    },
    "lilita_style": {
        "label": "Lilita One Style",
        "font_size": 50,
        "font_name": "lilita_one_style",
        "text_color": "#FFFFFF",
        "background": "none",
    },
    "grobold_style": {
        "label": "Grobold Style",
        "font_size": 48,
        "font_name": "grobold_style",
        "text_color": "#FFF7D6",
        "background": "none",
    },
}


class _PlainPasteTextEdit(QTextEdit):
    """QTextEdit base that always pastes plain text and replaces the current
    selection. The default Qt behavior should already replace a selection on
    paste, but we override insertFromMimeData explicitly so the behavior is
    deterministic regardless of clipboard MIME shape (HTML, RTF, image+text
    bundles) or focus quirks — a select-all then paste must overwrite the old
    value, never append next to it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptRichText(False)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt override.
        text = source.text() if source is not None and source.hasText() else ""
        if not text:
            return
        self.textCursor().insertText(text)


class MultilineTitleEdit(_PlainPasteTextEdit):
    """Plain-text title editor that preserves setup/punchline line breaks."""

    def __init__(self, *, min_height: int = 58, max_height: int = 92) -> None:
        super().__init__()
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setMinimumHeight(min_height)
        self.setMaximumHeight(max_height)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLineEdit API.
        self.setPlainText(text)


class MultilineCaptionEdit(_PlainPasteTextEdit):
    """Multi-line caption editor with plain-text paste and selection-replace."""

    def __init__(
        self,
        *,
        min_height: int | None = None,
        size_policy: tuple[QSizePolicy.Policy, QSizePolicy.Policy] | None = None,
    ) -> None:
        super().__init__()
        if min_height is not None:
            self.setMinimumHeight(min_height)
        if size_policy is not None:
            self.setSizePolicy(*size_policy)


PROCESSING_TEMPLATES: dict[str, dict[str, object]] = {
    "gaming_meme_black": {
        "label": "Gaming Meme Black",
        "title_style": "clean_hook",
        "layout": "top_band",
        "font_size": 64,
        "font_name": "arial_bold",
        "text_color": "#FFFFFF",
        "background": "none",
        "prompt_profile": "gaming_meme",
    },
    "reaction_clip_black": {
        "label": "Reaction Clip Black",
        "title_style": "clean_hook",
        "layout": "top_band",
        "font_size": 60,
        "font_name": "arial_bold",
        "text_color": "#FFFFFF",
        "background": "none",
        "prompt_profile": "reaction_clip",
    },
    "story_reel_clean": {
        "label": "Story Reel Clean",
        "title_style": "editorial_label",
        "layout": "top_band",
        "font_size": 56,
        "font_name": "arial_bold",
        "text_color": "#FFFFFF",
        "background": "none",
        "prompt_profile": "story_reel",
    },
    "lost_archive_black": {
        "label": "Past Moments Black",
        "title_style": "editorial_label",
        "layout": "top_band",
        # Smaller body so the editorial caption reads calm and sits well clear
        # of the phone safe-area edges (a 56px title wrapped close to the canvas
        # edge and felt shouty); 46px keeps it readable with comfortable margins.
        "font_size": 46,
        "font_name": "arial_bold",
        "text_color": "#FFFFFF",
        "background": "none",
        "prompt_profile": "past_moments",
    },
    "cinematic_study": {
        "label": "Cinematic Study",
        "title_style": "cinematic_soft_italic",
        "layout": "top_band",
        "font_size": 58,
        "font_name": "comic_italic",
        "text_color": "#F7F3EA",
        "background": "none",
        "prompt_profile": "cinema_study",
    },
    "cinema_viral_bold": {
        "label": "Cinema Viral Bold",
        "title_style": "cinema_bold_rounded",
        "layout": "top_band",
        "font_size": 56,
        "font_name": "arial_rounded_bold",
        "text_color": "#FFFFFF",
        "background": "none",
        "prompt_profile": "cinema_study",
    },
    "cinema_normal": {
        "label": "Cinema Normal",
        "title_style": "cinema_georgia_clean",
        "layout": "top_band",
        "font_size": 54,
        "font_name": "georgia",
        "text_color": "#F2E8D0",
        "background": "none",
        "prompt_profile": "cinema_study",
    },
    "cinema_bold_keywords": {
        "label": "Cinema Bold Keywords",
        "title_style": "cinema_georgia_clean",
        "prompt_title_style": "cinema_bold_keywords",
        "layout": "top_band",
        "font_size": 54,
        "font_name": "georgia",
        "text_color": "#F2E8D0",
        "background": "none",
        "prompt_profile": "cinema_study",
        "bold_keywords": True,
    },
    "full_video_overlay": {
        "label": "Full Video Overlay",
        "title_style": "boxed_banner",
        "layout": "overlay",
        "font_size": 50,
        "font_name": "arial_bold",
        "text_color": "#F8FAFC",
        "background": "dark",
        "prompt_profile": "broad_short_form",
    },
}

TITLE_LAYOUT_CHOICES: list[tuple[str, str]] = [
    ("Black Canvas", "top_band"),
    ("Overlay", "overlay"),
    ("No Title", "no_title"),
]

TITLE_FONT_CHOICES: list[tuple[str, str]] = [
    ("Segoe UI", "segoe_ui"),
    ("Bahnschrift", "bahnschrift"),
    ("Arial Bold", "arial_bold"),
    ("Arial Rounded Bold", "arial_rounded_bold"),
    ("Impact", "impact"),
    ("Comic Sans Italic", "comic_italic"),
    ("Georgia Italic", "georgia_italic"),
    ("Georgia", "georgia"),
    ("Lilita One Style", "lilita_one_style"),
    ("Grobold Style", "grobold_style"),
]


@dataclass(frozen=True)
class UiStrings:
    title: str = "NicheFlow Studio"
    eyebrow: str = "Download"
    headline: str = "Paste a YouTube or Instagram link, or use Scrape to build the queue"
    url_placeholder: str = "Paste a YouTube, Shorts, Instagram Reel, or post URL..."
    add_button: str = "Download"
    history_title: str = "Review Queue"
    detail_title: str = "Review Decision"
    detail_placeholder: str = "Select a clip to judge if it fits this niche account."


@dataclass(frozen=True)
class Tone:
    INFO: str = "info"
    SUCCESS: str = "success"
    WARNING: str = "warning"
    ERROR: str = "error"


@dataclass(frozen=True)
class ScrapeJobConfig:
    account_id: int
    source_ids: list[int]
    keywords: list[str]
    max_items: int
    max_age_days: int | None
    discovery_mode: str
    auto_queue_limit: int
    min_view_count: int
    min_like_count: int
    weights: DiscoveryWeights
    archive_backfill: bool = False


@dataclass(frozen=True)
class InstagramDiscoverRankJobConfig:
    username: str
    target_account_id: int
    target_account_name: str
    min_new: int
    limit: int
    scrolls: int = 30
    stall_limit: int = 8
    wait_ms: int = 4000


@dataclass(frozen=True)
class ProcessJobConfig:
    input_path: Path
    output_path: Path
    crop: CropSettings
    title_text: str | None = None
    title_font_size: int = 60
    title_font_name: str = "arial_bold"
    title_color: str = "#FFFFFF"
    title_background: str = "none"
    title_layout: str = "top_band"
    enable_bold_keywords: bool = False
    watermark_replacement_text: str | None = None
    audio_mode: str = "keep"


@dataclass(frozen=True)
class SuggestCropJobConfig:
    input_path: Path


@dataclass(frozen=True)
class TranscriptDraftJobConfig:
    input_path: Path
    fallback_title: str | None


@dataclass(frozen=True)
class SmartDraftJobConfig:
    transcript_text: str
    source_title: str | None
    niche_label: str | None
    source_description: str | None = None
    input_path: Path | None = None
    transcript_available: bool = False
    account_voice: dict[str, str] | None = None
    prompt_profile: str | None = None
    caption_style: str | None = None
    title_style: str | None = None
    recent_titles: list[str] | None = None
    recent_captions: list[str] | None = None


# Auto-publish safety limits. Per-account daily cap keeps an accidental
# mass-post from triggering a ban; the cooldown pauses an account after
# Instagram shows a checkpoint/challenge so we stop hammering it.
PUBLISH_DAILY_CAP = 5
PUBLISH_CHECKPOINT_COOLDOWN = dt.timedelta(hours=3)
# Randomized gap between posts in a batch run, so a multi-account sweep looks
# like a person spacing posts out rather than a burst. Applied BETWEEN posts
# only (single-account cadence stays free).
PUBLISH_BATCH_GAP_RANGE_SECONDS = (120, 420)  # 2-7 minutes
# Hook-tier gate for unattended batch publishing. A title whose applied hook
# tier resolves to one of these is HELD for manual review rather than
# auto-posted. Green (no checkable claim) and unknown/legacy (no tier data,
# or a manually edited title) pass through, so this never freezes the existing
# queue — it only catches AI hooks the model itself flagged as risky.
PUBLISH_AUTO_HOLD_TIERS = frozenset({"yellow", "red"})


@dataclass(frozen=True)
class PublishJobConfig:
    job_id: int
    profile_name: str
    video_path: Path
    caption: str
    do_share: bool = True


@dataclass(frozen=True)
class BatchJobPrep:
    """Result of re-validating a queued job before a batch publish.

    Either ``payload`` is set (account_id, profile_name, video_path, caption) and
    the job is ready to publish, or ``skip_reason`` explains why the batch dropped
    it — so a skip is never silent in the "Publish Due Now" summary.
    """

    payload: tuple[int, str, Path, str] | None = None
    skip_reason: str | None = None


@dataclass(frozen=True)
class DraftRecommendation:
    title_index: int | None
    caption_index: int | None
    reason: str | None = None
    option_notes: list[str] | None = None
    option_tiers: list[str] | None = None


@dataclass(frozen=True)
class PastedSmartDraft:
    """Structured result of parsing a copy-pasted smart-draft text blob."""

    title_options: list[str]
    caption_options: list[str]
    option_notes: list[str]
    recommended_styles: list[str]
    recommended_title_index: int | None
    recommended_caption_index: int | None
    reason: str | None


# Section headers in the generated text blob. ``title``/``caption``/``style``
# carry a 1-based option number; ``pick``/``why``/``notes`` are singletons.
_PASTED_DRAFT_HEADERS: list[tuple[str, re.Pattern[str]]] = [
    ("title", re.compile(r"^title\s*option\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)),
    ("caption", re.compile(r"^caption\s*option\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)),
    ("style", re.compile(r"^recommended\s*style\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)),
    ("pick", re.compile(r"^recommended\s*pick\s*:\s*(.*)$", re.IGNORECASE)),
    ("why", re.compile(r"^why\s*:\s*(.*)$", re.IGNORECASE)),
    ("notes", re.compile(r"^selection\s*notes\s*:\s*(.*)$", re.IGNORECASE)),
]

_PASTED_NOTE_LINE = re.compile(r"^option\s*(\d+)\s*:\s*(.*)$", re.IGNORECASE)


def parse_pasted_smart_draft(text: str) -> PastedSmartDraft:
    """Parse a generated smart-draft text blob into structured options.

    Recognises ``Title Option N:`` / ``Caption Option N:`` /
    ``Recommended Style N:`` blocks plus the ``Recommended Pick:`` / ``Why:`` /
    ``Selection Notes:`` footer. Title/style content is collapsed to one line;
    caption content keeps its paragraph breaks. Bold ``**word**`` markers are
    preserved verbatim. Indices in the result are 0-based.
    """
    titles: dict[int, list[str]] = {}
    captions: dict[int, list[str]] = {}
    styles: dict[int, list[str]] = {}
    pick_buf: list[str] = []
    why_buf: list[str] = []
    notes_buf: list[str] = []
    indexed = {"title": titles, "caption": captions, "style": styles}
    singletons = {"pick": pick_buf, "why": why_buf, "notes": notes_buf}

    active_kind: str | None = None
    active_index: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if active_kind in indexed and active_index is not None:
            indexed[active_kind].setdefault(active_index, []).extend(buffer)
        elif active_kind in singletons:
            singletons[active_kind].extend(buffer)
        buffer = []

    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip()
        header_match = None
        for kind, pattern in _PASTED_DRAFT_HEADERS:
            match = pattern.match(stripped)
            if match:
                header_match = (kind, match)
                break
        if header_match is None:
            buffer.append(raw_line.rstrip())
            continue
        flush()
        kind, match = header_match
        if kind in indexed:
            active_kind = kind
            active_index = int(match.group(1))
            inline = match.group(2).strip()
        else:
            active_kind = kind
            active_index = None
            inline = match.group(1).strip()
        buffer = [inline] if inline else []
    flush()

    def _join_line(lines: list[str]) -> str:
        return " ".join(part.strip() for part in lines if part.strip()).strip()

    def _join_caption(lines: list[str]) -> str:
        joined = "\n".join(lines).strip()
        return re.sub(r"\n{3,}", "\n\n", joined)

    max_index = max([*titles, *captions, *styles], default=0)
    title_options = [_join_line(titles.get(i, [])) for i in range(1, max_index + 1)]
    caption_options = [_join_caption(captions.get(i, [])) for i in range(1, max_index + 1)]
    recommended_styles = [_join_line(styles.get(i, [])) for i in range(1, max_index + 1)]

    # Drop trailing options that carry neither a title nor a caption.
    while title_options and not title_options[-1] and not caption_options[-1]:
        title_options.pop()
        caption_options.pop()
        if recommended_styles:
            recommended_styles.pop()

    note_map: dict[int, list[str]] = {}
    last_note_index: int | None = None
    for note_line in notes_buf:
        note_text = note_line.strip()
        if not note_text:
            continue
        note_match = _PASTED_NOTE_LINE.match(note_text)
        if note_match:
            last_note_index = int(note_match.group(1))
            note_map.setdefault(last_note_index, []).append(note_match.group(2).strip())
        elif last_note_index is not None:
            note_map[last_note_index].append(note_text)
    note_count = max([len(title_options), *note_map.keys()], default=0)
    option_notes = [
        " ".join(part for part in note_map.get(i, []) if part).strip()
        for i in range(1, note_count + 1)
    ]

    pick_text = " ".join(pick_buf)
    title_pick = re.search(r"title\s*option\s*(\d+)", pick_text, re.IGNORECASE)
    caption_pick = re.search(r"caption\s*option\s*(\d+)", pick_text, re.IGNORECASE)
    recommended_title_index = int(title_pick.group(1)) - 1 if title_pick else None
    recommended_caption_index = int(caption_pick.group(1)) - 1 if caption_pick else None
    reason = _join_line(why_buf) or None

    return PastedSmartDraft(
        title_options=title_options,
        caption_options=caption_options,
        option_notes=option_notes,
        recommended_styles=recommended_styles,
        recommended_title_index=recommended_title_index,
        recommended_caption_index=recommended_caption_index,
        reason=reason,
    )


class ScrapeWorker(QObject):
    progress = pyqtSignal(dict)
    source_completed = pyqtSignal(dict)
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, main_window: "MainWindow", job: ScrapeJobConfig) -> None:
        super().__init__()
        self._window = main_window
        self._job = job

    def run(self) -> None:
        try:
            total_created = 0
            total_refreshed = 0
            total_skipped = 0
            total_rejected = 0
            sources = [
                source
                for source in self._window._load_sources_for_account(self._job.account_id)
                if source.id in self._job.source_ids
            ]
            for index, source in enumerate(sources, start=1):
                self.progress.emit(
                    {
                        "current": index,
                        "total": len(sources),
                        "source_label": source.label,
                    }
                )
                (
                    created_count,
                    refreshed_count,
                    skipped_count,
                    rejected_count,
                ) = self._window._run_scrape_for_source(
                    account_id=self._job.account_id,
                    source=source,
                    keywords=self._job.keywords,
                    max_items=self._job.max_items,
                    max_age_days=self._job.max_age_days,
                    min_view_count=self._job.min_view_count,
                    min_like_count=self._job.min_like_count,
                    weights=self._job.weights,
                    archive_backfill=self._job.archive_backfill,
                )
                total_created += created_count
                total_refreshed += refreshed_count
                total_skipped += skipped_count
                total_rejected += rejected_count
                self.source_completed.emit(
                    {
                        "source_label": source.label,
                        "created": created_count,
                        "refreshed": refreshed_count,
                        "skipped": skipped_count,
                        "rejected": rejected_count,
                    }
                )

            auto_queued_count = 0
            if self._job.discovery_mode == "auto_queue" and self._job.auto_queue_limit > 0:
                auto_queued_count = self._window._auto_queue_top_candidates(
                    account_id=self._job.account_id,
                    limit=self._job.auto_queue_limit,
                )

            self.completed.emit(
                {
                    "sources": len(sources),
                    "created": total_created,
                    "refreshed": total_refreshed,
                    "skipped": total_skipped,
                    "rejected": total_rejected,
                    "auto_queued": auto_queued_count,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class InstagramDiscoverRankWorker(QObject):
    log = pyqtSignal(str)
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job: InstagramDiscoverRankJobConfig) -> None:
        super().__init__()
        self._job = job

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[3]

    @staticmethod
    def _safe_username(username: str) -> str:
        return "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_"
            for char in username.strip().lstrip("@")
        )

    @staticmethod
    def _safe_path_part(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
        return cleaned.lower() or "account"

    @staticmethod
    def _url_cache_count(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            raw_text = path.read_text(encoding="utf-8").strip().strip("\ufeff")
            if not raw_text:
                return 0
            if path.suffix.lower() == ".json":
                loaded = json.loads(raw_text)
                return len(loaded) if isinstance(loaded, list) else 0
            return len(
                [
                    line
                    for line in raw_text.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
            )
        except Exception:  # noqa: BLE001
            return 0

    @classmethod
    def _account_cache_output_path(
        cls,
        *,
        account_id: int,
        account_name: str,
        username: str,
    ) -> Path:
        safe_account = cls._safe_path_part(account_name)
        safe_username = cls._safe_username(username)
        return (
            Path("data")
            / "discovered"
            / "accounts"
            / f"{account_id}-{safe_account}"
            / f"{safe_username}-urls.json"
        )

    def _run_command(self, args: list[str]) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            args,
            cwd=self._project_root(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            cleaned = line.rstrip()
            if cleaned:
                self.log.emit(cleaned)
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"Command failed with exit code {return_code}: {' '.join(args)}")

    def run(self) -> None:
        try:
            raise RuntimeError(
                "Legacy Instagram Playwright discovery is disabled. "
                "Use the normal Scrape flow, which uses Apify without Instagram login cookies."
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class ProcessWorker(QObject):
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job: ProcessJobConfig) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            output_path = export_cropped_video(
                input_path=self._job.input_path,
                output_path=self._job.output_path,
                crop=self._job.crop,
                title_text=self._job.title_text,
                title_font_size=self._job.title_font_size,
                title_font_name=self._job.title_font_name,
                title_color=self._job.title_color,
                title_background=self._job.title_background,
                title_layout=self._job.title_layout,
                enable_bold_keywords=self._job.enable_bold_keywords,
                audio_mode=self._job.audio_mode,
            )
            watermark_payload: dict[str, object] = {
                "watermark_replaced": False,
                "watermark_skipped_reason": None,
                "watermark_detected_text": None,
                "watermark_replacement_text": self._job.watermark_replacement_text,
            }
            if self._job.watermark_replacement_text:
                temp_output = output_path.with_stem(output_path.stem + "_watermark")
                replacement = replace_detected_watermark(
                    output_path,
                    replacement_text=self._job.watermark_replacement_text,
                    output_path=temp_output,
                )
                watermark_payload["watermark_skipped_reason"] = replacement.skipped_reason
                watermark_payload["watermark_detected_text"] = (
                    replacement.region.text if replacement.region is not None else None
                )
                watermark_payload["watermark_replacement_text"] = replacement.replacement_text
                if replacement.output_path is not None:
                    Path(replacement.output_path).replace(output_path)
                    watermark_payload["watermark_replaced"] = True
            self.completed.emit({"output_path": str(output_path), **watermark_payload})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class PublishWorker(QObject):
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job: PublishJobConfig) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            result = publish_reel(
                self._job.profile_name,
                self._job.video_path,
                self._job.caption,
                do_share=self._job.do_share,
            )
            self.completed.emit(
                {
                    "job_id": self._job.job_id,
                    "status": result.status,
                    "posted_url": result.posted_url,
                    "error_message": result.error_message,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class AccountHealthCheckWorker(QObject):
    """Run live 'who am I?' checks for many accounts off the UI thread.

    Spaces requests out so a multi-account confirmation does not burst
    Instagram. Emits one ``result`` per account as it finishes.
    """

    result = pyqtSignal(dict)
    completed = pyqtSignal()

    def __init__(self, targets: list[tuple[str, str, str | None]]) -> None:
        super().__init__()
        # Each target: (account_name, profile_name, expected_handle_or_None).
        self._targets = targets

    def run(self) -> None:
        import time

        for index, (account_name, profile_name, expected_handle) in enumerate(self._targets):
            if index > 0:
                time.sleep(random.uniform(2.0, 4.0))  # gentle spacing between live checks
            try:
                health = live_health(
                    profile_name, account_name, expected_username=expected_handle
                )
                payload = {
                    "account_name": account_name,
                    "profile_name": profile_name,
                    "state": health.state,
                    "detail": health.detail,
                    "username": health.username,
                }
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "account_name": account_name,
                    "profile_name": profile_name,
                    "state": HealthState.UNKNOWN,
                    "detail": f"check failed: {exc}",
                    "username": None,
                }
            self.result.emit(payload)
        self.completed.emit()


class SuggestCropWorker(QObject):
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job: SuggestCropJobConfig) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            suggestion = suggest_crop_settings(self._job.input_path)
            diagnostics: PreprocessingOcrDiagnostics | None = None
            diagnostics_error: str | None = None
            try:
                diagnostics = diagnose_preprocessing_ocr(self._job.input_path)
            except Exception as exc:  # noqa: BLE001
                diagnostics_error = str(exc)
            self.completed.emit(
                {
                    "crop": suggestion.crop,
                    "reasons": list(suggestion.reasons),
                    "used_border_detection": suggestion.used_border_detection,
                    "used_ocr": suggestion.used_ocr,
                    "ocr_diagnostics": diagnostics,
                    "ocr_diagnostics_error": diagnostics_error,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class TranscriptDraftWorker(QObject):
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job: TranscriptDraftJobConfig) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            draft = generate_transcript_draft_in_subprocess(
                self._job.input_path,
                fallback_title=self._job.fallback_title,
            )
            self.completed.emit(
                {
                    "transcript_text": draft.transcript_text,
                    "title_draft": draft.title_draft,
                    "caption_draft": draft.caption_draft,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class SmartDraftWorker(QObject):
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job: SmartDraftJobConfig) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            drafts = generate_smart_drafts(
                transcript_text=self._job.transcript_text,
                source_title=self._job.source_title,
                source_description=self._job.source_description,
                niche_label=self._job.niche_label,
                input_path=self._job.input_path,
                account_voice=self._job.account_voice,
                prompt_profile=self._job.prompt_profile,
                caption_style=self._job.caption_style,
                title_style=self._job.title_style,
                recent_titles=self._job.recent_titles,
                recent_captions=self._job.recent_captions,
            )
            self.completed.emit(
                {
                    "summary": drafts.summary,
                    "title_options": drafts.title_options,
                    "caption_options": drafts.caption_options,
                    "recommended_title_index": drafts.recommended_title_index,
                    "recommended_caption_index": drafts.recommended_caption_index,
                    "recommendation_reason": drafts.recommendation_reason,
                    "option_notes": drafts.option_notes,
                    "option_tiers": drafts.option_tiers,
                    "provider_label": drafts.provider_label,
                    "used_fallback": drafts.used_fallback,
                    "vision_payload": drafts.vision_payload,
                    "generation_meta": drafts.generation_meta,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class TableFocusScrollWidget(QTableWidget):
    def wheelEvent(self, event) -> None:  # noqa: ANN001
        scrollbar = self.verticalScrollBar()
        before = scrollbar.value()
        super().wheelEvent(event)
        if scrollbar.value() != before or scrollbar.maximum() > 0:
            event.accept()
            return
        event.ignore()


class CurrentPageStackedWidget(QStackedWidget):
    def sizeHint(self) -> QSize:
        current_widget = self.currentWidget()
        if current_widget is None:
            return super().sizeHint()
        return current_widget.sizeHint()

    def minimumSizeHint(self) -> QSize:
        current_widget = self.currentWidget()
        if current_widget is None:
            return super().minimumSizeHint()
        return current_widget.minimumSizeHint()


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._ui = UiStrings()
        self._default_window_width = 1220
        self._default_window_height = 780
        self._minimum_window_width = 1100
        self._minimum_window_height = 720
        self._selected_item_id: int | None = None
        self._selected_candidate_id: int | None = None
        self._selected_source_id: int | None = None
        self._current_page = "downloads"
        self._displayed_items: list[DownloadItem] = []
        self._displayed_candidates: list[ScrapeCandidate] = []
        self._displayed_sources: list[Source] = []
        self._displayed_runs: list[ScrapeRun] = []
        self._accounts: list[Account] = []
        self._account_mode = "main"
        self._pending_refresh = False
        self._scrape_thread: QThread | None = None
        self._scrape_worker: ScrapeWorker | None = None
        self._scrape_in_progress = False
        self._instagram_discover_thread: QThread | None = None
        self._instagram_discover_worker: InstagramDiscoverRankWorker | None = None
        self._instagram_discover_in_progress = False
        self._process_thread: QThread | None = None
        self._process_worker: ProcessWorker | None = None
        self._suggest_thread: QThread | None = None
        self._suggest_worker: SuggestCropWorker | None = None
        self._draft_thread: QThread | None = None
        self._draft_worker: TranscriptDraftWorker | None = None
        self._smart_draft_thread: QThread | None = None
        self._smart_draft_worker: SmartDraftWorker | None = None
        self._publish_thread: QThread | None = None
        self._publish_worker: PublishWorker | None = None
        self._publish_in_progress = False
        # account_id -> UTC datetime the account stays paused until (set after
        # Instagram shows a checkpoint for that account).
        self._account_publish_cooldown: dict[int, dt.datetime] = {}
        # Batch ("Publish Due Now") state. The queue holds job ids; posts run
        # one at a time with a randomized gap scheduled via QTimer.singleShot.
        self._publish_batch_active = False
        self._publish_batch_queue: list[int] = []
        self._publish_batch_posted = 0
        self._publish_batch_skipped = 0
        # Deduped human-readable reasons for jobs the batch skipped (cap reached,
        # cooldown, missing file), surfaced in the final "Batch done" summary.
        self._batch_skip_reasons: list[str] = []
        self._last_due_count = 0
        # Last directory used by the "Import MP4" file dialog; persists for the
        # session so re-opening the dialog doesn't reset to home every time.
        self._last_import_dir: Path | None = None
        self._account_health_thread: QThread | None = None
        self._account_health_worker: AccountHealthCheckWorker | None = None
        self._account_health_in_progress = False
        self._session_health_page: QWidget | None = None
        self._processing_in_progress = False
        self._processing_busy_mode: str | None = None
        self._selected_processing_item_id: int | None = None
        self._processing_style_loaded_for_item_id: int | None = None
        self._processing_item_probe_cache: dict[int, tuple[str, int, int, bool]] = {}
        self._processing_source_labels: dict[int, str] = {}
        self._processing_clip_premises: dict[int, str] = {}
        self._processing_probe_item_id: int | None = None
        self._processing_probe: VideoProbe | None = None
        self._processing_last_output_path: Path | None = None
        self._last_created_schedule_job_id: int | None = None
        self._processing_auto_crop = CropSettings()
        self._processing_using_ai_layout_crop = False
        self._processing_applying_ai_layout_suggestion = False
        self._processing_ai_suggested_layout: str | None = None
        self._processing_pending_job: ProcessJobConfig | None = None
        self._processing_raw_transcript_text: str = ""
        self._processing_provider_label_text: str = ""
        self._processing_generation_meta_text: str = ""
        self._processing_vision_payload_text: str = ""
        self._processing_vision_payload: dict[str, object] | None = None
        self._processing_generated_at_text: str = ""
        self._processing_dirty_item_id: int | None = None
        self._suppress_processing_text_dirty = False
        self._processing_last_generated_titles: list[str] = []
        self._processing_preview_path: Path | None = None
        self._processing_preview_container: av.container.InputContainer | None = None
        self._processing_preview_stream = None
        self._processing_preview_frame_iter = None
        self._processing_preview_duration_ms: int = 0
        self._processing_preview_position_ms: int = 0
        self._processing_preview_last_frame_ms: int | None = None
        self._processing_preview_mode: str = "source"
        self._suppress_interaction_tracking = False
        self._suppress_account_form_sync = False
        self._last_view_signature: tuple[tuple[object, ...], ...] | None = None
        self._last_candidate_signature: tuple[tuple[object, ...], ...] | None = None
        self.setWindowTitle(self._ui.title)
        self.setStyleSheet(APP_STYLESHEET)

        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

        self._interaction_idle_timer = QTimer(self)
        self._interaction_idle_timer.setSingleShot(True)
        self._interaction_idle_timer.timeout.connect(self._on_interaction_idle)
        self._processing_loading_timer = QTimer(self)
        self._processing_loading_timer.setInterval(450)
        self._processing_loading_timer.timeout.connect(self._on_processing_loading_tick)
        self._processing_loading_base_text = ""
        self._processing_loading_phase = 0

        self._eyebrow_label = QLabel(self._ui.eyebrow)
        self._eyebrow_label.setObjectName("eyebrow")
        self._headline_label = QLabel(self._ui.headline)
        self._headline_label.setObjectName("headline")
        self._headline_label.setWordWrap(True)

        self._status_label = QLabel("Ready.")
        self._status_label.setObjectName("statusLabel")

        self._activity_bar = QFrame()
        self._activity_bar.setObjectName("activityBar")
        self._activity_bar.setVisible(False)
        activity_layout = QVBoxLayout()
        activity_layout.setContentsMargins(12, 9, 12, 9)
        activity_layout.setSpacing(6)
        self._activity_status_label = QLabel("Ready.")
        self._activity_status_label.setObjectName("activityStatus")
        self._activity_status_label.setWordWrap(True)
        self._activity_progress_bar = QProgressBar()
        self._activity_progress_bar.setObjectName("activityProgress")
        self._activity_progress_bar.setTextVisible(False)
        self._activity_progress_bar.setVisible(False)
        self._activity_progress_bar.setMinimum(0)
        self._activity_progress_bar.setMaximum(1)
        self._activity_progress_bar.setValue(0)
        activity_layout.addWidget(self._activity_status_label)
        activity_layout.addWidget(self._activity_progress_bar)
        self._activity_bar.setLayout(activity_layout)

        self._toast_label = QLabel(self)
        self._toast_label.setObjectName("toast")
        self._toast_label.setVisible(False)
        self._toast_label.setWordWrap(True)
        self._toast_label.setMinimumWidth(260)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText(self._ui.url_placeholder)

        self._download_button = QPushButton(self._ui.add_button)
        self._download_button.clicked.connect(self._on_download_clicked)
        self._import_local_button = QPushButton("Import MP4")
        self._import_local_button.clicked.connect(self._on_import_local_clicked)
        self._sidebar_toggle_button = QPushButton()
        self._sidebar_toggle_button.setObjectName("sidebarToggle")
        self._sidebar_toggle_button.clicked.connect(self._toggle_account_sidebar)
        self._sidebar_toggle_button.setToolTip("Open account manager")
        self._sidebar_toggle_button.setCheckable(True)
        self._sidebar_toggle_button.setText("")
        self._sidebar_toggle_button.setIcon(self._sidebar_icon("account-manager"))
        self._sidebar_toggle_button.setIconSize(QSize(20, 20))
        self._sidebar_toggle_button.setFixedSize(38, 38)
        self._sidebar_toggle_button.setProperty("selected", False)

        # Dedicated Session Health entry point (its own sidebar button, so it is
        # not buried in the account drawer and opens with full dialog space).
        self._sidebar_health_button = QPushButton()
        self._sidebar_health_button.setObjectName("sidebarToggle")
        self._sidebar_health_button.setToolTip("Publishing Dashboard")
        self._sidebar_health_button.setText("")
        self._sidebar_health_button.setIcon(self._sidebar_icon("shield"))
        self._sidebar_health_button.setIconSize(QSize(20, 20))
        self._sidebar_health_button.setFixedSize(38, 38)
        self._sidebar_health_button.clicked.connect(self._open_session_health_dialog)
        self._module_buttons: dict[str, QPushButton] = {}
        self._sidebar_account_combo = NoScrollComboBox()
        self._sidebar_account_combo.setObjectName("sidebarAccountCombo")
        self._sidebar_account_combo.currentIndexChanged.connect(self._on_sidebar_account_changed)
        self._sidebar_account_label = QLabel("Account")
        self._sidebar_account_label.setObjectName("sidebarAccountLabel")

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(self._url_input, stretch=1)
        top_row.addWidget(self._download_button)
        top_row.addWidget(self._import_local_button)

        hero_panel = QFrame()
        hero_panel.setObjectName("pageHeader")
        hero_layout = QVBoxLayout()
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_layout.setSpacing(8)
        hero_layout.addWidget(self._eyebrow_label)
        hero_layout.addWidget(self._headline_label)
        hero_layout.addLayout(top_row)
        hero_layout.addWidget(self._status_label)
        hero_panel.setLayout(hero_layout)

        account_title = QLabel("Niche Accounts")
        account_title.setObjectName("sectionTitle")

        workspace_title = QLabel("Active Niche")
        workspace_title.setObjectName("sectionTitle")
        workspace_hint = QLabel(
            "Choose the niche account you are building content for. Scrape, downloads, drafts, and scheduling use this workspace."
        )
        workspace_hint.setObjectName("subtleLabel")
        workspace_hint.setWordWrap(True)

        self._current_account_id: int | None = None
        # Set True while we're WRITING widget values from a saved account
        # snapshot — prevents the change signals we just programmatically
        # emitted from feeding straight back into another save and looping.
        self._suppress_processing_prefs_save = False
        self._current_account_combo = NoScrollComboBox()
        self._current_account_combo.currentIndexChanged.connect(self._on_current_account_changed)

        workspace_panel = QFrame()
        workspace_panel.setObjectName("panel")
        workspace_layout = QVBoxLayout()
        workspace_layout.setContentsMargins(14, 12, 14, 12)
        workspace_layout.setSpacing(8)
        workspace_layout.addWidget(workspace_title)
        workspace_layout.addWidget(workspace_hint)
        workspace_layout.addWidget(self._current_account_combo)
        workspace_panel.setLayout(workspace_layout)

        manage_title = QLabel("Manage Niches")
        manage_title.setObjectName("sectionTitle")
        manage_hint = QLabel(
            "Create a niche account, edit its content rules, or remove one you no longer use."
        )
        manage_hint.setObjectName("subtleLabel")
        manage_hint.setWordWrap(True)

        self._account_picker = NoScrollComboBox()
        self._account_picker.currentIndexChanged.connect(self._on_account_picker_changed)
        self._account_name_input = QLineEdit()
        self._account_name_input.setPlaceholderText("Account name")
        self._account_platform_combo = NoScrollComboBox()
        self._account_platform_combo.addItem("YouTube", "youtube")
        self._account_platform_combo.addItem("Instagram", "instagram")
        self._account_login_input = QLineEdit()
        self._account_login_input.setPlaceholderText("Login email or @username (used for re-login)")
        self._account_instagram_profile_input = QLineEdit()
        self._account_instagram_profile_input.setPlaceholderText("e.g. main, alt1, cinema")
        self._account_instagram_handle_input = QLineEdit()
        self._account_instagram_handle_input.setPlaceholderText(
            "@handle — verifies the right account is logged in (optional)"
        )
        self._account_scrape_sources_input = QLineEdit()
        self._account_scrape_sources_input.setPlaceholderText("Managed from Source Intake below")
        self._account_scrape_sources_input.setReadOnly(True)
        self._account_scrape_max_items_input = QLineEdit()
        self._account_scrape_max_items_input.setPlaceholderText("20")
        self._account_scrape_max_age_days_input = QLineEdit()
        self._account_scrape_max_age_days_input.setPlaceholderText("30")
        self._account_discovery_keywords_input = QLineEdit()
        self._account_discovery_keywords_input.setPlaceholderText(
            "Keywords / phrases (comma separated)"
        )
        self._account_discovery_mode_combo = NoScrollComboBox()
        for mode_value, mode_label in DISCOVERY_MODES.items():
            self._account_discovery_mode_combo.addItem(mode_label, mode_value)
        self._account_auto_queue_limit_input = QLineEdit()
        self._account_auto_queue_limit_input.setPlaceholderText("3")
        self._account_min_view_count_input = QLineEdit()
        self._account_min_view_count_input.setPlaceholderText("10000")
        self._account_min_like_count_input = QLineEdit()
        self._account_min_like_count_input.setPlaceholderText("500")
        self._account_weight_views_input = QLineEdit()
        self._account_weight_views_input.setPlaceholderText("35")
        self._account_weight_likes_input = QLineEdit()
        self._account_weight_likes_input.setPlaceholderText("20")
        self._account_weight_recency_input = QLineEdit()
        self._account_weight_recency_input.setPlaceholderText("25")
        self._account_weight_keyword_input = QLineEdit()
        self._account_weight_keyword_input.setPlaceholderText("20")

        def account_strategy_input(placeholder: str) -> QTextEdit:
            editor = QTextEdit()
            editor.setAcceptRichText(False)
            editor.setPlaceholderText(placeholder)
            editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            editor.setTabChangesFocus(True)
            editor.setMinimumHeight(82)
            editor.setMaximumHeight(132)
            editor.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            return editor

        self._account_niche_input = account_strategy_input("Content niche / category")
        self._account_credential_input = account_strategy_input("Private notes for this account")
        self._account_writing_tone_input = account_strategy_input(
            "playful, direct, dramatic..."
        )
        self._account_target_audience_input = account_strategy_input(
            "Who this account is trying to reach"
        )
        self._account_hook_style_input = account_strategy_input(
            "reaction-first, curiosity, payoff..."
        )
        self._account_banned_phrases_input = account_strategy_input("Phrases to avoid")
        self._account_title_style_notes_input = account_strategy_input("Short rules for titles")
        self._account_caption_style_notes_input = account_strategy_input(
            "Short rules for captions"
        )
        self._account_upload_timezone_input = QLineEdit()
        self._account_upload_timezone_input.setPlaceholderText("Asia/Jakarta")
        self._account_upload_privacy_combo = NoScrollComboBox()
        for privacy_value, privacy_label in UPLOAD_PRIVACY_OPTIONS.items():
            self._account_upload_privacy_combo.addItem(privacy_label, privacy_value)
        self._account_upload_schedule_slots_input = QLineEdit()
        self._account_upload_schedule_slots_input.setPlaceholderText("09:00, 18:00")
        self._account_upload_made_for_kids_combo = NoScrollComboBox()
        self._account_upload_made_for_kids_combo.addItem("No", 0)
        self._account_upload_made_for_kids_combo.addItem("Yes", 1)
        self._account_upload_synthetic_media_combo = NoScrollComboBox()
        self._account_upload_synthetic_media_combo.addItem("No", 0)
        self._account_upload_synthetic_media_combo.addItem("Yes", 1)

        self._account_mode_label = QLabel("Main")
        self._account_mode_label.setObjectName("sectionTitle")
        self._account_mode_hint = QLabel(
            "Create a new niche account, edit an existing niche, or remove one."
        )
        self._account_mode_hint.setObjectName("subtleLabel")
        self._account_mode_hint.setWordWrap(True)

        self._account_main_new_button = QPushButton("New Niche Account")
        self._account_main_new_button.clicked.connect(self._show_new_account_form)
        self._account_main_edit_button = QPushButton("Edit Niche Account")
        self._account_main_edit_button.clicked.connect(self._show_edit_account_form)
        self._account_main_delete_button = QPushButton("Delete Niche Account")
        self._account_main_delete_button.setObjectName("dangerButton")
        self._account_main_delete_button.clicked.connect(self._show_delete_account_panel)

        self._account_main_actions = QWidget()
        account_main_actions_layout = QVBoxLayout()
        account_main_actions_layout.setContentsMargins(0, 0, 0, 0)
        account_main_actions_layout.setSpacing(10)
        account_main_actions_layout.addWidget(self._account_main_new_button)
        account_main_actions_layout.addWidget(self._account_main_edit_button)
        account_main_actions_layout.addWidget(self._account_main_delete_button)
        self._account_main_actions.setLayout(account_main_actions_layout)

        self._account_picker_label = QLabel("Niche Account")
        self._account_picker_label.setObjectName("metaLabel")
        self._account_picker_panel = QWidget()
        account_picker_layout = QVBoxLayout()
        account_picker_layout.setContentsMargins(0, 0, 0, 0)
        account_picker_layout.setSpacing(6)
        account_picker_layout.addWidget(self._account_picker_label)
        account_picker_layout.addWidget(self._account_picker)
        self._account_picker_panel.setLayout(account_picker_layout)

        account_form = QGridLayout()
        account_form.setHorizontalSpacing(10)
        account_form.setVerticalSpacing(10)
        account_form.setColumnStretch(0, 0)
        account_form.setColumnStretch(1, 1)
        account_form.addWidget(QLabel("Name"), 0, 0)
        account_form.addWidget(self._account_name_input, 0, 1)
        account_form.addWidget(QLabel("Platform"), 1, 0)
        account_form.addWidget(self._account_platform_combo, 1, 1)
        account_form.addWidget(QLabel("Content Niche"), 2, 0)
        account_form.addWidget(self._account_niche_input, 2, 1)
        account_form.addWidget(QLabel("Posting Profile"), 3, 0)
        account_form.addWidget(self._account_login_input, 3, 1)
        account_form.addWidget(QLabel("IG Browser Profile"), 4, 0)
        account_form.addWidget(self._account_instagram_profile_input, 4, 1)
        account_form.addWidget(QLabel("IG Handle (expected)"), 5, 0)
        account_form.addWidget(self._account_instagram_handle_input, 5, 1)
        account_form.addWidget(QLabel("Private Notes"), 6, 0)
        account_form.addWidget(self._account_credential_input, 6, 1)
        account_form.addWidget(QLabel("AI Tone"), 7, 0)
        account_form.addWidget(self._account_writing_tone_input, 7, 1)
        account_form.addWidget(QLabel("Audience"), 8, 0)
        account_form.addWidget(self._account_target_audience_input, 8, 1)
        account_form.addWidget(QLabel("Hook Pattern"), 9, 0)
        account_form.addWidget(self._account_hook_style_input, 9, 1)
        account_form.addWidget(QLabel("Avoid Phrases"), 10, 0)
        account_form.addWidget(self._account_banned_phrases_input, 10, 1)
        account_form.addWidget(QLabel("Title Rules"), 11, 0)
        account_form.addWidget(self._account_title_style_notes_input, 11, 1)
        account_form.addWidget(QLabel("Caption Rules"), 12, 0)
        account_form.addWidget(self._account_caption_style_notes_input, 12, 1)
        self._account_form_panel = QWidget()
        account_form_panel_layout = QVBoxLayout()
        account_form_panel_layout.setContentsMargins(0, 0, 0, 0)
        account_form_panel_layout.setSpacing(10)
        account_form_panel_layout.addLayout(account_form)
        account_form_panel_layout.addSpacing(150)
        self._account_form_panel.setLayout(account_form_panel_layout)

        self._account_form_scroll = QScrollArea()
        self._account_form_scroll.setWidgetResizable(True)
        self._account_form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._account_form_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._account_form_scroll.setMinimumHeight(420)
        self._account_form_scroll.setWidget(self._account_form_panel)

        self._account_save_button = QPushButton("Create Niche Account")
        self._account_save_button.clicked.connect(self._on_save_account_clicked)
        self._account_cancel_button = QPushButton("Back to Main")
        self._account_cancel_button.setObjectName("ghostButton")
        self._account_cancel_button.clicked.connect(self._show_account_main)

        self._account_form_actions = QWidget()
        account_form_actions_layout = QHBoxLayout()
        account_form_actions_layout.setContentsMargins(0, 0, 0, 0)
        account_form_actions_layout.setSpacing(10)
        account_form_actions_layout.addWidget(self._account_save_button)
        account_form_actions_layout.addWidget(self._account_cancel_button)
        self._account_form_actions.setLayout(account_form_actions_layout)

        self._account_delete_picker = NoScrollComboBox()
        self._account_delete_picker_label = QLabel("Choose niche account to delete")
        self._account_delete_picker_label.setObjectName("metaLabel")
        self._account_delete_button = QPushButton("Delete Selected Niche")
        self._account_delete_button.setObjectName("dangerButton")
        self._account_delete_button.clicked.connect(self._on_delete_account_clicked)
        self._account_delete_cancel_button = QPushButton("Back to Main")
        self._account_delete_cancel_button.setObjectName("ghostButton")
        self._account_delete_cancel_button.clicked.connect(self._show_account_main)

        self._account_delete_panel = QWidget()
        account_delete_layout = QVBoxLayout()
        account_delete_layout.setContentsMargins(0, 0, 0, 0)
        account_delete_layout.setSpacing(10)
        account_delete_layout.addWidget(self._account_delete_picker_label)
        account_delete_layout.addWidget(self._account_delete_picker)
        account_delete_actions = QHBoxLayout()
        account_delete_actions.setSpacing(10)
        account_delete_actions.addWidget(self._account_delete_button)
        account_delete_actions.addWidget(self._account_delete_cancel_button)
        account_delete_layout.addLayout(account_delete_actions)
        self._account_delete_panel.setLayout(account_delete_layout)

        self._account_panel = QFrame()
        self._account_panel.setObjectName("panel")
        self._account_panel.setMinimumWidth(600)
        self._account_panel.setMaximumWidth(760)
        self._account_panel.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )
        account_layout = QVBoxLayout()
        account_layout.setContentsMargins(16, 14, 16, 14)
        account_layout.setSpacing(10)
        account_layout.addWidget(account_title)
        account_layout.addWidget(workspace_panel)
        account_layout.addWidget(manage_title)
        account_layout.addWidget(manage_hint)
        account_layout.addWidget(self._account_mode_label)
        account_layout.addWidget(self._account_mode_hint)
        account_layout.addWidget(self._account_main_actions)
        account_layout.addWidget(self._account_picker_panel)
        account_layout.addWidget(self._account_form_scroll, stretch=1)
        account_layout.addWidget(self._account_form_actions)
        account_layout.addWidget(self._account_delete_panel)
        self._account_panel.setLayout(account_layout)

        sidebar_modules = [
            ("scraping", "Scrape", "refresh"),
            ("downloads", "Download", "play"),
            ("processing", "Preprocess", "refresh"),
            ("uploads", "Publish", "check"),
            ("pooling", "Pool & Distribute", "refresh"),
        ]
        self._sidebar_nav = QWidget()
        sidebar_nav_layout = QVBoxLayout()
        sidebar_nav_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_nav_layout.setSpacing(10)
        for page_name, tooltip, icon_name in sidebar_modules:
            button = QPushButton()
            button.setObjectName("sidebarToggle")
            button.setText("")
            button.setToolTip(tooltip)
            button.setCheckable(True)
            button.setIcon(self._sidebar_icon(icon_name))
            button.setIconSize(QSize(18, 18))
            button.setFixedSize(38, 38)
            button.setProperty("selected", False)
            button.clicked.connect(
                lambda checked=False, target=page_name: self._set_current_page(target)
            )
            self._module_buttons[page_name] = button
            sidebar_nav_layout.addWidget(button)
        self._sidebar_nav.setLayout(sidebar_nav_layout)
        self._sidebar_nav.setFixedHeight(246)

        self._sidebar_panel = QFrame()
        self._sidebar_panel.setObjectName("sidebar")
        self._sidebar_panel.setFixedWidth(64)
        self._sidebar_panel.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )
        sidebar_layout = QVBoxLayout()
        sidebar_layout.setContentsMargins(8, 12, 8, 12)
        sidebar_layout.setSpacing(10)
        sidebar_layout.addWidget(
            self._sidebar_toggle_button,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        sidebar_layout.addWidget(
            self._sidebar_health_button,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self._sidebar_account_label.setVisible(False)
        self._sidebar_account_combo.setVisible(False)
        sidebar_layout.addWidget(self._sidebar_nav, alignment=Qt.AlignmentFlag.AlignCenter)
        sidebar_layout.addStretch(1)
        self._sidebar_panel.setLayout(sidebar_layout)

        history_title = QLabel(self._ui.history_title)
        history_title.setObjectName("downloadQueueTitle")

        self._download_queue_summary = QLabel("No downloads yet.")
        self._download_queue_summary.setObjectName("downloadQueueSummary")

        self._search_input = QLineEdit()
        self._search_input.setObjectName("downloadSearchInput")
        self._search_input.setPlaceholderText("Search clips...")
        self._search_input.setMinimumWidth(220)
        self._search_input.setMaximumWidth(300)
        self._search_input.textChanged.connect(self._on_search_changed)

        self._status_filter = NoScrollComboBox()
        self._status_filter.setObjectName("downloadFilter")
        self._status_filter.setMinimumWidth(160)
        self._status_filter.setMaximumWidth(190)
        self._status_filter.addItems(
            ["All statuses", "queued", "downloading", "downloaded", "failed"]
        )
        self._status_filter.currentIndexChanged.connect(self._on_status_filter_changed)

        self._review_filter = NoScrollComboBox()
        self._review_filter.setObjectName("downloadFilter")
        self._review_filter.addItem("All review states", "all")
        for label, state in REVIEW_STATE_OPTIONS:
            self._review_filter.addItem(label, state)
        self._review_filter.currentIndexChanged.connect(self._on_status_filter_changed)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        filter_row.addWidget(self._status_filter)
        filter_row.addWidget(self._search_input, stretch=1)
        self._review_filter.setVisible(False)

        self._table = TableFocusScrollWidget()
        self._table.setObjectName("downloadQueueTable")
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels(
            ["Status", "Fit", "Account", "Name", "Source URL", "File", "Size", "Added"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._table.verticalScrollBar().setSingleStep(18)
        self._table.setMinimumHeight(230)
        self._table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)

        self._batch_keep_button = QPushButton("Keep For This Account")
        self._batch_keep_button.clicked.connect(
            lambda: self._set_review_state_for_selection("kept")
        )
        self._batch_ignore_button = QPushButton("Ignore")
        self._batch_ignore_button.setObjectName("ghostButton")
        self._batch_ignore_button.clicked.connect(
            lambda: self._set_review_state_for_selection("rejected")
        )
        self._batch_return_button = QPushButton("Needs Review")
        self._batch_return_button.setObjectName("ghostButton")
        self._batch_return_button.clicked.connect(
            lambda: self._set_review_state_for_selection("new")
        )

        batch_row = QHBoxLayout()
        batch_row.setSpacing(10)
        for button in (
            self._batch_keep_button,
            self._batch_ignore_button,
            self._batch_return_button,
        ):
            button.setObjectName("downloadToolbarButton")
        batch_row.addWidget(self._batch_keep_button)
        batch_row.addWidget(self._batch_ignore_button)
        batch_row.addWidget(self._batch_return_button)
        batch_row.addStretch(1)

        toolbar = QWidget()
        toolbar.setObjectName("downloadToolbar")
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(10)
        toolbar_layout.addWidget(history_title)
        toolbar_layout.addWidget(self._download_queue_summary)
        toolbar_layout.addStretch(1)
        toolbar_layout.addLayout(filter_row)
        toolbar.setLayout(toolbar_layout)

        history_panel = QFrame()
        history_panel.setObjectName("downloadQueuePanel")
        history_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        history_layout = QVBoxLayout()
        history_layout.setContentsMargins(14, 12, 14, 12)
        history_layout.setSpacing(10)
        history_layout.addWidget(toolbar)
        self._download_advanced_row = QWidget()
        self._download_advanced_row.setLayout(batch_row)
        self._download_advanced_row.setVisible(False)
        history_layout.addWidget(self._download_advanced_row)
        self._library_gate_label = QLabel(
            "Choose a current account to open downloads and the library."
        )
        self._library_gate_label.setObjectName("metaValue")
        self._library_gate_label.setWordWrap(True)
        self._library_gate_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        history_layout.addWidget(self._table, stretch=1)
        self._download_drop_zone = QLabel(
            "Paste a YouTube or Instagram URL above, or send candidates to Download"
        )
        self._download_drop_zone.setObjectName("downloadDropZone")
        self._download_drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._download_drop_zone.setMaximumHeight(84)
        history_layout.addWidget(self._download_drop_zone)
        history_panel.setLayout(history_layout)

        intake_title = QLabel("Scrape")
        intake_title.setObjectName("sectionTitle")
        intake_hint = QLabel(
            "Add a YouTube source or Instagram hashtag/profile, fetch candidates, then send useful clips to Download."
        )
        intake_hint.setObjectName("subtleLabel")
        intake_hint.setWordWrap(True)
        intake_hint.setVisible(False)
        self._scrape_summary_label = QLabel("Select an account to configure source intake.")
        self._scrape_summary_label.setObjectName("subtleLabel")
        self._scrape_summary_label.setWordWrap(True)
        self._scrape_summary_label.setVisible(False)
        self._scrape_progress_label = QLabel("")
        self._scrape_progress_label.setObjectName("subtleLabel")
        self._scrape_progress_label.setWordWrap(True)
        self._scrape_progress_label.setVisible(False)
        self._scrape_progress_bar = QProgressBar()
        self._scrape_progress_bar.setTextVisible(True)
        self._scrape_progress_bar.setVisible(False)
        self._scrape_progress_bar.setMinimum(0)
        self._scrape_progress_bar.setMaximum(1)
        self._scrape_progress_bar.setValue(0)
        self._source_summary_label = QLabel("No source selected.")
        self._source_summary_label.setObjectName("subtleLabel")
        self._source_summary_label.setWordWrap(True)
        self._source_summary_label.setVisible(False)
        self._scrape_source_input = QLineEdit()
        self._scrape_source_input.setPlaceholderText(
            "YouTube URL, Instagram URL, #hashtag, or keyword..."
        )
        self._scrape_add_source_button = QPushButton("Add")
        self._scrape_add_source_button.clicked.connect(self._on_add_scrape_source_clicked)
        self._source_filter = NoScrollComboBox()
        self._source_filter.addItem("All sources", "all")
        self._source_filter.addItem("Enabled only", "enabled")
        self._source_filter.addItem("Disabled only", "disabled")
        self._source_filter.currentIndexChanged.connect(self._on_source_filter_changed)
        self._source_sort = NoScrollComboBox()
        self._source_sort.addItem("Sort: Priority", "priority")
        self._source_sort.addItem("Sort: Status", "status")
        self._source_sort.addItem("Sort: Last scraped", "last_scraped")
        self._source_sort.addItem("Sort: Label", "label")
        self._source_sort.currentIndexChanged.connect(self._on_source_filter_changed)
        self._source_filter.setVisible(False)
        self._source_sort.setVisible(False)
        self._source_remove_button = QPushButton("Remove")
        self._source_remove_button.setObjectName("ghostButton")
        self._source_remove_button.clicked.connect(self._on_remove_source_clicked)
        self._source_toggle_button = QPushButton("Disable")
        self._source_toggle_button.setObjectName("ghostButton")
        self._source_toggle_button.clicked.connect(self._on_toggle_source_clicked)
        self._scrape_selected_button = QPushButton("Scrape Selected")
        self._scrape_selected_button.clicked.connect(self._on_scrape_selected_clicked)
        self._scrape_button = QPushButton("Scrape All")
        self._scrape_button.clicked.connect(self._on_scrape_clicked)
        self._instagram_discover_source_combo = NoScrollComboBox()
        self._instagram_discover_source_combo.setMinimumWidth(220)
        self._instagram_discover_source_combo.setMaximumWidth(300)
        self._instagram_result_limit_input = NoScrollSpinBox()
        self._instagram_result_limit_input.setRange(1, INSTAGRAM_MAX_RESULT_LIMIT)
        self._instagram_result_limit_input.setValue(10)
        self._instagram_result_limit_input.setToolTip(
            "Maximum Apify results to request for either latest scan or archive backfill."
        )
        self._instagram_discover_min_likes_input = NoScrollSpinBox()
        self._instagram_discover_min_likes_input.setRange(0, 10_000_000)
        self._instagram_discover_min_likes_input.setSingleStep(1_000)
        self._instagram_discover_min_likes_input.setValue(20_000)
        self._instagram_discover_min_likes_input.valueChanged.connect(
            self._on_candidate_min_likes_filter_changed
        )
        self._candidate_sort_combo = NoScrollComboBox()
        self._candidate_sort_combo.addItem("Sort: Score", "score")
        self._candidate_sort_combo.addItem("Sort: Likes", "likes")
        self._candidate_sort_combo.addItem("Sort: Comments", "comments")
        self._candidate_sort_combo.addItem("Sort: Newest", "newest")
        self._candidate_sort_combo.currentIndexChanged.connect(self._on_candidate_filter_changed)
        self._candidate_sort_direction_combo = NoScrollComboBox()
        self._candidate_sort_direction_combo.addItem("High to low", "desc")
        self._candidate_sort_direction_combo.addItem("Low to high", "asc")
        self._candidate_sort_direction_combo.currentIndexChanged.connect(
            self._on_candidate_filter_changed
        )
        self._instagram_discover_button = QPushButton("Find Latest")
        self._instagram_discover_button.clicked.connect(self._on_instagram_discover_clicked)
        self._instagram_archive_button = QPushButton("Search Archive")
        self._instagram_archive_button.setToolTip(
            "Fetch a larger archive window with Apify, dedupe locally, then rank candidates."
        )
        self._instagram_archive_button.clicked.connect(self._on_instagram_archive_clicked)
        self._instagram_discover_log = QTextEdit()
        self._instagram_discover_log.setReadOnly(True)
        self._instagram_discover_log.setFixedHeight(86)
        self._instagram_discover_log.setPlaceholderText("Discovery log appears here.")

        intake_source_row = QHBoxLayout()
        intake_source_row.setSpacing(10)
        intake_source_row.addWidget(self._scrape_source_input, stretch=1)
        intake_source_row.addWidget(self._scrape_add_source_button)

        instagram_discover_row = QHBoxLayout()
        instagram_discover_row.setSpacing(10)
        instagram_discover_row.addWidget(QLabel("Scrape from"))
        instagram_discover_row.addWidget(self._instagram_discover_source_combo)
        instagram_discover_row.addWidget(QLabel("Results"))
        instagram_discover_row.addWidget(self._instagram_result_limit_input)
        instagram_discover_row.addWidget(self._instagram_discover_button)
        instagram_discover_row.addWidget(self._instagram_archive_button)
        instagram_discover_row.addStretch(1)
        self._instagram_discover_row = instagram_discover_row

        source_filter_row = QHBoxLayout()
        source_filter_row.setSpacing(10)
        source_filter_row.addWidget(self._source_filter)
        source_filter_row.addWidget(self._source_sort)
        source_filter_row.addStretch(1)

        self._source_table = TableFocusScrollWidget()
        self._source_table.setColumnCount(6)
        self._source_table.setHorizontalHeaderLabels(
            ["On", "Source", "Type", "URL", "Last Scraped", "Status"]
        )
        self._source_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._source_table.horizontalHeader().setStretchLastSection(True)
        self._source_table.verticalHeader().setVisible(False)
        self._source_table.setAlternatingRowColors(True)
        self._source_table.setShowGrid(True)
        self._source_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._source_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._source_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._source_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._source_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._source_table.setMinimumHeight(260)
        self._source_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._source_table.setColumnHidden(2, True)
        self._source_table.setColumnHidden(3, True)
        self._source_table.itemSelectionChanged.connect(self._on_source_selection_changed)

        source_actions = QHBoxLayout()
        source_actions.setSpacing(10)
        source_actions.addWidget(self._source_remove_button)
        source_actions.addWidget(self._source_toggle_button)
        source_actions.addWidget(self._scrape_selected_button)
        source_actions.addWidget(self._scrape_button)

        self._candidate_table = TableFocusScrollWidget()
        self._candidate_table.setColumnCount(10)
        self._candidate_table.setHorizontalHeaderLabels(
            [
                "Status",
                "Score",
                "Likes",
                "Comments",
                "Duration",
                "Added",
                "Published",
                "Channel",
                "Title",
                "Match",
            ]
        )
        self._candidate_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._candidate_table.horizontalHeader().setStretchLastSection(True)
        self._candidate_table.verticalHeader().setVisible(False)
        self._candidate_table.setAlternatingRowColors(True)
        self._candidate_table.setShowGrid(True)
        self._candidate_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._candidate_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._candidate_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._candidate_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._candidate_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._candidate_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._candidate_table.setWordWrap(False)
        self._candidate_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._candidate_table.verticalScrollBar().setSingleStep(18)
        self._candidate_table.setMinimumHeight(340)
        self._candidate_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._candidate_table.setColumnHidden(9, True)
        self._candidate_table.itemSelectionChanged.connect(self._on_candidate_selection_changed)
        # Double-click any cell in a candidate row → open the Instagram post in
        # the default browser so the user can preview it before downloading.
        self._candidate_table.cellDoubleClicked.connect(
            self._on_candidate_row_double_clicked
        )

        self._candidate_queue_button = QPushButton("Send To Download")
        self._candidate_queue_button.clicked.connect(self._on_candidate_queue_clicked)
        self._candidate_queue_button.setIcon(self._icon("play"))
        self._candidate_queue_button.setIconSize(QSize(16, 16))
        self._candidate_ignore_button = QPushButton("Ignore")
        self._candidate_ignore_button.setObjectName("ghostButton")
        self._candidate_ignore_button.clicked.connect(self._on_candidate_ignore_clicked)
        self._candidate_ignore_button.setIcon(self._icon("trash"))
        self._candidate_ignore_button.setIconSize(QSize(16, 16))
        self._candidate_restore_button = QPushButton("Restore")
        self._candidate_restore_button.setObjectName("ghostButton")
        self._candidate_restore_button.clicked.connect(self._on_candidate_restore_clicked)
        self._candidate_restore_button.setIcon(self._icon("refresh"))
        self._candidate_restore_button.setIconSize(QSize(16, 16))
        self._candidate_action_hint = QLabel("Select a candidate.")
        self._candidate_action_hint.setObjectName("subtleLabel")
        self._candidate_action_hint.setWordWrap(True)
        self._candidate_action_hint.setVisible(False)
        self._candidate_filter_label = QLabel("Showing candidates with 20,000+ likes.")
        self._candidate_filter_label.setObjectName("subtleLabel")
        self._candidate_filter_label.setWordWrap(True)
        self._candidate_state_filter = NoScrollComboBox()
        self._candidate_state_filter.addItem("All candidates", "all")
        self._candidate_state_filter.addItem("Ready to review", "candidate")
        self._candidate_state_filter.addItem("Queued for download", "queued")
        self._candidate_state_filter.addItem("Already downloaded", "downloaded")
        self._candidate_state_filter.addItem("Ignored for now", "ignored")
        self._candidate_state_filter.currentIndexChanged.connect(self._on_candidate_filter_changed)
        self._candidate_state_filter.setMinimumWidth(180)
        self._candidate_source_filter = NoScrollComboBox()
        self._candidate_source_filter.addItem("Source: All", "all")
        self._candidate_source_filter.currentIndexChanged.connect(self._on_candidate_filter_changed)
        self._candidate_source_filter.setMinimumWidth(220)

        intake_actions = QHBoxLayout()
        intake_actions.setSpacing(10)
        intake_actions.addWidget(QLabel("Min likes"))
        intake_actions.addWidget(self._instagram_discover_min_likes_input)
        intake_actions.addWidget(self._candidate_sort_combo)
        intake_actions.addWidget(self._candidate_sort_direction_combo)
        intake_actions.addWidget(self._candidate_state_filter)
        intake_actions.addWidget(self._candidate_source_filter)
        intake_actions.addStretch(1)
        intake_actions.addWidget(self._candidate_queue_button)
        intake_actions.addWidget(self._candidate_ignore_button)
        intake_actions.addWidget(self._candidate_restore_button)
        self._candidate_restore_button.setVisible(False)

        self._run_table = TableFocusScrollWidget()
        self._run_table.setColumnCount(6)
        self._run_table.setHorizontalHeaderLabels(
            ["Started", "Source", "Status", "Fetched", "Accepted", "Error"]
        )
        self._run_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._run_table.horizontalHeader().setStretchLastSection(True)
        self._run_table.verticalHeader().setVisible(False)
        self._run_table.setAlternatingRowColors(True)
        self._run_table.setShowGrid(True)
        self._run_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._run_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._run_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._run_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._run_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._run_table.setMinimumHeight(260)
        self._run_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        self._scrape_tabs = QTabWidget()
        self._scrape_tabs.setObjectName("panel")
        self._scrape_tabs.setMinimumHeight(430)
        self._scrape_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        source_tab = QWidget()
        source_tab_layout = QVBoxLayout()
        source_tab_layout.setContentsMargins(0, 0, 0, 0)
        source_tab_layout.setSpacing(12)
        source_tab_layout.addWidget(self._source_summary_label)
        source_tab_layout.addLayout(source_actions)
        source_tab_layout.addWidget(self._source_table, stretch=1)
        source_tab.setLayout(source_tab_layout)

        candidate_tab = QWidget()
        candidate_tab_layout = QVBoxLayout()
        candidate_tab_layout.setContentsMargins(0, 0, 0, 0)
        candidate_tab_layout.setSpacing(12)
        candidate_tab_layout.addWidget(self._candidate_action_hint)
        candidate_tab_layout.addLayout(intake_actions)
        candidate_tab_layout.addWidget(self._candidate_filter_label)
        candidate_tab_layout.addWidget(self._candidate_table, stretch=1)
        candidate_tab.setLayout(candidate_tab_layout)

        run_tab = QWidget()
        run_tab_layout = QVBoxLayout()
        run_tab_layout.setContentsMargins(0, 0, 0, 0)
        run_tab_layout.setSpacing(12)
        run_tab_layout.addWidget(self._run_table, stretch=1)
        run_tab.setLayout(run_tab_layout)

        self._scrape_tabs.addTab(candidate_tab, "Candidates")
        self._scrape_tabs.addTab(source_tab, "Sources")
        self._scrape_tabs.addTab(run_tab, "Activity")
        self._scrape_tabs.setTabVisible(2, False)

        intake_panel = QFrame()
        intake_panel.setObjectName("panel")
        intake_layout = QVBoxLayout()
        intake_layout.setContentsMargins(18, 18, 18, 18)
        intake_layout.setSpacing(12)
        intake_layout.addWidget(intake_title)
        intake_layout.addWidget(intake_hint)
        intake_layout.addLayout(intake_source_row)
        intake_layout.addLayout(instagram_discover_row)
        intake_layout.addWidget(self._instagram_discover_log)
        intake_layout.addWidget(self._scrape_summary_label)
        intake_layout.addWidget(self._scrape_progress_label)
        intake_layout.addWidget(self._scrape_progress_bar)
        intake_layout.addWidget(self._scrape_tabs, stretch=1)
        intake_panel.setLayout(intake_layout)
        intake_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._scrape_intake_panel = intake_panel
        self._scrape_intake_title = intake_title
        self._scrape_intake_hint = intake_hint
        self._scrape_intake_source_row = intake_source_row

        detail_title = QLabel(self._ui.detail_title)
        detail_title.setObjectName("sectionTitle")

        self._detail_panel = QFrame()
        self._detail_panel.setObjectName("panel")
        self._detail_panel.setMinimumWidth(380)
        self._detail_panel.setMaximumWidth(460)
        detail_layout = QVBoxLayout()
        detail_layout.setContentsMargins(14, 12, 14, 12)
        detail_layout.setSpacing(8)
        detail_header = QHBoxLayout()
        detail_header.addWidget(detail_title)
        self._close_detail_button = QPushButton("Close")
        self._close_detail_button.setObjectName("ghostButton")
        self._close_detail_button.setMaximumWidth(110)
        self._close_detail_button.clicked.connect(self._clear_selection)
        detail_header.addWidget(self._close_detail_button)
        detail_layout.addLayout(detail_header)

        self._detail_placeholder = QLabel(self._ui.detail_placeholder)
        self._detail_placeholder.setObjectName("metaValue")
        self._detail_placeholder.setWordWrap(True)
        detail_layout.addWidget(self._detail_placeholder)

        self._detail_review_hint = QLabel("Select a library item to review it.")
        self._detail_review_hint.setObjectName("subtleLabel")
        self._detail_review_hint.setWordWrap(True)
        detail_layout.addWidget(self._detail_review_hint)

        self._detail_grid = QGridLayout()
        self._detail_grid.setHorizontalSpacing(12)
        self._detail_grid.setVerticalSpacing(10)

        self._detail_fields: dict[str, QLabel] = {}
        self._detail_field_labels: dict[str, QLabel] = {}
        self._detail_advanced_keys = {"created", "extractor", "video_id", "source_url", "file_path"}
        detail_keys = [
            ("Title", "title"),
            ("Decision", "review"),
            ("Download", "status"),
            ("Niche Account", "account"),
            ("Created", "created"),
            ("Extractor", "extractor"),
            ("Video ID", "video_id"),
            ("Source URL", "source_url"),
            ("File Path", "file_path"),
            ("File Info", "file_info"),
            ("Error", "error"),
        ]
        for row, (label, key) in enumerate(detail_keys):
            meta_label = QLabel(label)
            meta_label.setObjectName("metaLabel")
            value_label = QLabel(self._ui.detail_placeholder)
            value_label.setObjectName("metaValue")
            value_label.setWordWrap(True)
            self._detail_grid.addWidget(meta_label, row, 0)
            self._detail_grid.addWidget(value_label, row, 1)
            self._detail_field_labels[key] = meta_label
            self._detail_fields[key] = value_label

        detail_layout.addLayout(self._detail_grid)
        self._detail_advanced_toggle = QPushButton("Show File Details")
        self._detail_advanced_toggle.setObjectName("ghostButton")
        self._detail_advanced_toggle.setCheckable(True)
        self._detail_advanced_toggle.toggled.connect(self._on_detail_advanced_toggled)
        detail_layout.addWidget(self._detail_advanced_toggle)
        assignment_row = QVBoxLayout()
        assignment_row.setSpacing(8)
        self._detail_account_combo = NoScrollComboBox()
        self._detail_account_combo.setMinimumWidth(220)
        self._detail_assign_button = QPushButton("Save Niche Assignment")
        self._detail_assign_button.clicked.connect(self._on_detail_assign_clicked)
        self._detail_assign_button.setIcon(self._icon("check"))
        self._detail_assign_button.setIconSize(QSize(16, 16))
        self._detail_assign_button.setMinimumHeight(38)
        assignment_row.addWidget(self._detail_account_combo)
        assignment_row.addWidget(self._detail_assign_button)
        detail_layout.addLayout(assignment_row)

        self._detail_action_row = QVBoxLayout()
        self._detail_action_row.setSpacing(8)

        self._detail_keep_button = QPushButton("Keep For This Account")
        self._detail_keep_button.setIcon(self._icon("check"))
        self._detail_keep_button.setIconSize(QSize(16, 16))
        self._detail_keep_button.setMinimumHeight(38)
        self._detail_keep_button.clicked.connect(
            lambda: self._set_review_state_for_selected("kept")
        )
        self._detail_reject_button = QPushButton("Ignore From Library")
        self._detail_reject_button.setObjectName("ghostButton")
        self._detail_reject_button.setIcon(self._icon("x"))
        self._detail_reject_button.setIconSize(QSize(16, 16))
        self._detail_reject_button.setMinimumHeight(38)
        self._detail_reject_button.clicked.connect(
            lambda: self._set_review_state_for_selected("rejected")
        )
        self._detail_reset_button = QPushButton("Return To Review")
        self._detail_reset_button.setObjectName("ghostButton")
        self._detail_reset_button.setIcon(self._icon("refresh"))
        self._detail_reset_button.setIconSize(QSize(16, 16))
        self._detail_reset_button.setMinimumHeight(38)
        self._detail_reset_button.clicked.connect(
            lambda: self._set_review_state_for_selected("new")
        )
        self._detail_open_button = QPushButton("Open Video")
        self._detail_open_button.clicked.connect(self._on_detail_open_clicked)
        self._detail_open_button.setIcon(self._icon("play"))
        self._detail_open_button.setIconSize(QSize(16, 16))
        self._detail_open_button.setToolTip("Open the downloaded file")
        self._detail_open_button.setMinimumHeight(38)
        self._detail_reveal_button = QPushButton("Open Folder")
        self._detail_reveal_button.clicked.connect(self._on_detail_reveal_clicked)
        self._detail_reveal_button.setIcon(self._icon("folder-open"))
        self._detail_reveal_button.setIconSize(QSize(16, 16))
        self._detail_reveal_button.setToolTip("Reveal the file inside its folder")
        self._detail_reveal_button.setMinimumHeight(38)
        self._detail_retry_button = QPushButton("Retry Download")
        self._detail_retry_button.clicked.connect(self._on_detail_retry_clicked)
        self._detail_retry_button.setIcon(self._icon("refresh"))
        self._detail_retry_button.setIconSize(QSize(16, 16))
        self._detail_retry_button.setToolTip("Retry this download")
        self._detail_retry_button.setMinimumHeight(38)
        self._detail_remove_button = QPushButton("Remove from Library")
        self._detail_remove_button.setObjectName("ghostButton")
        self._detail_remove_button.clicked.connect(self._on_detail_remove_clicked)
        self._detail_remove_button.setIcon(self._icon("trash"))
        self._detail_remove_button.setIconSize(QSize(16, 16))
        self._detail_remove_button.setToolTip("Remove this item from library history")
        self._detail_remove_button.setMinimumHeight(38)

        for button in (
            self._detail_keep_button,
            self._detail_reject_button,
            self._detail_reset_button,
            self._detail_open_button,
            self._detail_reveal_button,
            self._detail_retry_button,
            self._detail_remove_button,
        ):
            self._detail_action_row.addWidget(button)
        detail_layout.addLayout(self._detail_action_row)

        self._detail_panel.setLayout(detail_layout)
        self._detail_panel.setVisible(False)

        library_row = QHBoxLayout()
        library_row.setSpacing(16)
        library_row.addWidget(history_panel, stretch=1)

        self._library_workspace = QWidget()
        self._library_workspace.setLayout(library_row)

        self._scraping_page = QWidget()
        scraping_page_layout = QVBoxLayout()
        scraping_page_layout.setContentsMargins(0, 0, 0, 0)
        scraping_page_layout.setSpacing(16)
        scraping_page_layout.addWidget(intake_panel, stretch=1)
        self._scraping_page.setLayout(scraping_page_layout)

        self._downloads_page = QWidget()
        downloads_page_layout = QVBoxLayout()
        downloads_page_layout.setContentsMargins(0, 0, 0, 0)
        downloads_page_layout.setSpacing(16)
        downloads_page_layout.addWidget(hero_panel)
        downloads_page_layout.addWidget(self._library_workspace, stretch=1)
        self._downloads_page.setLayout(downloads_page_layout)

        self._processing_page = self._make_processing_page()
        self._uploads_page = self._make_schedule_page()
        self._accounts_page = self._make_accounts_page()
        self._session_health_page = self._make_session_health_page()
        self._pooling_page = self._make_pooling_page()

        self._library_gate_panel = QFrame()
        self._library_gate_panel.setObjectName("panel")
        library_gate_layout = QVBoxLayout()
        library_gate_layout.setContentsMargins(18, 18, 18, 18)
        library_gate_layout.setSpacing(8)
        library_gate_layout.addStretch(1)
        library_gate_layout.addWidget(self._library_gate_label)
        library_gate_layout.addStretch(1)
        self._library_gate_panel.setLayout(library_gate_layout)

        self._workspace_content = QWidget()
        workspace_content_layout = QVBoxLayout()
        workspace_content_layout.setContentsMargins(0, 0, 0, 0)
        workspace_content_layout.setSpacing(16)
        self._workspace_stack = CurrentPageStackedWidget()
        self._workspace_stack.addWidget(self._scraping_page)
        self._workspace_stack.addWidget(self._downloads_page)
        self._workspace_stack.addWidget(self._processing_page)
        self._workspace_stack.addWidget(self._uploads_page)
        self._workspace_stack.addWidget(self._session_health_page)
        self._workspace_stack.addWidget(self._pooling_page)
        workspace_content_layout.addWidget(self._workspace_stack)
        self._workspace_content.setLayout(workspace_content_layout)

        workspace_column = QVBoxLayout()
        workspace_column.setContentsMargins(0, 0, 0, 0)
        workspace_column.setSpacing(16)
        workspace_column.addWidget(self._library_gate_panel)
        workspace_column.addWidget(self._workspace_content)
        workspace_column.setAlignment(Qt.AlignmentFlag.AlignTop)

        workspace_panel = QWidget()
        workspace_panel.setLayout(workspace_column)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setWidget(workspace_panel)
        self._scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        body_row = QHBoxLayout()
        body_row.setContentsMargins(8, 16, 16, 16)
        body_row.setSpacing(12)
        body_row.addWidget(self._sidebar_panel, stretch=0)
        body_row.addWidget(self._account_panel, stretch=0)
        main_workspace_layout = QVBoxLayout()
        main_workspace_layout.setContentsMargins(0, 0, 0, 0)
        main_workspace_layout.setSpacing(10)
        main_workspace_layout.addWidget(self._activity_bar)
        main_workspace_layout.addWidget(self._scroll_area, stretch=1)
        body_row.addLayout(main_workspace_layout, stretch=1)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(body_row, stretch=1)

        self.setLayout(outer)
        self.setMinimumSize(self._minimum_window_width, self._minimum_window_height)
        self.resize(self._default_window_width, self._default_window_height)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(8000)
        self._refresh_timer.timeout.connect(self._request_refresh)
        self._refresh_timer.start()

        # Semi-auto scheduler: periodically surface how many reels are due so the
        # user gets a nudge + one-click batch, without unattended browser pop-ups.
        self._due_check_timer = QTimer(self)
        self._due_check_timer.setInterval(60000)
        self._due_check_timer.timeout.connect(self._update_due_badge)
        self._due_check_timer.start()

        self._set_status("Ready.", Tone.INFO)
        self._set_detail_placeholder()
        self._refresh_runtime_fields()
        self._refresh_account_controls()
        self._show_account_main()
        self._set_current_page("downloads")
        self._apply_refresh(force=True)

    @staticmethod
    def _output_text(item: DownloadItem) -> str:
        if item.file_path:
            return item.file_path
        if item.status == "failed" and item.error_message:
            return item.error_message
        return "(pending)"

    @staticmethod
    def _status_colors(status: str) -> tuple[QColor, QColor]:
        colors = {
            "queued": (QColor("#203246"), QColor("#9fc5f8")),
            "downloading": (QColor("#35270f"), QColor("#f5cd79")),
            "downloaded": (QColor("#11271a"), QColor("#8ee6b1")),
            "failed": (QColor("#34171b"), QColor("#ff9c9c")),
        }
        return colors.get(status, (QColor("#111827"), QColor("#d7e0ea")))

    @staticmethod
    def _review_colors(review_state: str) -> tuple[QColor, QColor]:
        colors = {
            "new": (QColor("#1d2633"), QColor("#b7c5d4")),
            "kept": (QColor("#11311b"), QColor("#8ee6b1")),
            "rejected": (QColor("#34171b"), QColor("#ff9c9c")),
        }
        return colors.get(review_state, (QColor("#111827"), QColor("#d7e0ea")))

    @staticmethod
    def _queue_status_bar(item: DownloadItem) -> QProgressBar:
        progress = QProgressBar()
        progress.setObjectName("queueStatusBar")
        progress.setProperty("status", item.status)
        progress.setTextVisible(True)
        progress.setMinimum(0)
        progress.setMaximum(100)
        progress.setValue(0)

        if item.status == "downloaded":
            progress.setValue(100)
            progress.setFormat("Completed")
        elif item.status == "downloading":
            progress.setValue(35)
            progress.setFormat("Downloading")
        elif item.status == "failed":
            progress.setValue(100)
            progress.setFormat("Failed")
        elif item.status == "queued":
            progress.setValue(8)
            progress.setFormat("Queued")
        else:
            progress.setFormat(item.status)
        return progress

    @staticmethod
    def _review_state_label(review_state: str) -> str:
        labels = {
            "new": "Needs Review",
            "kept": "Kept",
            "rejected": "Ignored",
        }
        return labels.get(review_state, review_state)

    @staticmethod
    def _review_state_message(review_state: str) -> str:
        messages = {
            "new": "Returned item to review.",
            "kept": "Kept item for this account.",
            "rejected": "Ignored item from this library.",
        }
        return messages.get(review_state, f"Marked item as {review_state}.")

    def _download_review_hint_text(self, item: DownloadItem | None) -> str:
        if item is None:
            return "Select a library item to review it."
        if item.review_state == "new":
            return "Judge whether this clip is good for the active niche account."
        if item.review_state == "kept":
            return "Kept for this account. You can assign it, open it, or return it to review."
        if item.review_state == "rejected":
            return "Ignored from this library. Return it to review if you want to reconsider it."
        return "Select a library item to review it."

    @staticmethod
    def _download_retry_label(status: str) -> str:
        if status == "downloaded":
            return "Redownload Video"
        return "Retry Download"

    def _selected_item_ids(self) -> list[int]:
        selected_ids: list[int] = []
        seen_ids: set[int] = set()
        for selected_item in self._table.selectedItems():
            item_id = selected_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(item_id, int) or item_id in seen_ids:
                continue
            selected_ids.append(item_id)
            seen_ids.add(item_id)
        return selected_ids

    @staticmethod
    def _source_status_colors(status: str) -> tuple[QColor, QColor]:
        colors = {
            "completed": (QColor("#163325"), QColor("#c7f3d8")),
            "failed": (QColor("#3a1f24"), QColor("#ffd0d0")),
            "running": (QColor("#1d3248"), QColor("#c5dcff")),
            "(idle)": (QColor("#1b2430"), QColor("#d2dbe8")),
        }
        return colors.get(status, (QColor("#111827"), QColor("#d7e0ea")))

    @staticmethod
    def _candidate_state_colors(state: str) -> tuple[QColor, QColor]:
        colors = {
            "candidate": (QColor("#1b2635"), QColor("#b7cbe2")),
            "queued": (QColor("#203246"), QColor("#9fc5f8")),
            "downloaded": (QColor("#11271a"), QColor("#8ee6b1")),
            "ignored": (QColor("#34171b"), QColor("#ff9c9c")),
        }
        return colors.get(state, (QColor("#111827"), QColor("#d7e0ea")))

    @staticmethod
    def _icon(name: str) -> QIcon:
        return QIcon(str(ICON_DIR / f"{name}.svg"))

    @staticmethod
    def _sidebar_icon(name: str) -> QIcon:
        icon_path = ICON_DIR / f"{name}.svg"
        try:
            svg_text = icon_path.read_text(encoding="utf-8")
        except OSError:
            return QIcon(str(icon_path))
        svg_text = svg_text.replace("currentColor", "#ffffff")
        pixmap = QPixmap()
        pixmap.loadFromData(svg_text.encode("utf-8"), "SVG")
        return QIcon(pixmap)

    def _make_processing_page(self) -> QWidget:
        title_label = QLabel("Preprocess")
        title_label.setObjectName("sectionTitle")
        message_label = QLabel(
            "Select a downloaded video, generate drafts, then export the processed clip."
        )
        message_label.setObjectName("metaValue")
        message_label.setWordWrap(True)
        message_label.setVisible(False)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout()
        panel_layout.setContentsMargins(24, 24, 24, 24)
        panel_layout.setSpacing(14)
        panel_layout.addWidget(title_label)
        panel_layout.addWidget(message_label)

        selector_label = QLabel("Videos")
        selector_label.setObjectName("metaLabel")
        self._processing_item_combo = NoScrollComboBox()
        self._processing_item_combo.currentIndexChanged.connect(self._on_processing_item_changed)
        self._processing_item_combo.setVisible(False)

        self._processing_state_filter = NoScrollComboBox()
        self._processing_state_filter.addItem("All", "all")
        self._processing_state_filter.addItem("Needs Processing", "needs")
        self._processing_state_filter.addItem("Processed", "processed")
        self._processing_state_filter.currentIndexChanged.connect(
            self._on_processing_inbox_filter_changed
        )
        self._processing_search_input = QLineEdit()
        self._processing_search_input.setPlaceholderText("Search videos...")
        self._processing_search_input.textChanged.connect(self._on_processing_inbox_filter_changed)

        inbox_header = QHBoxLayout()
        inbox_header.setSpacing(10)
        inbox_header.addWidget(selector_label)
        inbox_header.addStretch(1)
        inbox_header.addWidget(self._processing_state_filter)
        inbox_header.addWidget(self._processing_search_input, stretch=1)
        panel_layout.addLayout(inbox_header)

        self._processing_inbox_table = TableFocusScrollWidget()
        self._processing_inbox_table.setColumnCount(5)
        self._processing_inbox_table.setHorizontalHeaderLabels(
            ["Status", "Source", "Title", "Added", ""]
        )
        self._processing_inbox_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._processing_inbox_table.horizontalHeader().setStretchLastSection(False)
        self._processing_inbox_table.verticalHeader().setVisible(False)
        self._processing_inbox_table.setAlternatingRowColors(True)
        self._processing_inbox_table.setShowGrid(False)
        self._processing_inbox_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._processing_inbox_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._processing_inbox_table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._processing_inbox_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._processing_inbox_table.setWordWrap(False)
        self._processing_inbox_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._processing_inbox_table.setMinimumHeight(220)
        self._processing_inbox_table.setMaximumHeight(320)
        self._processing_inbox_table.setColumnHidden(1, True)
        self._processing_inbox_table.setColumnHidden(3, True)
        self._processing_inbox_table.setColumnHidden(4, True)
        self._processing_inbox_table.itemSelectionChanged.connect(
            self._on_processing_inbox_selection_changed
        )
        panel_layout.addWidget(self._processing_inbox_table)
        panel_layout.addWidget(self._processing_item_combo)

        self._processing_summary_label = QLabel("Select an account workspace to prepare videos.")
        self._processing_summary_label.setObjectName("subtleLabel")
        self._processing_summary_label.setWordWrap(True)
        self._processing_summary_label.setVisible(False)
        panel_layout.addWidget(self._processing_summary_label)

        self._processing_progress_label = QLabel("")
        self._processing_progress_label.setObjectName("subtleLabel")
        self._processing_progress_label.setWordWrap(True)
        panel_layout.addWidget(self._processing_progress_label)

        self._processing_progress_bar = QProgressBar()
        self._processing_progress_bar.setObjectName("thinProgress")
        self._processing_progress_bar.setVisible(False)
        self._processing_progress_bar.setTextVisible(True)
        self._processing_progress_bar.setMinimum(0)
        self._processing_progress_bar.setMaximum(1)
        self._processing_progress_bar.setValue(0)
        panel_layout.addWidget(self._processing_progress_bar)

        preview_panel = QFrame()
        preview_panel.setObjectName("panel")
        preview_layout = QVBoxLayout()
        preview_layout.setContentsMargins(16, 16, 16, 16)
        preview_layout.setSpacing(10)
        preview_header = QHBoxLayout()
        preview_header.setSpacing(10)
        preview_title = QLabel("Preview")
        preview_title.setObjectName("metaLabel")
        self._processing_preview_mode_combo = NoScrollComboBox()
        self._processing_preview_mode_combo.addItem("Original Video", "source")
        self._processing_preview_mode_combo.addItem("Processed Output", "output")
        self._processing_preview_mode_combo.currentIndexChanged.connect(
            self._on_processing_preview_mode_changed
        )
        preview_header.addWidget(preview_title)
        preview_header.addStretch(1)
        preview_header.addWidget(self._processing_preview_mode_combo)
        self._processing_preview_mode_combo.setVisible(False)
        preview_layout.addLayout(preview_header)

        self._processing_video_widget = QLabel("Preview unavailable")
        self._processing_video_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._processing_video_widget.setFixedHeight(520)
        self._processing_video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._processing_video_widget.setObjectName("videoPreview")
        video_row = QHBoxLayout()
        video_row.setContentsMargins(12, 0, 12, 0)
        video_row.addWidget(self._processing_video_widget)
        preview_layout.addLayout(video_row)

        preview_action_row = QHBoxLayout()
        preview_action_row.setSpacing(10)
        self._processing_preview_back_button = QPushButton("-1s")
        self._processing_preview_back_button.setObjectName("ghostButton")
        self._processing_preview_back_button.clicked.connect(
            lambda: self._shift_processing_preview(-1000)
        )
        preview_action_row.addWidget(self._processing_preview_back_button)
        self._processing_preview_back_large_button = QPushButton("-5s")
        self._processing_preview_back_large_button.setObjectName("ghostButton")
        self._processing_preview_back_large_button.clicked.connect(
            lambda: self._shift_processing_preview(-5000)
        )
        preview_action_row.addWidget(self._processing_preview_back_large_button)
        self._processing_toggle_preview_button = QPushButton("Play Full Video")
        self._processing_toggle_preview_button.setObjectName("ghostButton")
        self._processing_toggle_preview_button.clicked.connect(
            self._on_toggle_processing_preview_clicked
        )
        preview_action_row.addWidget(self._processing_toggle_preview_button)
        self._processing_preview_position_slider = QSlider(Qt.Orientation.Horizontal)
        self._processing_preview_position_slider.setRange(0, 0)
        self._processing_preview_position_slider.sliderMoved.connect(
            self._on_processing_preview_seek
        )
        preview_action_row.addWidget(self._processing_preview_position_slider, stretch=1)
        self._processing_preview_time_label = QLabel("00:00 / 00:00")
        self._processing_preview_time_label.setObjectName("subtleLabel")
        preview_action_row.addWidget(self._processing_preview_time_label)
        self._processing_preview_forward_large_button = QPushButton("+5s")
        self._processing_preview_forward_large_button.setObjectName("ghostButton")
        self._processing_preview_forward_large_button.clicked.connect(
            lambda: self._shift_processing_preview(5000)
        )
        preview_action_row.addWidget(self._processing_preview_forward_large_button)
        self._processing_preview_forward_button = QPushButton("+1s")
        self._processing_preview_forward_button.setObjectName("ghostButton")
        self._processing_preview_forward_button.clicked.connect(
            lambda: self._shift_processing_preview(1000)
        )
        preview_action_row.addWidget(self._processing_preview_forward_button)
        preview_action_row.addStretch(1)
        preview_layout.addLayout(preview_action_row)

        self._processing_preview_meta_label = QLabel(
            "Select a downloaded video to preview it here."
        )
        self._processing_preview_meta_label.setObjectName("subtleLabel")
        self._processing_preview_meta_label.setWordWrap(True)
        self._processing_preview_meta_label.setVisible(False)
        preview_layout.addWidget(self._processing_preview_meta_label)

        preview_panel.setLayout(preview_layout)
        panel_layout.addWidget(preview_panel)

        text_panel = QFrame()
        text_panel.setObjectName("panel")
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(16, 16, 16, 16)
        text_layout.setSpacing(10)

        text_header = QHBoxLayout()
        text_header.setSpacing(10)
        text_title = QLabel("Drafts")
        text_title.setObjectName("metaLabel")
        self._processing_loading_badge = QLabel("")
        self._processing_loading_badge.setObjectName("statusLabel")
        self._processing_loading_badge.setVisible(False)
        self._processing_generate_drafts_button = QPushButton("Generate")
        self._processing_generate_drafts_button.setObjectName("ghostButton")
        self._processing_generate_drafts_button.clicked.connect(
            self._on_generate_text_drafts_clicked
        )
        self._processing_copy_chat_prompt_button = QPushButton("Copy Chat Prompt")
        self._processing_copy_chat_prompt_button.setObjectName("ghostButton")
        self._processing_copy_chat_prompt_button.setToolTip(
            "Copy a chat-ready prompt with the selected local video path and current niche context."
        )
        self._processing_copy_chat_prompt_button.clicked.connect(
            self._on_copy_generation_chat_prompt_clicked
        )
        self._processing_save_drafts_button = QPushButton("Save")
        self._processing_save_drafts_button.clicked.connect(self._on_save_text_drafts_clicked)
        text_header.addWidget(text_title)
        text_header.addStretch(1)
        text_header.addWidget(self._processing_loading_badge)
        text_header.addWidget(self._processing_generate_drafts_button)
        text_header.addWidget(self._processing_copy_chat_prompt_button)
        text_header.addWidget(self._processing_save_drafts_button)
        text_layout.addLayout(text_header)

        context_label = QLabel("Generation Context")
        context_label.setObjectName("metaLabel")
        self._processing_transcript_input = QTextEdit()
        self._processing_transcript_input.setObjectName("smartOptionEdit")
        self._processing_transcript_input.setPlaceholderText(
            "Selected video context and transcript signals will appear here..."
        )
        self._processing_transcript_input.setReadOnly(True)
        self._processing_transcript_input.setMinimumHeight(150)
        context_label.setVisible(False)
        self._processing_transcript_input.setVisible(False)

        clip_premise_label = QLabel("Clip Premise")
        clip_premise_label.setObjectName("metaLabel")
        self._processing_clip_premise_input = QTextEdit()
        self._processing_clip_premise_input.setObjectName("smartOptionEdit")
        self._processing_clip_premise_input.setPlaceholderText(
            "Optional: explain the joke, context, or anomaly before generating..."
        )
        self._processing_clip_premise_input.setMinimumHeight(72)
        self._processing_clip_premise_input.textChanged.connect(
            self._on_processing_clip_premise_changed
        )
        text_layout.addWidget(clip_premise_label)
        text_layout.addWidget(self._processing_clip_premise_input)

        caption_style_row = QHBoxLayout()
        caption_style_row.setSpacing(10)
        caption_style_label = QLabel("Caption Style")
        caption_style_label.setObjectName("metaLabel")
        self._processing_caption_style_combo = NoScrollComboBox()
        self._processing_caption_style_combo.addItem("(Meme) Context / info", "contextual_info")
        self._processing_caption_style_combo.addItem("(Meme) Friend Group", "meme_friend_group")
        self._processing_caption_style_combo.addItem(
            "(Meme) Bro / Main Character", "meme_bro_main_character"
        )
        self._processing_caption_style_combo.addItem(
            "(Meme) Chronically Online", "meme_chronically_online"
        )
        self._processing_caption_style_combo.addItem(
            "(Meme) Reaction / Situation", "meme_reaction_situation"
        )
        self._processing_caption_style_combo.addItem("(Meme) Daily Cope", "meme_daily_cope")
        self._processing_caption_style_combo.addItem(
            "(Movie) Cinema Atmospheric", "cinema_hook"
        )
        self._processing_caption_style_combo.addItem(
            "(History) Past Moments", "history_lost_archive"
        )
        self._processing_caption_style_combo.setMinimumWidth(240)
        caption_style_row.addWidget(caption_style_label)
        caption_style_row.addWidget(self._processing_caption_style_combo)
        caption_style_row.addStretch(1)
        text_layout.addLayout(caption_style_row)

        # Title Style is decoupled from Caption Style so users can mix any
        # title format (e.g. the IGHT "When ...:" setup-punchline meme hook)
        # with any caption template. "Auto" preserves the previous behavior
        # of deriving the title rules from the caption_style selection.
        title_style_row = QHBoxLayout()
        title_style_row.setSpacing(10)
        title_style_label = QLabel("Title Style")
        title_style_label.setObjectName("metaLabel")
        self._processing_prompt_title_style_combo = NoScrollComboBox()
        # "Auto" -> empty string sentinel; getter normalizes to None so
        # smart_drafts falls back to caption-style-derived title rules.
        self._processing_prompt_title_style_combo.addItem("Auto (match caption style)", "")
        # The IGHT setup-punchline pattern: 'When ...:' / 'POV: ...:' /
        # 'Me ...:' framing where the video footage delivers the punchline.
        self._processing_prompt_title_style_combo.addItem(
            "(Meme) Setup -> Punchline", "meme_setup_punchline"
        )
        # Keep only the niche styles we actually use day to day.
        self._processing_prompt_title_style_combo.addItem(
            "(Meme) Relatable Hook", "meme_relatable"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(Meme) Friend Group", "meme_friend_group"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(Meme) Bro / Main Character", "meme_bro_main_character"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(Meme) Chronically Online", "meme_chronically_online"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(Meme) Reaction / Situation", "meme_reaction_situation"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(Meme) Daily Cope", "meme_daily_cope"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(Movie) Cinema Atmospheric", "cinema_hook"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(Movie) Cinema Bold Keywords", "cinema_bold_keywords"
        )
        self._processing_prompt_title_style_combo.addItem(
            "(History) Past Moments", "history_lost_archive"
        )
        self._processing_prompt_title_style_combo.setMinimumWidth(240)
        title_style_row.addWidget(title_style_label)
        title_style_row.addWidget(self._processing_prompt_title_style_combo)
        title_style_row.addStretch(1)
        text_layout.addLayout(title_style_row)

        title_draft_label = QLabel("Title")
        title_draft_label.setObjectName("metaLabel")
        self._processing_title_draft_input = MultilineTitleEdit(min_height=68, max_height=118)
        self._processing_title_draft_input.setObjectName("smartOptionEdit")
        self._processing_title_draft_input.setPlaceholderText("Generated or edited title...")
        self._processing_title_draft_input.textChanged.connect(
            self._mark_processing_text_dirty
        )
        text_layout.addWidget(title_draft_label)
        text_layout.addWidget(self._processing_title_draft_input)

        caption_draft_label = QLabel("Caption")
        caption_draft_label.setObjectName("metaLabel")
        self._processing_caption_draft_input = MultilineCaptionEdit(min_height=120)
        self._processing_caption_draft_input.setPlaceholderText("Generated or edited caption...")
        self._processing_caption_draft_input.textChanged.connect(
            self._mark_processing_text_dirty
        )
        text_layout.addWidget(caption_draft_label)
        text_layout.addWidget(self._processing_caption_draft_input)

        self._processing_draft_status_label = QLabel(
            "Generate visual-first title and caption drafts from the selected downloaded video."
        )
        self._processing_draft_status_label.setObjectName("subtleLabel")
        self._processing_draft_status_label.setWordWrap(True)
        self._processing_draft_status_label.setVisible(False)
        text_layout.addWidget(self._processing_draft_status_label)

        self._processing_smart_summary_label = QLabel(
            "Smart draft summary will appear here after Groq generation."
        )
        self._processing_smart_summary_label.setObjectName("metaValue")
        self._processing_smart_summary_label.setWordWrap(True)
        self._processing_smart_summary_label.setVisible(False)

        self._processing_smart_recommendation_label = QLabel(
            "Recommended draft pick will appear here after generation."
        )
        self._processing_smart_recommendation_label.setObjectName("metaValue")
        self._processing_smart_recommendation_label.setWordWrap(True)
        self._processing_smart_recommendation_label.setVisible(False)
        text_layout.addWidget(self._processing_smart_recommendation_label)

        self._processing_eval_provider_label = QLabel(
            "Provider metadata will appear here after smart generation."
        )
        self._processing_eval_provider_label.setObjectName("subtleLabel")
        self._processing_eval_provider_label.setWordWrap(True)
        self._processing_eval_provider_label.setVisible(False)

        self._processing_usage_label = QLabel(
            "Usage budget will appear here after smart generation is configured."
        )
        self._processing_usage_label.setObjectName("subtleLabel")
        self._processing_usage_label.setWordWrap(True)
        self._processing_usage_label.setVisible(False)
        text_layout.addWidget(self._processing_usage_label)

        self._processing_debug_toggle = QPushButton("Show Generation Details")
        self._processing_debug_toggle.setObjectName("ghostButton")
        self._processing_debug_toggle.setCheckable(True)
        self._processing_debug_toggle.toggled.connect(self._on_processing_debug_toggled)
        self._processing_debug_toggle.setVisible(False)

        self._processing_debug_panel = QFrame()
        self._processing_debug_panel.setObjectName("panel")
        processing_debug_layout = QVBoxLayout()
        processing_debug_layout.setContentsMargins(12, 12, 12, 12)
        processing_debug_layout.setSpacing(8)
        eval_title = QLabel("Generation Details")
        eval_title.setObjectName("metaLabel")
        processing_debug_layout.addWidget(eval_title)

        self._processing_eval_meta_input = QTextEdit()
        self._processing_eval_meta_input.setObjectName("smartOptionEdit")
        self._processing_eval_meta_input.setReadOnly(True)
        self._processing_eval_meta_input.setPlaceholderText("Compact generation metadata...")
        self._processing_eval_meta_input.setMinimumHeight(68)
        processing_debug_layout.addWidget(self._processing_eval_meta_input)

        self._processing_eval_vision_input = QTextEdit()
        self._processing_eval_vision_input.setObjectName("smartOptionEdit")
        self._processing_eval_vision_input.setReadOnly(True)
        self._processing_eval_vision_input.setPlaceholderText(
            "Structured vision extraction JSON..."
        )
        self._processing_eval_vision_input.setMinimumHeight(96)
        processing_debug_layout.addWidget(self._processing_eval_vision_input)
        self._processing_debug_panel.setLayout(processing_debug_layout)
        self._processing_debug_panel.setVisible(False)
        text_layout.addWidget(self._processing_debug_panel)

        self._processing_smart_cards_status_label = QLabel(
            "Pick one generated option card to apply it here."
        )
        self._processing_smart_cards_status_label.setObjectName("subtleLabel")
        self._processing_smart_cards_status_label.setWordWrap(True)
        self._processing_smart_cards_status_label.setVisible(False)
        text_layout.addWidget(self._processing_smart_cards_status_label)

        paste_draft_row = QHBoxLayout()
        paste_draft_row.setSpacing(10)
        self._processing_paste_draft_button = QPushButton("Paste Draft From Clipboard")
        self._processing_paste_draft_button.setObjectName("ghostButton")
        self._processing_paste_draft_button.setIcon(self._icon("refresh"))
        self._processing_paste_draft_button.setIconSize(QSize(16, 16))
        self._processing_paste_draft_button.clicked.connect(
            self._on_paste_smart_draft_clicked
        )
        paste_draft_hint = QLabel(
            "Copy a generated draft (Title/Caption Options + Recommended Pick), "
            "then paste it into the option cards below."
        )
        paste_draft_hint.setObjectName("subtleLabel")
        paste_draft_hint.setWordWrap(True)
        paste_draft_row.addWidget(self._processing_paste_draft_button)
        paste_draft_row.addWidget(paste_draft_hint, stretch=1)
        text_layout.addLayout(paste_draft_row)

        self._processing_smart_option_buttons: list[QPushButton] = []
        self._processing_smart_option_title_inputs: list[MultilineTitleEdit] = []
        self._processing_smart_option_caption_inputs: list[QTextEdit] = []
        self._processing_smart_option_note_labels: list[QLabel] = []
        self._processing_smart_option_pairs: list[tuple[str | None, str | None]] = []
        smart_cards_layout = QGridLayout()
        smart_cards_layout.setHorizontalSpacing(10)
        smart_cards_layout.setVerticalSpacing(10)
        for index in range(SMART_DRAFT_OPTION_COUNT):
            card_panel = QFrame()
            card_panel.setObjectName("panel")
            card_panel.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            card_layout = QVBoxLayout()
            card_layout.setContentsMargins(14, 14, 14, 14)
            card_layout.setSpacing(8)
            card_label = QLabel(f"Option {index + 1}")
            card_label.setObjectName("metaLabel")
            card_layout.addWidget(card_label)

            option_note_label = QLabel("")
            option_note_label.setObjectName("subtleLabel")
            option_note_label.setWordWrap(True)
            option_note_label.setVisible(False)
            self._processing_smart_option_note_labels.append(option_note_label)
            card_layout.addWidget(option_note_label)

            title_input = MultilineTitleEdit()
            title_input.setObjectName("smartOptionEdit")
            title_input.setPlaceholderText("Generated title option...")
            title_input.textChanged.connect(self._mark_processing_text_dirty)
            self._processing_smart_option_title_inputs.append(title_input)
            card_layout.addWidget(title_input)

            caption_input = MultilineCaptionEdit(
                min_height=88,
                size_policy=(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Expanding,
                ),
            )
            caption_input.setObjectName("smartOptionEdit")
            caption_input.setPlaceholderText("Generated caption option...")
            caption_input.textChanged.connect(self._mark_processing_text_dirty)
            self._processing_smart_option_caption_inputs.append(caption_input)
            card_layout.addWidget(caption_input)

            apply_button = QPushButton("Apply")
            apply_button.setObjectName("smartOptionCard")
            apply_button.setCheckable(True)
            apply_button.clicked.connect(
                lambda _checked=False, option_index=index: self._on_processing_smart_option_clicked(
                    option_index
                )
            )
            self._processing_smart_option_buttons.append(apply_button)
            card_layout.addWidget(apply_button)
            card_panel.setLayout(card_layout)
            smart_cards_layout.addWidget(card_panel, index // 2, index % 2)
        text_layout.addLayout(smart_cards_layout)

        self._processing_style_panel = QFrame()
        style_panel = self._processing_style_panel
        style_panel.setObjectName("panel")
        style_layout = QGridLayout()
        style_layout.setContentsMargins(16, 16, 16, 16)
        style_layout.setHorizontalSpacing(12)
        style_layout.setVerticalSpacing(10)

        template_label = QLabel("Template")
        template_label.setObjectName("metaLabel")
        self._processing_template_combo = NoScrollComboBox()
        for key, config in PROCESSING_TEMPLATES.items():
            self._processing_template_combo.addItem(str(config["label"]), key)
        self._processing_template_combo.currentIndexChanged.connect(
            self._on_processing_template_changed
        )
        style_layout.addWidget(template_label, 0, 0)
        style_layout.addWidget(self._processing_template_combo, 0, 1)

        title_style_label = QLabel("Title Style")
        title_style_label.setObjectName("metaLabel")
        self._processing_title_style_combo = NoScrollComboBox()
        for key, config in TITLE_STYLE_PRESETS.items():
            self._processing_title_style_combo.addItem(str(config["label"]), key)
        self._processing_title_style_combo.currentIndexChanged.connect(
            self._on_title_style_preset_changed
        )
        style_layout.addWidget(title_style_label, 1, 0)
        style_layout.addWidget(self._processing_title_style_combo, 1, 1)

        title_size_label = QLabel("Title Size")
        title_size_label.setObjectName("metaLabel")
        self._processing_title_font_size = NoScrollSpinBox()
        self._processing_title_font_size.setRange(18, 144)
        style_layout.addWidget(title_size_label, 1, 2)
        style_layout.addWidget(self._processing_title_font_size, 1, 3)

        title_font_label = QLabel("Title Font")
        title_font_label.setObjectName("metaLabel")
        self._processing_title_font_combo = NoScrollComboBox()
        for label, key in TITLE_FONT_CHOICES:
            self._processing_title_font_combo.addItem(label, key)
        style_layout.addWidget(title_font_label, 2, 0)
        style_layout.addWidget(self._processing_title_font_combo, 2, 1)

        title_color_label = QLabel("Title Color")
        title_color_label.setObjectName("metaLabel")
        self._processing_title_color_input = QLineEdit()
        self._processing_title_color_input.setPlaceholderText("#FFFFFF")
        style_layout.addWidget(title_color_label, 2, 2)
        style_layout.addWidget(self._processing_title_color_input, 2, 3)

        title_background_label = QLabel("Title Background")
        title_background_label.setObjectName("metaLabel")
        self._processing_title_background_combo = NoScrollComboBox()
        self._processing_title_background_combo.addItem("None", "none")
        self._processing_title_background_combo.addItem("Dark Box", "dark")
        self._processing_title_background_combo.addItem("Light Box", "light")
        style_layout.addWidget(title_background_label, 3, 0)
        style_layout.addWidget(self._processing_title_background_combo, 3, 1)
        for manual_title_widget in (
            title_style_label,
            self._processing_title_style_combo,
            title_size_label,
            self._processing_title_font_size,
            title_font_label,
            self._processing_title_font_combo,
            title_color_label,
            self._processing_title_color_input,
            title_background_label,
            self._processing_title_background_combo,
        ):
            manual_title_widget.setVisible(False)

        self._processing_style_status_label = QLabel(
            "Template controls the rendered title style. Edit the title text and caption below."
        )
        self._processing_style_status_label.setObjectName("subtleLabel")
        self._processing_style_status_label.setWordWrap(True)
        style_layout.addWidget(self._processing_style_status_label, 4, 0, 1, 4)
        style_panel.setLayout(style_layout)
        # Keep this panel visible for Template only. The hidden title controls
        # still store template-applied values for save/export compatibility.
        text_panel.setLayout(text_layout)
        panel_layout.addWidget(text_panel)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        title_layout_label = QLabel("Title Layout")
        title_layout_label.setObjectName("metaLabel")
        self._processing_title_layout_combo = NoScrollComboBox()
        for label, key in TITLE_LAYOUT_CHOICES:
            self._processing_title_layout_combo.addItem(label, key)
        self._processing_title_layout_combo.currentIndexChanged.connect(
            self._on_processing_title_layout_changed
        )
        action_row.addWidget(title_layout_label)
        action_row.addWidget(self._processing_title_layout_combo)
        title_layout_label.setVisible(False)
        self._processing_title_layout_combo.setVisible(False)
        top_crop_label = QLabel("Top Crop")
        top_crop_label.setObjectName("metaLabel")
        top_crop_label.setVisible(False)
        self._processing_top_crop_spin = NoScrollSpinBox()
        self._processing_top_crop_spin.setRange(0, 2000)
        self._processing_top_crop_spin.setSuffix(" px")
        self._processing_top_crop_spin.valueChanged.connect(self._on_processing_manual_crop_changed)
        self._processing_top_crop_spin.setVisible(False)
        bottom_crop_label = QLabel("Bottom Crop")
        bottom_crop_label.setObjectName("metaLabel")
        bottom_crop_label.setVisible(False)
        self._processing_bottom_crop_spin = NoScrollSpinBox()
        self._processing_bottom_crop_spin.setRange(0, 2000)
        self._processing_bottom_crop_spin.setSuffix(" px")
        self._processing_bottom_crop_spin.valueChanged.connect(
            self._on_processing_manual_crop_changed
        )
        self._processing_bottom_crop_spin.setVisible(False)
        action_row.addWidget(top_crop_label)
        action_row.addWidget(self._processing_top_crop_spin)
        action_row.addWidget(bottom_crop_label)
        action_row.addWidget(self._processing_bottom_crop_spin)
        self._processing_alter_audio_checkbox = QCheckBox("Alter audio")
        self._processing_alter_audio_checkbox.setToolTip(
            "Apply subtle, randomized audio changes on export so the same clip "
            "posted to different accounts is a non-identical file. Leave off to "
            "keep the original audio unchanged (use this for clips where the "
            "audio is the point)."
        )
        action_row.addWidget(self._processing_alter_audio_checkbox)
        self._processing_export_button = QPushButton("Export")
        self._processing_export_button.clicked.connect(self._on_process_video_clicked)
        self._processing_open_processed_button = QPushButton("Open Folder")
        self._processing_open_processed_button.setObjectName("ghostButton")
        self._processing_open_processed_button.clicked.connect(
            self._on_open_processed_folder_clicked
        )
        action_row.addWidget(self._processing_export_button)
        action_row.addStretch(1)
        action_row.addWidget(self._processing_open_processed_button)
        panel_layout.addLayout(action_row)

        latest_output_row = QHBoxLayout()
        latest_output_row.setSpacing(10)
        latest_output_label = QLabel("Latest Output")
        latest_output_label.setObjectName("metaLabel")
        self._processing_latest_output_label = QLabel("No processed output yet in this session.")
        self._processing_latest_output_label.setObjectName("metaValue")
        self._processing_latest_output_label.setWordWrap(True)
        self._processing_open_latest_output_button = QPushButton("Open Video")
        self._processing_open_latest_output_button.setObjectName("ghostButton")
        self._processing_open_latest_output_button.clicked.connect(
            self._on_open_latest_processed_output_clicked
        )
        self._processing_add_to_schedule_button = QPushButton("Add to Schedule")
        self._processing_add_to_schedule_button.setObjectName("ghostButton")
        self._processing_add_to_schedule_button.clicked.connect(
            self._on_add_processed_to_schedule_clicked
        )
        latest_output_row.addWidget(latest_output_label)
        latest_output_row.addWidget(self._processing_latest_output_label, stretch=1)
        latest_output_row.addWidget(self._processing_open_latest_output_button)
        latest_output_row.addWidget(self._processing_add_to_schedule_button)
        self._processing_open_latest_output_button.setVisible(False)
        self._processing_add_to_schedule_button.setVisible(False)
        panel_layout.addLayout(latest_output_row)

        self._processing_suggestion_label = QLabel(
            "Processing now auto-detects the crop and renders the applied title onto the video. "
            "You no longer need to tune crop margins manually."
        )
        self._processing_suggestion_label.setObjectName("subtleLabel")
        self._processing_suggestion_label.setWordWrap(True)
        self._processing_suggestion_label.setVisible(False)
        panel_layout.addWidget(self._processing_suggestion_label)
        # NOTE: removed the matching setVisible(False) here; see the comment
        # next to style_panel.setLayout above.
        panel_layout.addWidget(self._processing_style_panel)

        # Per-account preference persistence: any change to one of these
        # widgets snapshots the full Processing state and writes it to the
        # current account. The reverse path (loading on account switch)
        # runs from _set_current_account_from_combo.
        save = self._save_processing_preferences_for_current_account
        self._processing_template_combo.currentIndexChanged.connect(save)
        self._processing_caption_style_combo.currentIndexChanged.connect(save)
        self._processing_prompt_title_style_combo.currentIndexChanged.connect(save)
        self._processing_title_style_combo.currentIndexChanged.connect(save)
        self._processing_title_font_combo.currentIndexChanged.connect(save)
        self._processing_title_font_size.valueChanged.connect(save)
        self._processing_title_color_input.editingFinished.connect(save)
        self._processing_title_background_combo.currentIndexChanged.connect(save)
        self._processing_title_layout_combo.currentIndexChanged.connect(save)
        self._processing_alter_audio_checkbox.stateChanged.connect(save)

        panel_layout.addStretch(1)
        panel.setLayout(panel_layout)

        self._processing_preview_timer = QTimer(self)
        self._processing_preview_timer.setSingleShot(True)
        self._processing_preview_timer.setInterval(33)
        self._processing_preview_timer.timeout.connect(self._advance_processing_preview)

        page = QWidget()
        page_layout = QVBoxLayout()
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(panel)
        page.setLayout(page_layout)
        page.setMinimumHeight(900)
        return page

    def _make_accounts_page(self) -> QWidget:
        title_label = QLabel("Accounts & Runtime")
        title_label.setObjectName("sectionTitle")
        message_label = QLabel(
            "Manage scraping strategy profiles from the account manager panel. "
            "This page shows the current runtime paths and lets you export a local backup snapshot."
        )
        message_label.setObjectName("metaValue")
        message_label.setWordWrap(True)

        runtime_panel = QFrame()
        runtime_panel.setObjectName("panel")
        runtime_layout = QVBoxLayout()
        runtime_layout.setContentsMargins(24, 24, 24, 24)
        runtime_layout.setSpacing(12)
        runtime_layout.addWidget(title_label)
        runtime_layout.addWidget(message_label)

        runtime_grid = QGridLayout()
        runtime_grid.setHorizontalSpacing(12)
        runtime_grid.setVerticalSpacing(10)
        self._runtime_fields: dict[str, QLabel] = {}
        runtime_keys = [
            ("Data directory", "data_dir"),
            ("Database", "db_path"),
            ("Downloads", "downloads_dir"),
            ("Processed", "processed_dir"),
            ("Logs", "logs_dir"),
            ("Backups", "backups_dir"),
        ]
        for row, (label, key) in enumerate(runtime_keys):
            meta_label = QLabel(label)
            meta_label.setObjectName("metaLabel")
            value_label = QLabel("")
            value_label.setObjectName("metaValue")
            value_label.setWordWrap(True)
            runtime_grid.addWidget(meta_label, row, 0)
            runtime_grid.addWidget(value_label, row, 1)
            self._runtime_fields[key] = value_label
        runtime_layout.addLayout(runtime_grid)

        runtime_actions = QHBoxLayout()
        runtime_actions.setSpacing(10)
        self._open_data_folder_button = QPushButton("Open Data Folder")
        self._open_data_folder_button.clicked.connect(self._on_open_data_folder_clicked)
        self._export_backup_button = QPushButton("Create Backup Zip")
        self._export_backup_button.clicked.connect(self._on_export_backup_clicked)
        self._restore_backup_input = QLineEdit()
        self._restore_backup_input.setPlaceholderText("Backup zip path...")
        self._use_latest_backup_button = QPushButton("Use Latest Backup")
        self._use_latest_backup_button.clicked.connect(self._on_use_latest_backup_clicked)
        self._restore_backup_button = QPushButton("Restore Backup Zip")
        self._restore_backup_button.clicked.connect(self._on_restore_backup_clicked)
        runtime_actions.addWidget(self._open_data_folder_button)
        runtime_actions.addWidget(self._export_backup_button)
        runtime_actions.addStretch(1)
        runtime_layout.addLayout(runtime_actions)

        restore_row = QHBoxLayout()
        restore_row.setSpacing(10)
        restore_row.addWidget(self._restore_backup_input, stretch=1)
        restore_row.addWidget(self._use_latest_backup_button)
        restore_row.addWidget(self._restore_backup_button)
        runtime_layout.addLayout(restore_row)

        self._backup_summary_label = QLabel("No backup created in this session.")
        self._backup_summary_label.setObjectName("subtleLabel")
        self._backup_summary_label.setWordWrap(True)
        runtime_layout.addWidget(self._backup_summary_label)
        runtime_layout.addStretch(1)
        runtime_panel.setLayout(runtime_layout)

        page = QWidget()
        page_layout = QVBoxLayout()
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(runtime_panel)
        page.setLayout(page_layout)
        return page

    def _make_session_health_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._make_account_health_panel())
        page.setLayout(layout)
        return page

    # --- Pool & Distribute page -----------------------------------------

    _POOLING_NICHES = ("history", "movie")

    def _make_pooling_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._make_pooling_panel())
        page.setLayout(layout)
        return page

    def _make_pooling_panel(self) -> QFrame:
        self._pooling_niche_labels: dict[str, QLabel] = {}
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout()
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Pool & Distribute")
        title.setObjectName("sectionTitle")
        subtitle = QLabel(
            "Accept downloaded Instagram clips into a niche pool, then distribute "
            "them across that niche's accounts — one clip per account, evenly, "
            "with history and movie kept strictly separate."
        )
        subtitle.setObjectName("metaValue")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self._pooling_summary_label = QLabel("")
        self._pooling_summary_label.setObjectName("metaValue")
        self._pooling_summary_label.setWordWrap(True)
        layout.addWidget(self._pooling_summary_label)

        self._pooling_table = TableFocusScrollWidget()
        self._pooling_table.setObjectName("downloadQueueTable")
        self._pooling_table.setColumnCount(3)
        self._pooling_table.setHorizontalHeaderLabels(["Account", "Niche", "Assigned clips"])
        pool_header = self._pooling_table.horizontalHeader()
        pool_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        pool_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        pool_header.setStretchLastSection(True)
        self._pooling_table.verticalHeader().setVisible(False)
        self._pooling_table.setMinimumHeight(150)
        self._pooling_table.setMaximumHeight(320)
        layout.addWidget(self._pooling_table)

        prep_row = QHBoxLayout()
        prep_row.setSpacing(10)
        backfill_btn = QPushButton("Register Downloaded Clips")
        backfill_btn.setObjectName("downloadToolbarButton")
        backfill_btn.setToolTip(
            "Register already-downloaded Instagram clips into the media library. "
            "Run once for clips downloaded before pooling existed."
        )
        backfill_btn.clicked.connect(self._on_pool_backfill_clicked)
        pool_refresh_btn = QPushButton("Refresh")
        pool_refresh_btn.setObjectName("downloadToolbarButton")
        pool_refresh_btn.clicked.connect(self._refresh_pooling_page)
        prep_row.addWidget(backfill_btn)
        prep_row.addWidget(pool_refresh_btn)
        prep_row.addStretch(1)
        layout.addLayout(prep_row)

        for niche in self._POOLING_NICHES:
            layout.addWidget(self._make_pooling_niche_row(niche))

        self._pooling_status_label = QLabel("")
        self._pooling_status_label.setObjectName("subtleLabel")
        self._pooling_status_label.setWordWrap(True)
        layout.addWidget(self._pooling_status_label)
        layout.addStretch(1)
        panel.setLayout(layout)
        return panel

    def _make_pooling_niche_row(self, niche: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("panel")
        row = QHBoxLayout()
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        label = QLabel("")
        label.setObjectName("metaValue")
        self._pooling_niche_labels[niche] = label
        accept_btn = QPushButton(f"Accept downloaded → {niche.title()} pool")
        accept_btn.setObjectName("downloadToolbarButton")
        accept_btn.clicked.connect(lambda _=False, n=niche: self._on_pool_accept_clicked(n))
        distribute_btn = QPushButton(f"Distribute {niche.title()}")
        distribute_btn.setObjectName("downloadToolbarButton")
        distribute_btn.clicked.connect(lambda _=False, n=niche: self._on_pool_distribute_clicked(n))
        row.addWidget(label, stretch=1)
        row.addWidget(accept_btn)
        row.addWidget(distribute_btn)
        frame.setLayout(row)
        return frame

    def _downloaded_instagram_items(self, session) -> list[DownloadItem]:  # noqa: ANN001
        items = (
            session.query(DownloadItem)
            .filter(DownloadItem.status == "downloaded")
            .filter(DownloadItem.file_path.isnot(None))
            .all()
        )
        return [i for i in items if instagram_shortcode_from_url(i.source_url) is not None]

    @staticmethod
    def _register_item_asset(session, item: DownloadItem):  # noqa: ANN001, ANN205
        """Idempotently register/mark a DownloadItem's original in the pantry."""
        asset, created = find_or_register_media_asset(
            session,
            source_url=item.source_url,
            shortcode=item.video_id,
            platform="instagram",
        )
        if asset.download_status != "downloaded" and item.file_path:
            size: int | None = None
            try:
                size = Path(item.file_path).stat().st_size
            except OSError:
                size = None
            mark_media_asset_downloaded(
                asset, original_download_path=item.file_path, file_size_bytes=size
            )
        return asset, created

    def _on_pool_backfill_clicked(self) -> None:
        registered = 0
        with get_session() as session:
            for item in self._downloaded_instagram_items(session):
                _asset, created = self._register_item_asset(session, item)
                if created:
                    registered += 1
            session.commit()
        self._notify(
            f"Registered {registered} downloaded clip(s) into the media library.",
            Tone.SUCCESS,
        )
        self._refresh_pooling_page()

    def _on_pool_accept_clicked(self, niche: str) -> None:
        accepted = 0
        skipped = 0
        with get_session() as session:
            for item in self._downloaded_instagram_items(session):
                asset, _created = self._register_item_asset(session, item)
                pools = (
                    session.query(PoolItem).filter(PoolItem.media_asset_id == asset.id).all()
                )
                # Already in this pool, or in a different niche pool -> skip
                # (cross-niche moves require an explicit override, not a bulk click).
                if pools:
                    skipped += 1
                    continue
                accept_into_pool(
                    session, media_asset=asset, niche=niche, accepted_reason="pool page"
                )
                accepted += 1
            size_now = pool_size(session, niche)
            session.commit()
        self._notify(
            f"Accepted {accepted} clip(s) into the {niche} pool "
            f"({skipped} already pooled). {niche.title()} pool size: {size_now}.",
            Tone.SUCCESS,
        )
        self._refresh_pooling_page()

    def _on_pool_distribute_clicked(self, niche: str) -> None:
        with get_session() as session:
            account_count = (
                session.query(Account).filter(Account.niche == niche).count()
            )
            if account_count == 0:
                self._notify(
                    f"No {niche} accounts yet. Set an account's niche to '{niche}' "
                    "in account settings first.",
                    Tone.WARNING,
                )
                return
            created_count = len(distribute_niche(session, niche))
            session.commit()
            counts = assignment_counts_by_account(session, niche)
            names = {
                a.id: a.name
                for a in session.query(Account).filter(Account.niche == niche).all()
            }
        if created_count == 0:
            self._notify(
                f"Nothing new to distribute for {niche} "
                "(pool empty, or every clip is already assigned).",
                Tone.WARNING,
            )
            self._refresh_pooling_page()
            return
        summary = ", ".join(
            f"{names.get(account_id, account_id)}: {count}"
            for account_id, count in sorted(counts.items())
        )
        self._notify(
            f"Distributed {created_count} clip(s) across {niche} accounts.", Tone.SUCCESS
        )
        self._pooling_status_label.setText(f"Latest {niche} distribution → {summary}")
        self._refresh_pooling_page()

    def _refresh_pooling_page(self) -> None:
        if not hasattr(self, "_pooling_table"):
            return
        with get_session() as session:
            accounts = [
                (a.id, a.name, a.niche)
                for a in session.query(Account).order_by(Account.name.asc()).all()
            ]
            pool_sizes = {n: pool_size(session, n) for n in self._POOLING_NICHES}
            counts = {n: assignment_counts_by_account(session, n) for n in self._POOLING_NICHES}
            dl_items = self._downloaded_instagram_items(session)
            dl_count = len(dl_items)
            registered = sum(
                1
                for i in dl_items
                if find_media_asset(session, source_url=i.source_url) is not None
            )

        self._pooling_table.blockSignals(True)
        self._pooling_table.setRowCount(0)
        for account_id, name, niche in accounts:
            assigned = counts.get(niche or "", {}).get(account_id, 0)
            row = self._pooling_table.rowCount()
            self._pooling_table.insertRow(row)
            for col, value in enumerate([name or "(unnamed)", niche or "—", str(assigned)]):
                cell = QTableWidgetItem(value)
                cell.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if col == 2:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._pooling_table.setItem(row, col, cell)
        self._pooling_table.resizeRowsToContents()
        self._pooling_table.blockSignals(False)

        for niche in self._POOLING_NICHES:
            label = self._pooling_niche_labels.get(niche)
            if label is not None:
                label.setText(f"{niche.title()} pool: {pool_sizes[niche]} accepted clip(s)")
        self._pooling_summary_label.setText(
            f"{dl_count} downloaded Instagram clip(s), {registered} in the media library.  "
            f"History pool {pool_sizes['history']} · Movie pool {pool_sizes['movie']}."
        )

    def _open_session_health_dialog(self) -> None:
        """Navigate to the Session Health tab and refresh local status."""
        self._set_current_page("session_health")
        self._refresh_account_health()

    def _make_account_health_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout()
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Publishing Dashboard")
        title.setObjectName("sectionTitle")
        subtitle = QLabel(
            "Per-account login and publish readiness. 'Due now' is how many reels "
            "could post right now; a red session means due work is blocked until "
            "re-login. Local checks are instant and safe; 'Check All (live)' "
            "confirms each session with Instagram, spaced out."
        )
        subtitle.setObjectName("metaValue")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self._dashboard_totals_label = QLabel("")
        self._dashboard_totals_label.setObjectName("metaValue")
        self._dashboard_totals_label.setWordWrap(True)
        layout.addWidget(self._dashboard_totals_label)

        self._account_health_table = TableFocusScrollWidget()
        self._account_health_table.setObjectName("downloadQueueTable")
        self._account_health_table.setColumnCount(7)
        self._account_health_table.setHorizontalHeaderLabels(
            ["Account", "Profile", "Session", "Due now", "Scheduled", "Next post", "Detail"]
        )
        health_header = self._account_health_table.horizontalHeader()
        health_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # Numeric columns (Due now, Scheduled, Next post) only need their content
        # width; let Account/Profile/Detail absorb the remaining space.
        for _numeric_col in (3, 4, 5):
            health_header.setSectionResizeMode(
                _numeric_col, QHeaderView.ResizeMode.ResizeToContents
            )
        health_header.setStretchLastSection(True)
        # Hide row numbers + the white corner button, matching every other table.
        self._account_health_table.verticalHeader().setVisible(False)
        self._account_health_table.setMinimumHeight(240)
        self._account_health_table.setMaximumHeight(400)
        self._account_health_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._account_health_table.itemSelectionChanged.connect(
            self._on_account_health_selection_changed
        )
        layout.addWidget(self._account_health_table)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self._account_health_refresh_button = QPushButton("Refresh")
        self._account_health_refresh_button.setObjectName("downloadToolbarButton")
        self._account_health_refresh_button.clicked.connect(self._refresh_account_health)
        self._account_health_check_all_button = QPushButton("Check All (live)")
        self._account_health_check_all_button.setObjectName("downloadToolbarButton")
        self._account_health_check_all_button.clicked.connect(self._on_check_all_health_clicked)
        self._account_relogin_button = QPushButton("Re-login")
        self._account_relogin_button.setObjectName("downloadToolbarButton")
        self._account_relogin_button.setEnabled(False)
        self._account_relogin_button.clicked.connect(self._on_relogin_selected_account_clicked)
        self._account_copy_email_button = QPushButton("Copy Email")
        self._account_copy_email_button.setObjectName("downloadToolbarButton")
        self._account_copy_email_button.setEnabled(False)
        self._account_copy_email_button.clicked.connect(self._on_copy_account_email_clicked)
        actions.addWidget(self._account_health_refresh_button)
        actions.addWidget(self._account_health_check_all_button)
        actions.addWidget(self._account_relogin_button)
        actions.addWidget(self._account_copy_email_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._account_health_summary = QLabel("")
        self._account_health_summary.setObjectName("subtleLabel")
        self._account_health_summary.setWordWrap(True)
        layout.addWidget(self._account_health_summary)
        layout.addStretch(1)

        panel.setLayout(layout)
        return panel

    _HEALTH_LABELS = {
        HealthState.OK: "OK",
        HealthState.WARN: "Aging",
        HealthState.STALE: "Re-login",
        HealthState.NO_SESSION: "No login",
        HealthState.COOLDOWN: "Cooldown",
        HealthState.THROTTLED: "Throttled",
        HealthState.LOGGED_OUT: "Logged out",
        HealthState.UNKNOWN: "Unknown",
        HealthState.NOT_CONFIGURED: "No profile",
        HealthState.MISMATCH: "Wrong account",
    }

    @classmethod
    def _health_state_label(cls, state: str) -> str:
        return cls._HEALTH_LABELS.get(state, state)

    @staticmethod
    def _apply_health_status_style(item: QTableWidgetItem, state: str) -> None:
        if state == HealthState.OK:
            item.setForeground(QColor("#8ee6b1"))
            item.setBackground(QColor("#173222"))
        elif state == HealthState.COOLDOWN:
            item.setForeground(QColor("#9fc6ff"))
            item.setBackground(QColor("#162a45"))
        elif state in {HealthState.WARN, HealthState.UNKNOWN}:
            item.setForeground(QColor("#f5cd79"))
            item.setBackground(QColor("#342812"))
        else:  # STALE, NO_SESSION, THROTTLED, LOGGED_OUT
            item.setForeground(QColor("#ff9c9c"))
            item.setBackground(QColor("#35171c"))

    def _selected_health_profile(self) -> tuple[str, str | None] | None:
        if not hasattr(self, "_account_health_table"):
            return None
        items = self._account_health_table.selectedItems()
        if not items:
            return None
        cell = self._account_health_table.item(items[0].row(), 0)
        if cell is None:
            return None
        profile = cell.data(Qt.ItemDataRole.UserRole)
        if not profile:
            return None
        email = cell.data(Qt.ItemDataRole.UserRole + 1)
        return str(profile), (str(email) if email else None)

    def _on_account_health_selection_changed(self) -> None:
        if not hasattr(self, "_account_relogin_button"):
            return
        selected = self._selected_health_profile()
        self._account_relogin_button.setEnabled(
            selected is not None and not self._account_health_in_progress
        )
        self._account_copy_email_button.setEnabled(
            selected is not None and bool(selected[1])
        )

    @staticmethod
    def _format_next_post(when: dt.datetime | None) -> str:
        """Local 'HH:MM (in Nh Mm)' for the soonest scheduled post, or '—'."""
        if when is None:
            return "—"
        now = dt.datetime.now(dt.timezone.utc)
        minutes = int((when - now).total_seconds() // 60)
        if minutes < 0:
            relative = "due"
        elif minutes < 60:
            relative = f"in {minutes}m"
        elif minutes < 60 * 24:
            relative = f"in {minutes // 60}h {minutes % 60}m"
        else:
            relative = f"in {minutes // (60 * 24)}d"
        return f"{when.astimezone():%H:%M} ({relative})"

    def _set_account_health_row(
        self,
        dash_row: AccountDashboardRow,
        email: str | None,
        *,
        collision_note: str = "",
    ) -> None:
        table = self._account_health_table
        row = table.rowCount()
        table.insertRow(row)
        detail = dash_row.session_detail
        if dash_row.blocked_reason:
            detail = f"{dash_row.blocked_reason}. {detail}" if detail else dash_row.blocked_reason
        if collision_note:
            detail = f"{detail}  {collision_note}" if detail else collision_note
        values = [
            dash_row.account_name or "(unnamed)",
            dash_row.profile or "—",  # blank profile shows as unset, never as "main"
            self._health_state_label(dash_row.session_state),
            str(dash_row.due_now),
            str(dash_row.scheduled),
            self._format_next_post(dash_row.next_post_at),
            detail,
        ]
        for col, value in enumerate(values):
            cell = QTableWidgetItem(value)
            cell.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            if col == 0:
                # Empty profile -> re-login stays disabled (_selected_health_profile
                # treats falsy as "nothing to act on").
                cell.setData(Qt.ItemDataRole.UserRole, dash_row.profile or "")
                cell.setData(Qt.ItemDataRole.UserRole + 1, email)
                cell.setData(Qt.ItemDataRole.UserRole + 2, dash_row.account_name or "")
            if col == 2:
                self._apply_health_status_style(cell, dash_row.session_state)
            if col == 3:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # Colour the due-now count by what it MEANS: green = ready to go,
                # red = work is blocked behind a bad session, plain = nothing due.
                if dash_row.blocked_reason:
                    cell.setForeground(QColor("#ff9c9c"))
                elif dash_row.publishable:
                    cell.setForeground(QColor("#8ee6b1"))
            if col in (4, 5):
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, col, cell)

    @staticmethod
    def _clean_profile(value: str | None) -> str:
        """An account's Instagram profile, blank if unset.

        A blank profile must NEVER be treated as ``main``: that silently borrows
        whatever account ``main`` is logged into and would publish there.
        """
        return (value or "").strip()

    @staticmethod
    def _not_configured_health(account_name: str | None) -> SessionHealth:
        return SessionHealth(
            profile_name="",
            account_name=account_name,
            state=HealthState.NOT_CONFIGURED,
            detail="No Instagram profile assigned — set one in account settings, then log in.",
            checked_at=dt.datetime.now(dt.timezone.utc),
            is_live=False,
        )

    @staticmethod
    def _profile_collision_note(
        profile_name: str | None,
        account_name: str | None,
        profile_accounts: dict[str, list[str]],
    ) -> str:
        """Warn when more than one account shares a single profile/session."""
        if not profile_name:
            return ""
        others = [
            name
            for name in profile_accounts.get(profile_name, [])
            if name != (account_name or "(unnamed)")
        ]
        if not others:
            return ""
        return f"(!) shares profile '{profile_name}' with {', '.join(others)}"

    def _refresh_account_health(self) -> None:
        if not hasattr(self, "_account_health_table"):
            return
        now = dt.datetime.now(dt.timezone.utc)
        pool = ProfilePool.load()
        with get_session() as session:
            accounts = session.query(Account).order_by(Account.name.asc()).all()
            rows = [
                (
                    account.id,
                    account.name,
                    self._clean_profile(account.instagram_profile) or None,
                    account.login_identifier,
                    account.upload_schedule_slots,
                )
                for account in accounts
            ]
            # One query for all un-posted jobs, bucketed by account, so the
            # dashboard scales without an N+1 per-account query.
            jobs_by_account: dict[int, list[PublishJobView]] = {}
            for account_id, status, posted_at, scheduled_at in (
                session.query(
                    UploadJob.account_id,
                    UploadJob.status,
                    UploadJob.posted_at,
                    UploadJob.scheduled_at,
                )
                .filter(UploadJob.posted_at.is_(None))
                .all()
            ):
                jobs_by_account.setdefault(account_id, []).append(
                    PublishJobView(status=status, posted_at=posted_at, scheduled_at=scheduled_at)
                )

        # Map each non-empty profile to the accounts using it, so a shared session
        # (two accounts on one profile) is flagged instead of silently allowed.
        profile_accounts: dict[str, list[str]] = {}
        for _id, account_name, profile_name, _email, _slots in rows:
            if profile_name:
                profile_accounts.setdefault(profile_name, []).append(account_name or "(unnamed)")

        self._account_health_table.blockSignals(True)
        self._account_health_table.setRowCount(0)
        dash_rows: list[AccountDashboardRow] = []
        for account_id, account_name, profile_name, email, slots in rows:
            if profile_name is None:
                health = self._not_configured_health(account_name)
            else:
                health = local_health(profile_name, account_name, pool=pool)
            dash_row = build_dashboard_row(
                account_id=account_id,
                account_name=account_name or "(unnamed)",
                profile=profile_name,
                session_state=health.state,
                session_label=self._health_state_label(health.state),
                session_detail=health.detail,
                jobs=jobs_by_account.get(account_id, []),
                slots=slots,
                now=now,
            )
            dash_rows.append(dash_row)
            note = self._profile_collision_note(profile_name, account_name, profile_accounts)
            self._set_account_health_row(dash_row, email, collision_note=note)
        self._account_health_table.resizeRowsToContents()
        self._account_health_table.blockSignals(False)

        totals = summarize_dashboard(dash_rows)
        if dash_rows:
            strip = (
                f"{totals.account_count} account(s) · "
                f"{totals.total_due_now} due now · "
                f"{totals.total_scheduled} scheduled · "
                f"next post {self._format_next_post(totals.next_post_at)}"
            )
            if totals.blocked_accounts:
                strip += f"  ·  ⚠ {totals.blocked_accounts} account(s) blocked (re-login)"
            self._dashboard_totals_label.setText(strip)
            shared = sum(1 for names in profile_accounts.values() if len(names) > 1)
            summary = (
                f"{totals.account_count} account(s). Local status shown — use "
                "'Check All (live)' to confirm sessions with Instagram."
            )
            if shared:
                summary += f" (!) {shared} profile(s) shared by multiple accounts."
            self._account_health_summary.setText(summary)
        else:
            self._dashboard_totals_label.setText("")
            self._account_health_summary.setText(
                "No accounts yet. Create a niche account to track its login."
            )
        self._on_account_health_selection_changed()

    def _on_check_all_health_clicked(self) -> None:
        if self._account_health_in_progress:
            self._notify("A health check is already running.", Tone.WARNING)
            return
        with get_session() as session:
            accounts = session.query(Account).order_by(Account.name.asc()).all()
            targets: list[tuple[str, str, str | None]] = []
            unconfigured = 0
            for account in accounts:
                profile = self._clean_profile(account.instagram_profile)
                if profile:
                    expected = (account.instagram_handle or "").strip() or None
                    targets.append((account.name, profile, expected))
                else:
                    unconfigured += 1
        if not targets:
            self._notify(
                "No accounts have an Instagram profile set — assign one first.", Tone.INFO
            )
            return

        self._account_health_in_progress = True
        self._account_health_check_all_button.setEnabled(False)
        self._account_relogin_button.setEnabled(False)
        message = f"Checking {len(targets)} account(s) live, spaced out..."
        if unconfigured:
            message += f" ({unconfigured} skipped — no profile set.)"
        self._notify(message, Tone.INFO)

        thread = QThread(self)
        worker = AccountHealthCheckWorker(targets)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self._on_account_health_result)
        worker.completed.connect(self._on_account_health_check_done)
        worker.completed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._account_health_thread = thread
        self._account_health_worker = worker
        thread.start()

    def _on_account_health_result(self, payload: dict) -> None:
        account_name = payload.get("account_name") or ""
        state = str(payload.get("state"))
        detail = payload.get("detail") or ""
        table = self._account_health_table
        for row in range(table.rowCount()):
            cell = table.item(row, 0)
            if cell is None:
                continue
            if cell.data(Qt.ItemDataRole.UserRole + 2) == account_name:
                status_item = table.item(row, 2)
                if status_item is not None:
                    status_item.setText(self._health_state_label(state))
                    self._apply_health_status_style(status_item, state)
                detail_item = table.item(row, 6)  # Detail is the last column
                if detail_item is not None:
                    detail_item.setText(detail)
                break

    def _on_account_health_check_done(self) -> None:
        self._account_health_in_progress = False
        self._account_health_thread = None
        self._account_health_worker = None
        self._account_health_check_all_button.setEnabled(True)
        self._on_account_health_selection_changed()
        self._notify("Live health check complete.", Tone.SUCCESS)

    def _on_relogin_selected_account_clicked(self) -> None:
        selected = self._selected_health_profile()
        if selected is None:
            self._notify("Select an account first.", Tone.WARNING)
            return
        profile_name, email = selected
        if email:
            QApplication.clipboard().setText(email)
        try:
            launch_instagram_login(profile_name)
        except FileNotFoundError:
            self._notify(
                "Login helper not available in this build. Run the login script manually.",
                Tone.ERROR,
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Could not open Instagram login: {exc}", Tone.ERROR)
            return
        message = "Opened Instagram login. Log in, then click Refresh."
        if email:
            message = f"Email copied. {message}"
        self._notify(message, Tone.SUCCESS)

    def _on_copy_account_email_clicked(self) -> None:
        selected = self._selected_health_profile()
        if selected is None or not selected[1]:
            self._notify("No login email saved for this account.", Tone.WARNING)
            return
        QApplication.clipboard().setText(selected[1])
        self._notify("Copied login email.", Tone.SUCCESS)

    def _make_placeholder_page(self, title: str, message: str) -> QWidget:
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        message_label = QLabel(message)
        message_label.setObjectName("metaValue")
        message_label.setWordWrap(True)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout()
        panel_layout.setContentsMargins(24, 24, 24, 24)
        panel_layout.setSpacing(12)
        panel_layout.addWidget(title_label)
        panel_layout.addWidget(message_label)
        panel_layout.addStretch(1)
        panel.setLayout(panel_layout)

        page = QWidget()
        page_layout = QVBoxLayout()
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(panel)
        page.setLayout(page_layout)
        return page

    def _make_schedule_page(self) -> QWidget:
        title_label = QLabel("Publish Queue")
        title_label.setObjectName("sectionTitle")
        message_label = QLabel(
            "Review Instagram-ready Reels, copy captions, and track manual publishing."
        )
        message_label.setObjectName("metaValue")
        message_label.setWordWrap(True)

        self._schedule_summary_label = QLabel(
            "Select an account workspace to review schedule drafts."
        )
        self._schedule_summary_label.setObjectName("subtleLabel")
        self._schedule_summary_label.setWordWrap(True)

        self._schedule_copy_caption_button = QPushButton("Copy Caption")
        self._schedule_copy_caption_button.setObjectName("downloadToolbarButton")
        self._schedule_copy_caption_button.setEnabled(False)
        self._schedule_copy_caption_button.clicked.connect(self._on_copy_schedule_caption_clicked)
        self._schedule_open_output_button = QPushButton("Open Reel")
        self._schedule_open_output_button.setObjectName("downloadToolbarButton")
        self._schedule_open_output_button.setEnabled(False)
        self._schedule_open_output_button.clicked.connect(self._on_open_schedule_output_clicked)
        self._schedule_copy_path_button = QPushButton("Copy Path")
        self._schedule_copy_path_button.setObjectName("downloadToolbarButton")
        self._schedule_copy_path_button.setEnabled(False)
        self._schedule_copy_path_button.clicked.connect(self._on_copy_schedule_path_clicked)
        self._schedule_open_folder_button = QPushButton("Open Folder")
        self._schedule_open_folder_button.setObjectName("downloadToolbarButton")
        self._schedule_open_folder_button.setEnabled(False)
        self._schedule_open_folder_button.clicked.connect(self._on_open_schedule_folder_clicked)
        self._schedule_instagram_assist_button = QPushButton("Open Instagram Upload")
        self._schedule_instagram_assist_button.setObjectName("downloadToolbarButton")
        self._schedule_instagram_assist_button.setEnabled(False)
        self._schedule_instagram_assist_button.clicked.connect(
            self._on_open_instagram_assist_clicked
        )
        self._schedule_auto_publish_button = QPushButton("Auto Publish")
        self._schedule_auto_publish_button.setObjectName("downloadToolbarButton")
        self._schedule_auto_publish_button.setEnabled(False)
        self._schedule_auto_publish_button.clicked.connect(
            self._on_auto_publish_selected_clicked
        )
        # Batch runner across all accounts: posts every Ready/due job one at a
        # time with a randomized gap. Doubles as a Stop button while running.
        self._schedule_publish_due_button = QPushButton("Publish Due Now")
        self._schedule_publish_due_button.setObjectName("downloadToolbarButton")
        self._schedule_publish_due_button.clicked.connect(
            self._on_publish_due_now_clicked
        )
        # Multi-account: post every due reel across ALL accounts, one at a time
        # with the same randomized gap. Sequential (never simultaneous) keeps the
        # automation footprint low.
        self._schedule_publish_all_button = QPushButton("Publish All Due")
        self._schedule_publish_all_button.setObjectName("downloadToolbarButton")
        self._schedule_publish_all_button.setToolTip(
            "Post every due reel across ALL accounts, one at a time with a "
            "2-7 minute gap between posts (sequential, never simultaneous)."
        )
        self._schedule_publish_all_button.clicked.connect(
            self._on_publish_all_due_clicked
        )
        # Kill-switch: when checked, drive the whole flow but stop before Share.
        self._schedule_dry_run_checkbox = QCheckBox("Safe mode (stop before posting)")

        schedule_action_row = QHBoxLayout()
        schedule_action_row.setSpacing(10)
        schedule_action_row.addWidget(self._schedule_copy_caption_button)
        schedule_action_row.addWidget(self._schedule_open_output_button)
        schedule_action_row.addWidget(self._schedule_copy_path_button)
        schedule_action_row.addWidget(self._schedule_open_folder_button)
        schedule_action_row.addWidget(self._schedule_instagram_assist_button)
        schedule_action_row.addWidget(self._schedule_auto_publish_button)
        schedule_action_row.addWidget(self._schedule_publish_due_button)
        schedule_action_row.addWidget(self._schedule_publish_all_button)
        schedule_action_row.addWidget(self._schedule_dry_run_checkbox)
        schedule_action_row.addStretch(1)

        self._schedule_caption_combo = NoScrollComboBox()
        self._schedule_caption_combo.addItem("Caption", "caption")
        self._schedule_caption_combo.addItem("Title + Caption", "title_caption")
        self._schedule_caption_combo.addItem("Title Only", "title")
        self._schedule_caption_combo.currentIndexChanged.connect(
            self._refresh_schedule_caption_preview
        )
        self._schedule_status_combo = NoScrollComboBox()
        for label, value in (
            ("Draft", "draft"),
            ("Ready", "ready"),
            ("Posted", "posted"),
            ("Skipped", "skipped"),
        ):
            self._schedule_status_combo.addItem(label, value)
        self._schedule_status_combo.setEnabled(False)
        self._schedule_status_combo.currentIndexChanged.connect(
            self._on_schedule_status_combo_changed
        )
        self._schedule_datetime_edit = QDateTimeEdit()
        self._schedule_datetime_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._schedule_datetime_edit.setCalendarPopup(True)
        self._schedule_datetime_edit.setEnabled(False)
        self._schedule_datetime_edit.setMinimumWidth(170)
        self._schedule_datetime_edit.setDateTime(
            self._datetime_to_qdatetime(dt.datetime.now().astimezone())
        )
        self._schedule_save_time_button = QPushButton("Save Time")
        self._schedule_save_time_button.setObjectName("downloadToolbarButton")
        self._schedule_save_time_button.setEnabled(False)
        self._schedule_save_time_button.clicked.connect(self._on_save_schedule_time_clicked)
        self._schedule_clear_time_button = QPushButton("Clear Time")
        self._schedule_clear_time_button.setObjectName("downloadToolbarButton")
        self._schedule_clear_time_button.setEnabled(False)
        self._schedule_clear_time_button.clicked.connect(self._on_clear_schedule_time_clicked)
        # Assign jittered slot times to all unscheduled jobs for this account.
        self._schedule_auto_time_button = QPushButton("Auto-Schedule")
        self._schedule_auto_time_button.setObjectName("downloadToolbarButton")
        self._schedule_auto_time_button.setToolTip(
            "Assign randomized times from this account's schedule slots to unscheduled reels"
        )
        self._schedule_auto_time_button.clicked.connect(self._on_auto_schedule_clicked)
        self._schedule_caption_preview = QTextEdit()
        self._schedule_caption_preview.setPlaceholderText(
            "Select a publish job to preview the caption for manual posting."
        )
        self._schedule_caption_preview.setReadOnly(True)
        self._schedule_caption_preview.setMinimumHeight(120)

        schedule_detail_row = QHBoxLayout()
        schedule_detail_row.setSpacing(10)
        schedule_detail_row.addWidget(QLabel("Copy Text"))
        schedule_detail_row.addWidget(self._schedule_caption_combo)
        schedule_detail_row.addWidget(self._schedule_copy_caption_button)
        schedule_detail_row.addWidget(QLabel("Status"))
        schedule_detail_row.addWidget(self._schedule_status_combo)
        schedule_detail_row.addWidget(QLabel("Schedule"))
        schedule_detail_row.addWidget(self._schedule_datetime_edit)
        schedule_detail_row.addWidget(self._schedule_save_time_button)
        schedule_detail_row.addWidget(self._schedule_clear_time_button)
        schedule_detail_row.addWidget(self._schedule_auto_time_button)
        schedule_detail_row.addStretch(1)

        self._schedule_table = TableFocusScrollWidget()
        self._schedule_table.setObjectName("downloadQueueTable")
        self._schedule_table.setColumnCount(7)
        self._schedule_table.setHorizontalHeaderLabels(
            ["Account", "Video", "Title", "Scheduled", "Privacy", "Status", "Output"]
        )
        _sch_hdr = self._schedule_table.horizontalHeader()
        _sch_hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        _sch_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)  # Title col stretches
        _sch_hdr.setStretchLastSection(False)
        self._schedule_table.setColumnWidth(0, 110)   # Account
        self._schedule_table.setColumnWidth(1, 110)   # Video
        # col 2 (Title) stretches to fill remaining space
        self._schedule_table.setColumnWidth(3, 135)   # Scheduled
        self._schedule_table.setColumnWidth(4, 75)    # Privacy
        self._schedule_table.setColumnWidth(5, 230)   # Status — wide enough for "Failed — <reason>"
        self._schedule_table.setColumnWidth(6, 100)   # Output
        self._schedule_table.verticalHeader().setVisible(False)
        self._schedule_table.setAlternatingRowColors(True)
        self._schedule_table.setShowGrid(True)
        self._schedule_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._schedule_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._schedule_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._schedule_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._schedule_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._schedule_table.setWordWrap(False)
        self._schedule_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._schedule_table.setMinimumHeight(230)
        self._schedule_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._schedule_table.itemSelectionChanged.connect(self._on_schedule_selection_changed)

        panel = QFrame()
        panel.setObjectName("downloadQueuePanel")
        panel_layout = QVBoxLayout()
        panel_layout.setContentsMargins(18, 18, 18, 18)
        panel_layout.setSpacing(12)
        panel_layout.addWidget(title_label)
        panel_layout.addWidget(message_label)
        panel_layout.addWidget(self._schedule_summary_label)
        schedule_action_row.removeWidget(self._schedule_copy_caption_button)
        panel_layout.addLayout(schedule_detail_row)
        panel_layout.addWidget(self._schedule_caption_preview)
        panel_layout.addLayout(schedule_action_row)
        panel_layout.addWidget(self._schedule_table, stretch=1)
        panel.setLayout(panel_layout)
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        page = QWidget()
        page_layout = QVBoxLayout()
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(panel, stretch=1)
        page.setLayout(page_layout)
        self._schedule_title_label = title_label
        self._schedule_message_label = message_label
        self._schedule_panel = panel
        return page

    def _refresh_processing_page(self) -> None:
        if self._current_page != "processing":
            return
        if self._current_account_id is None:
            self._processing_probe = None
            self._processing_probe_item_id = None
            self._selected_processing_item_id = None
            self._processing_item_combo.blockSignals(True)
            self._processing_item_combo.clear()
            self._processing_item_combo.blockSignals(False)
            self._set_processing_placeholder_state("Select an account workspace to prepare videos.")
            return

        items = self._processing_available_items()
        current_item = self._current_selected_item()
        if (
            self._selected_processing_item_id is None
            and current_item is not None
            and current_item.account_id == self._current_account_id
            and self._item_exists(current_item)
        ):
            self._selected_processing_item_id = current_item.id

        if self._selected_processing_item_id is not None and not any(
            item.id == self._selected_processing_item_id for item in items
        ):
            self._selected_processing_item_id = None

        if self._selected_processing_item_id is None and items:
            self._selected_processing_item_id = items[0].id

        self._processing_item_combo.blockSignals(True)
        self._processing_item_combo.clear()
        self._processing_item_combo.addItem("Choose downloaded video...", None)
        for item in items:
            label = item.title or item.video_id or item.source_url
            self._processing_item_combo.addItem(label, item.id)
        selected_index = self._processing_item_combo.findData(self._selected_processing_item_id)
        self._processing_item_combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        self._processing_item_combo.blockSignals(False)

        if not items:
            self._processing_probe = None
            self._processing_probe_item_id = None
            self._refresh_processing_inbox(items)
            self._set_processing_placeholder_state(
                "No downloaded files are ready for processing in this account yet."
            )
            return

        self._refresh_processing_inbox(items)
        self._refresh_processing_selection()

    def _processing_available_items(self) -> list[DownloadItem]:
        available_items: list[DownloadItem] = []
        visible_item_ids = {item.id for item in self._displayed_items}
        stale_ids = [
            item_id
            for item_id in self._processing_item_probe_cache
            if item_id not in visible_item_ids
        ]
        for item_id in stale_ids:
            self._processing_item_probe_cache.pop(item_id, None)

        for item in self._displayed_items:
            if item.account_id != self._current_account_id or not self._item_exists(item):
                continue
            path = Path(item.file_path)
            try:
                stat = path.stat()
                cache_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
            cached_probe = self._processing_item_probe_cache.get(item.id)
            if cached_probe is None or cached_probe[:3] != cache_key:
                try:
                    probe_video(path)
                except Exception:  # noqa: BLE001
                    self._processing_item_probe_cache[item.id] = (*cache_key, False)
                    continue
                self._processing_item_probe_cache[item.id] = (*cache_key, True)
            if not self._processing_item_probe_cache[item.id][3]:
                continue
            available_items.append(item)
        return available_items

    def _processing_source_label_map(self, items: list[DownloadItem]) -> dict[int, str]:
        if self._current_account_id is None or not items:
            return {}

        item_keys: dict[int, set[str]] = {}
        all_keys: set[str] = set()
        for item in items:
            keys = {item.source_url}
            if item.video_id:
                keys.add(item.video_id)
            item_keys[item.id] = keys
            all_keys.update(keys)

        if not all_keys:
            return {}

        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == self._current_account_id)
                .all()
            )

        matched_labels: dict[int, str] = {}
        for item_id, keys in item_keys.items():
            for candidate in candidates:
                candidate_keys = {candidate.source_url}
                if candidate.video_id:
                    candidate_keys.add(candidate.video_id)
                if keys & candidate_keys:
                    matched_labels[item_id] = "Scraped"
                    break
            else:
                matched_labels[item_id] = "Manual"
        return matched_labels

    def _processing_status_text(self, item: DownloadItem) -> str:
        if item.file_path:
            output_path = self._existing_processed_output_path_for_item(item)
            if output_path is not None and output_path.exists():
                return "Processed"
        if item.title_draft or item.caption_draft or item.smart_generated_at:
            return "Drafted"
        return "Not Processed"

    @staticmethod
    def _processing_status_colors(status: str) -> tuple[QColor, QColor]:
        colors = {
            "Not Processed": (QColor("#3a1f24"), QColor("#ffd0d0")),
            "Drafted": (QColor("#3a2b12"), QColor("#ffe0a3")),
            "Processed": (QColor("#123521"), QColor("#9ff0bf")),
        }
        return colors.get(status, (QColor("#111827"), QColor("#d7e0ea")))

    def _processing_action_text(self, item: DownloadItem) -> str:
        status = self._processing_status_text(item)
        if status == "Processed":
            return "Reprocess"
        if status == "Drafted":
            return "Continue"
        return "Preprocess"

    def _matches_processing_inbox_filters(self, item: DownloadItem) -> bool:
        selected_state = self._processing_state_filter.currentData()
        status = self._processing_status_text(item)
        if selected_state == "needs" and status == "Processed":
            return False
        if selected_state == "processed" and status != "Processed":
            return False

        query = self._processing_search_input.text().strip().lower()
        if not query:
            return True

        source_label = self._processing_source_labels.get(item.id, "Manual")
        haystacks = [
            item.title or "",
            item.source_url,
            item.video_id or "",
            status,
            source_label,
        ]
        return any(query in value.lower() for value in haystacks)

    def _refresh_processing_inbox(self, items: list[DownloadItem]) -> None:
        self._processing_source_labels = self._processing_source_label_map(items)
        visible_items = [item for item in items if self._matches_processing_inbox_filters(item)]

        self._processing_inbox_table.blockSignals(True)
        self._processing_inbox_table.setRowCount(0)
        for item in visible_items:
            row = self._processing_inbox_table.rowCount()
            self._processing_inbox_table.insertRow(row)

            values = [
                self._processing_status_text(item),
                self._processing_source_labels.get(item.id, "Manual"),
                item.title or item.video_id or item.source_url,
                self._created_text(item),
                self._processing_action_text(item),
            ]
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.ItemDataRole.UserRole, item.id)
                table_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if column == 0:
                    background, foreground = self._processing_status_colors(value)
                    table_item.setBackground(background)
                    table_item.setForeground(foreground)
                self._processing_inbox_table.setItem(row, column, table_item)

            if item.id == self._selected_processing_item_id:
                self._processing_inbox_table.selectRow(row)

        self._processing_inbox_table.resizeRowsToContents()
        for row in range(self._processing_inbox_table.rowCount()):
            self._processing_inbox_table.setRowHeight(row, 32)
        self._processing_inbox_table.setColumnWidth(0, 130)
        self._processing_inbox_table.setColumnWidth(1, 96)
        self._processing_inbox_table.setColumnWidth(3, 150)
        self._processing_inbox_table.setColumnWidth(4, 110)
        self._processing_inbox_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._processing_inbox_table.blockSignals(False)

    def _set_processing_placeholder_state(self, message: str) -> None:
        self._processing_summary_label.setText(message)
        self._processing_preview_path = None
        self._processing_preview_mode = "source"
        self._stop_processing_preview()
        self._processing_toggle_preview_button.setText("Play Full Video")
        self._processing_preview_position_slider.setRange(0, 0)
        self._processing_preview_position_slider.setValue(0)
        self._processing_preview_time_label.setText("00:00 / 00:00")
        self._processing_preview_last_frame_ms = None
        self._processing_preview_meta_label.setText("Select a downloaded video to preview it here.")
        self._processing_video_widget.setPixmap(QPixmap())
        self._processing_video_widget.setText("Preview unavailable")
        self._processing_preview_mode_combo.blockSignals(True)
        self._processing_preview_mode_combo.setCurrentIndex(0)
        self._processing_preview_mode_combo.blockSignals(False)
        self._processing_title_draft_input.setText("")
        self._processing_caption_draft_input.setPlainText("")
        self._processing_transcript_input.setPlainText("")
        self._processing_last_generated_titles = []
        self._processing_dirty_item_id = None
        self._set_processing_manual_crop_values(0, 0)
        self._processing_using_ai_layout_crop = False
        self._processing_ai_suggested_layout = None
        self._processing_clip_premise_input.blockSignals(True)
        self._processing_clip_premise_input.setPlainText("")
        self._processing_clip_premise_input.blockSignals(False)
        self._processing_smart_summary_label.setText(
            "Smart draft summary will appear here after Groq generation."
        )
        self._set_processing_smart_recommendation(None)
        self._set_processing_eval_state()
        self._refresh_processing_usage_label()
        self._set_processing_smart_options([], [])
        self._apply_processing_template("gaming_meme_black")
        self._processing_style_status_label.setText(
            "Template controls the rendered title style. Edit the title text and caption below."
        )
        self._processing_draft_status_label.setText(
            "Generate visual-first title and caption drafts from the selected downloaded video."
        )
        self._processing_suggestion_label.setText(
            "Automatic crop suggestions will use border detection now and OCR once Tesseract is installed."
        )
        self._processing_progress_label.setText("")
        if not self._processing_in_progress:
            self._processing_progress_bar.setVisible(False)
            self._stop_processing_loading_state()
        self._set_processing_controls_enabled(False)

    def _set_processing_controls_enabled(self, enabled: bool) -> None:
        combo_enabled = enabled and not self._processing_in_progress
        self._processing_item_combo.setEnabled(combo_enabled)
        self._processing_inbox_table.setEnabled(combo_enabled)
        self._processing_state_filter.setEnabled(combo_enabled)
        self._processing_search_input.setEnabled(combo_enabled)
        self._processing_export_button.setEnabled(combo_enabled)
        self._processing_open_processed_button.setEnabled(True)
        self._processing_preview_back_button.setEnabled(
            combo_enabled and self._processing_preview_path is not None
        )
        self._processing_preview_back_large_button.setEnabled(
            combo_enabled and self._processing_preview_path is not None
        )
        self._processing_preview_forward_button.setEnabled(
            combo_enabled and self._processing_preview_path is not None
        )
        self._processing_preview_forward_large_button.setEnabled(
            combo_enabled and self._processing_preview_path is not None
        )
        self._processing_generate_drafts_button.setEnabled(combo_enabled)
        self._processing_copy_chat_prompt_button.setEnabled(combo_enabled)
        self._processing_save_drafts_button.setEnabled(combo_enabled)
        self._processing_title_draft_input.setEnabled(combo_enabled)
        self._processing_caption_draft_input.setEnabled(combo_enabled)
        self._processing_caption_style_combo.setEnabled(combo_enabled)
        self._processing_prompt_title_style_combo.setEnabled(combo_enabled)
        self._processing_transcript_input.setEnabled(True)
        self._processing_clip_premise_input.setEnabled(combo_enabled)
        for button in self._processing_smart_option_buttons:
            button.setEnabled(combo_enabled and button.isVisible())
        for title_input in self._processing_smart_option_title_inputs:
            title_input.setEnabled(combo_enabled)
        for caption_input in self._processing_smart_option_caption_inputs:
            caption_input.setEnabled(combo_enabled)
        self._processing_title_style_combo.setEnabled(combo_enabled)
        self._processing_template_combo.setEnabled(combo_enabled)
        self._processing_title_layout_combo.setEnabled(combo_enabled)
        self._processing_top_crop_spin.setEnabled(combo_enabled)
        self._processing_bottom_crop_spin.setEnabled(combo_enabled)
        self._processing_title_font_size.setEnabled(combo_enabled)
        self._processing_title_font_combo.setEnabled(combo_enabled)
        self._processing_title_color_input.setEnabled(combo_enabled)
        self._processing_title_background_combo.setEnabled(combo_enabled)

    def _start_processing_loading_state(self, base_text: str) -> None:
        self._processing_loading_base_text = base_text
        self._processing_loading_phase = 0
        self._processing_loading_badge.setProperty("tone", "info")
        self._processing_loading_badge.style().unpolish(self._processing_loading_badge)
        self._processing_loading_badge.style().polish(self._processing_loading_badge)
        self._processing_loading_badge.setVisible(True)
        self._processing_generate_drafts_button.setText("Generating...")
        self._on_processing_loading_tick()
        self._processing_loading_timer.start()

    def _stop_processing_loading_state(self) -> None:
        self._processing_loading_timer.stop()
        self._processing_loading_base_text = ""
        self._processing_loading_phase = 0
        self._processing_loading_badge.setVisible(False)
        self._processing_generate_drafts_button.setText("Generate")

    def _on_processing_loading_tick(self) -> None:
        if not self._processing_loading_base_text:
            self._processing_loading_badge.setVisible(False)
            return
        dots = "." * ((self._processing_loading_phase % 3) + 1)
        self._processing_loading_badge.setText(f"{self._processing_loading_base_text}{dots}")
        self._processing_loading_phase += 1

    def _processing_selected_item(self) -> DownloadItem | None:
        if self._selected_processing_item_id is None:
            return None
        return next(
            (
                item
                for item in self._displayed_items
                if item.id == self._selected_processing_item_id
            ),
            None,
        )

    def _mark_processing_text_dirty(self) -> None:
        if self._suppress_processing_text_dirty:
            return
        if self._selected_processing_item_id is not None:
            self._processing_dirty_item_id = self._selected_processing_item_id
        self._mark_user_interacting()

    def _processing_has_unsaved_text_edits(self) -> bool:
        return (
            self._current_page == "processing"
            and self._selected_processing_item_id is not None
            and self._processing_dirty_item_id == self._selected_processing_item_id
        )

    def _on_processing_clip_premise_changed(self) -> None:
        item = self._processing_selected_item()
        if item is None:
            return
        premise = self._processing_clip_premise_input.toPlainText().strip()
        if premise:
            self._processing_clip_premises[item.id] = premise
        else:
            self._processing_clip_premises.pop(item.id, None)

    @staticmethod
    def _caption_for_save(raw_text: str) -> str | None:
        """Preserve the caption verbatim (blank lines, trailing newlines,
        indentation) when persisting. Only collapse to None when the field
        is empty or contains nothing but whitespace, so the DB column stays
        NULL instead of holding an empty/whitespace-only string."""
        return raw_text if raw_text.strip() else None

    @staticmethod
    def _set_text_edit_if_changed(text_edit: QTextEdit, value: str) -> None:
        if text_edit.toPlainText() == value:
            return
        cursor = text_edit.textCursor()
        position = cursor.position()
        anchor = cursor.anchor()
        text_edit.blockSignals(True)
        text_edit.setPlainText(value)
        restored_cursor = text_edit.textCursor()
        max_position = len(value)
        restored_cursor.setPosition(min(anchor, max_position))
        restored_cursor.setPosition(
            min(position, max_position),
            QTextCursor.MoveMode.KeepAnchor,
        )
        text_edit.setTextCursor(restored_cursor)
        text_edit.blockSignals(False)

    def _refresh_processing_selection(self) -> None:
        item = self._processing_selected_item()
        if item is None or not self._item_exists(item):
            self._processing_probe = None
            self._processing_probe_item_id = None
            self._processing_style_loaded_for_item_id = None
            self._processing_auto_crop = CropSettings()
            self._processing_using_ai_layout_crop = False
            self._processing_ai_suggested_layout = None
            self._processing_raw_transcript_text = ""
            self._set_processing_placeholder_state("Select a downloaded video to configure a crop.")
            return

        path = Path(item.file_path or "").expanduser().resolve()
        preview_path = self._processing_preview_target_path(path)
        preview_path_changed = self._processing_preview_path != preview_path
        self._processing_preview_path = preview_path
        if preview_path_changed or self._processing_preview_container is None:
            self._load_processing_preview(preview_path)
            self._processing_toggle_preview_button.setText("Play Full Video")
            self._processing_preview_position_slider.setValue(0)
        if not self._processing_has_unsaved_text_edits():
            self._suppress_processing_text_dirty = True
            try:
                self._processing_raw_transcript_text = item.transcript_text or ""
                self._processing_title_draft_input.setText(item.title_draft or "")
                self._processing_caption_draft_input.setPlainText(item.caption_draft or "")
                self._set_text_edit_if_changed(
                    self._processing_clip_premise_input,
                    self._processing_clip_premises.get(item.id, ""),
                )
                self._processing_transcript_input.setPlainText(
                    self._processing_context_text(item, self._processing_raw_transcript_text)
                )
                self._load_processing_smart_drafts(item)
                # Only re-apply the item's saved style on a real item switch.
                # Auto-refresh ticks on the same item must not clobber the
                # template/font/colour the user just picked in the dropdown.
                # Account-level preferences (saved on every template change)
                # remain the source of truth for the in-session selection.
                if self._processing_style_loaded_for_item_id != item.id:
                    self._load_processing_style_state(item)
                    self._processing_style_loaded_for_item_id = item.id
            finally:
                self._suppress_processing_text_dirty = False
        if item.transcript_text:
            self._processing_draft_status_label.setText(
                "Saved text drafts are loaded for this video."
            )
        else:
            self._processing_draft_status_label.setText(
                "Generate visual-first title and caption drafts from the selected downloaded video."
            )

        if self._processing_probe_item_id != item.id:
            try:
                self._processing_probe = probe_video(path)
                self._processing_probe_item_id = item.id
                self._processing_auto_crop = CropSettings()
                self._set_processing_manual_crop_ranges(self._processing_probe)
                self._set_processing_manual_crop_values(0, 0)
                self._processing_using_ai_layout_crop = False
                self._processing_ai_suggested_layout = None
            except Exception as exc:  # noqa: BLE001
                self._processing_probe = None
                self._processing_probe_item_id = None
                self._processing_summary_label.setText(
                    "Could not inspect the selected video. Install ffprobe or choose another file."
                )
                self._processing_preview_meta_label.setText(str(exc))
                self._set_processing_controls_enabled(False)
                return

        probe = self._processing_probe
        assert probe is not None

        self._processing_preview_meta_label.setText(
            (
                f"{item.title or '(untitled)'} • "
                f"{probe.width} x {probe.height} • "
                f"{probe.duration_seconds:.2f}s • "
                f"Processed output: {self._processed_output_path_for_item(item).name}"
            )
        )
        self._processing_suggestion_label.setText(
            "Processing will auto-detect the crop and render the applied title onto the output video."
        )
        self._processing_summary_label.setText(
            "Generate your draft, then process the video. Crop is detected automatically during export."
        )
        self._refresh_processing_output_preview()

    def _apply_title_style_preset(self, preset_key: str) -> None:
        config = TITLE_STYLE_PRESETS.get(preset_key, TITLE_STYLE_PRESETS["clean_hook"])
        self._processing_title_style_combo.blockSignals(True)
        index = self._processing_title_style_combo.findData(preset_key)
        self._processing_title_style_combo.setCurrentIndex(index if index >= 0 else 0)
        self._processing_title_style_combo.blockSignals(False)
        self._processing_title_font_size.setValue(int(config["font_size"]))
        font_index = self._processing_title_font_combo.findData(
            str(config.get("font_name", "segoe_ui"))
        )
        self._processing_title_font_combo.setCurrentIndex(font_index if font_index >= 0 else 0)
        self._processing_title_color_input.setText(str(config["text_color"]))
        background_index = self._processing_title_background_combo.findData(
            str(config["background"])
        )
        self._processing_title_background_combo.setCurrentIndex(
            background_index if background_index >= 0 else 0
        )
        layout_index = self._processing_title_layout_combo.findData("top_band")
        self._processing_title_layout_combo.setCurrentIndex(
            layout_index if layout_index >= 0 else 0
        )

    def _set_processing_template(self, template_key: str) -> None:
        self._processing_template_combo.blockSignals(True)
        index = self._processing_template_combo.findData(template_key)
        self._processing_template_combo.setCurrentIndex(index if index >= 0 else 0)
        self._processing_template_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Per-account Processing preference persistence.
    #
    # Goal: when the user switches between niche accounts (Cinema vs Meme
    # vs History), the Processing page restores that account's last-used
    # template / caption style / title style / font / colour / etc., so
    # they aren't re-picking the niche aesthetic every time.
    #
    # Storage: a single ``processing_preferences`` JSON column on Account.
    # ``_processing_preferences_snapshot`` reads current widget state into
    # a dict; ``_save_processing_preferences_for_current_account`` writes
    # it to the DB; ``_apply_processing_preferences_for_account`` reads it
    # back and applies it to the widgets when the account is selected.
    #
    # Looping guard: ``_suppress_processing_prefs_save`` is True while we
    # programmatically set widget values from a snapshot, so the change
    # signals we emit don't feed straight back into another save.
    # ------------------------------------------------------------------

    _PROCESSING_PREFERENCE_KEYS: tuple[str, ...] = (
        "template",
        "caption_style",
        "prompt_title_style",
        "title_style_preset",
        "font_name",
        "font_size",
        "text_color",
        "background",
        "layout",
    )

    def _processing_preferences_snapshot(self) -> dict[str, object]:
        """Read current Processing widget values into a snapshot dict."""
        return {
            "template": self._processing_template_combo.currentData() or "",
            "caption_style": self._processing_caption_style_combo.currentData() or "",
            "prompt_title_style": (
                self._processing_prompt_title_style_combo.currentData() or ""
            ),
            "title_style_preset": self._processing_title_style_combo.currentData() or "",
            "font_name": self._processing_title_font_combo.currentData() or "",
            "font_size": int(self._processing_title_font_size.value()),
            "text_color": self._processing_title_color_input.text().strip(),
            "background": self._processing_title_background_combo.currentData() or "",
            "layout": self._processing_title_layout_combo.currentData() or "",
            "alter_audio": bool(self._processing_alter_audio_checkbox.isChecked()),
        }

    def _save_processing_preferences_for_current_account(self) -> None:
        """Persist the current widget snapshot to the active account."""
        if self._suppress_processing_prefs_save:
            return
        if self._current_account_id is None:
            return
        snapshot = self._processing_preferences_snapshot()
        try:
            payload = json.dumps(snapshot, ensure_ascii=False)
        except (TypeError, ValueError):
            # snapshot values are primitives — JSON encoding should never
            # fail, but if a future field is added without thinking about
            # serialisation, skip the save instead of crashing the UI.
            return
        try:
            with get_session() as session:
                account = session.get(Account, self._current_account_id)
                if account is None:
                    return
                if account.processing_preferences == payload:
                    return  # no change → skip the write
                account.processing_preferences = payload
                session.commit()
        except Exception:  # noqa: BLE001 — DB write must never crash the UI
            return

    def _apply_processing_preferences_for_account(self, account_id: int | None) -> bool:
        """Restore the saved snapshot for ``account_id`` onto the Processing
        widgets. Returns True when a snapshot was applied, False when the
        account has no saved preferences yet — so callers can fall back to a
        template default. No-op (returns False) for first-time accounts, which
        keep whatever defaults the UI already shows."""
        if account_id is None:
            return False
        try:
            with get_session() as session:
                account = session.get(Account, account_id)
                payload = account.processing_preferences if account else None
        except Exception:  # noqa: BLE001
            return False
        if not payload:
            return False
        try:
            snapshot = json.loads(payload)
        except (TypeError, ValueError):
            return False
        if not isinstance(snapshot, dict):
            return False

        widgets = [
            self._processing_template_combo,
            self._processing_caption_style_combo,
            self._processing_prompt_title_style_combo,
            self._processing_title_style_combo,
            self._processing_title_font_combo,
            self._processing_title_font_size,
            self._processing_title_color_input,
            self._processing_title_background_combo,
            self._processing_title_layout_combo,
            self._processing_alter_audio_checkbox,
        ]
        previous_suppress = self._suppress_processing_prefs_save
        self._suppress_processing_prefs_save = True
        for widget in widgets:
            widget.blockSignals(True)
        try:
            template_value = str(snapshot.get("template", "") or "")
            if template_value:
                index = self._processing_template_combo.findData(template_value)
                if index >= 0:
                    self._processing_template_combo.setCurrentIndex(index)

            caption_value = str(snapshot.get("caption_style", "") or "")
            if caption_value:
                index = self._processing_caption_style_combo.findData(caption_value)
                if index >= 0:
                    self._processing_caption_style_combo.setCurrentIndex(index)

            prompt_title_value = str(snapshot.get("prompt_title_style", "") or "")
            # Empty string is a real value here ("Auto") so always restore it.
            index = self._processing_prompt_title_style_combo.findData(prompt_title_value)
            if index >= 0:
                self._processing_prompt_title_style_combo.setCurrentIndex(index)

            title_style_value = str(snapshot.get("title_style_preset", "") or "")
            if title_style_value:
                index = self._processing_title_style_combo.findData(title_style_value)
                if index >= 0:
                    self._processing_title_style_combo.setCurrentIndex(index)

            font_value = str(snapshot.get("font_name", "") or "")
            if font_value:
                index = self._processing_title_font_combo.findData(font_value)
                if index >= 0:
                    self._processing_title_font_combo.setCurrentIndex(index)

            font_size_value = snapshot.get("font_size")
            if isinstance(font_size_value, int) and 18 <= font_size_value <= 144:
                self._processing_title_font_size.setValue(font_size_value)

            color_value = str(snapshot.get("text_color", "") or "").strip()
            if color_value:
                self._processing_title_color_input.setText(color_value)

            background_value = str(snapshot.get("background", "") or "")
            if background_value:
                index = self._processing_title_background_combo.findData(background_value)
                if index >= 0:
                    self._processing_title_background_combo.setCurrentIndex(index)

            layout_value = str(snapshot.get("layout", "") or "")
            if layout_value:
                index = self._processing_title_layout_combo.findData(layout_value)
                if index >= 0:
                    self._processing_title_layout_combo.setCurrentIndex(index)

            self._processing_alter_audio_checkbox.setChecked(
                bool(snapshot.get("alter_audio", False))
            )
        finally:
            for widget in widgets:
                widget.blockSignals(False)
            self._suppress_processing_prefs_save = previous_suppress
        return True

    def _apply_processing_template(self, template_key: str) -> None:
        template = PROCESSING_TEMPLATES.get(template_key, PROCESSING_TEMPLATES["gaming_meme_black"])
        # Applying a template programmatically mutates several style widgets,
        # each of which fires the per-account save signal. Suppress those
        # cascaded saves so a template reset never overwrites the account's
        # stored preferences. A genuine user template pick still persists via
        # the template combo's own ``save`` connection, which fires after this
        # method returns (with the flag restored to its prior value).
        previous_suppress = self._suppress_processing_prefs_save
        self._suppress_processing_prefs_save = True
        try:
            self._set_processing_template(template_key)
            style_key = str(template.get("title_style", "clean_hook"))
            self._processing_title_style_combo.blockSignals(True)
            style_index = self._processing_title_style_combo.findData(style_key)
            self._processing_title_style_combo.setCurrentIndex(style_index if style_index >= 0 else 0)
            self._processing_title_style_combo.blockSignals(False)
            self._processing_title_font_size.setValue(int(template.get("font_size", 60)))
            font_index = self._processing_title_font_combo.findData(
                str(template.get("font_name", "arial_bold"))
            )
            self._processing_title_font_combo.setCurrentIndex(font_index if font_index >= 0 else 0)
            self._processing_title_color_input.setText(str(template.get("text_color", "#FFFFFF")))
            background_index = self._processing_title_background_combo.findData(
                str(template.get("background", "none"))
            )
            self._processing_title_background_combo.setCurrentIndex(
                background_index if background_index >= 0 else 0
            )
            layout_index = self._processing_title_layout_combo.findData(
                str(template.get("layout", "top_band"))
            )
            self._processing_title_layout_combo.setCurrentIndex(
                layout_index if layout_index >= 0 else 0
            )
            # Templates may pin the LLM title style (e.g. the bold-keyword
            # template wants the model to emit ``**word**`` markers). Only
            # override when the template specifies one so other templates leave
            # the user's choice be.
            prompt_title_style = template.get("prompt_title_style")
            if prompt_title_style is not None:
                prompt_index = self._processing_prompt_title_style_combo.findData(
                    str(prompt_title_style)
                )
                if prompt_index >= 0:
                    self._processing_prompt_title_style_combo.setCurrentIndex(prompt_index)
        finally:
            self._suppress_processing_prefs_save = previous_suppress

    def _current_template_enables_bold(self) -> bool:
        template_key = str(self._processing_template_combo.currentData() or "")
        template = PROCESSING_TEMPLATES.get(template_key, {})
        return bool(template.get("bold_keywords", False))

    def _processing_prompt_profile(self) -> str:
        template_key = str(self._processing_template_combo.currentData() or "gaming_meme_black")
        template = PROCESSING_TEMPLATES.get(template_key, PROCESSING_TEMPLATES["gaming_meme_black"])
        return str(template.get("prompt_profile", "broad_short_form"))

    def _processing_caption_style(self) -> str:
        return str(self._processing_caption_style_combo.currentData() or "contextual_info")

    def _processing_title_style(self) -> str | None:
        """Return the explicit title style, or None for Auto.

        Auto is the empty-string sentinel set on the dropdown; smart_drafts
        treats None as "fall back to caption-style-derived title rules" so
        the previous behavior is preserved when the user does not pick a
        specific title style.
        """
        value = self._processing_prompt_title_style_combo.currentData()
        if not value:
            return None
        return str(value)

    def _recent_smart_drafts_for_account(
        self,
        *,
        account_id: int | None,
        exclude_item_id: int | None,
        limit: int = 25,
    ) -> tuple[list[str], list[str]]:
        if account_id is None:
            return [], []
        titles: list[str] = []
        captions: list[str] = []
        with get_session() as session:
            query = (
                session.query(DownloadItem.title_draft, DownloadItem.caption_draft)
                .filter(DownloadItem.account_id == account_id)
                .filter(
                    (DownloadItem.title_draft.is_not(None))
                    | (DownloadItem.caption_draft.is_not(None))
                )
            )
            if exclude_item_id is not None:
                query = query.filter(DownloadItem.id != exclude_item_id)
            query = query.order_by(
                DownloadItem.smart_generated_at.desc(),
                DownloadItem.created_at.desc(),
            ).limit(limit)
            for title_draft, caption_draft in query.all():
                title_text = (title_draft or "").strip()
                caption_text = (caption_draft or "").strip()
                if title_text:
                    titles.append(title_text)
                if caption_text:
                    captions.append(caption_text)
        return titles, captions

    def _set_processing_manual_crop_ranges(self, probe: VideoProbe) -> None:
        # Allow deep crops: meme-format sources can have footage well past the
        # midline. Export-time output_dimensions still rejects impossible crops.
        max_vertical_crop = max(0, int(probe.height * 0.85))
        self._processing_top_crop_spin.setMaximum(max_vertical_crop)
        self._processing_bottom_crop_spin.setMaximum(max_vertical_crop)

    def _set_processing_manual_crop_values(self, top: int, bottom: int) -> None:
        self._processing_top_crop_spin.blockSignals(True)
        self._processing_bottom_crop_spin.blockSignals(True)
        self._processing_top_crop_spin.setValue(max(0, top))
        self._processing_bottom_crop_spin.setValue(max(0, bottom))
        self._processing_top_crop_spin.blockSignals(False)
        self._processing_bottom_crop_spin.blockSignals(False)

    def _has_manual_crop_override(self) -> bool:
        return (
            self._processing_top_crop_spin.value() > 0
            or self._processing_bottom_crop_spin.value() > 0
        )

    def _on_processing_manual_crop_changed(self) -> None:
        self._processing_using_ai_layout_crop = False
        self._refresh_processing_output_preview()

    def _should_use_title_replacement_crop(self, item: DownloadItem) -> bool:
        if str(self._processing_title_layout_combo.currentData() or "") != "top_band":
            return False
        if self._has_manual_crop_override():
            return False
        if "instagram.com" not in (item.source_url or "").lower():
            return False
        payload = self._processing_vision_payload
        if not isinstance(payload, dict):
            return True
        top_text_type = str(payload.get("top_text_type") or "none").strip().lower()
        return top_text_type in {"meme_joke", "source_title", "watermark", "channel_name", "none"}

    def _load_processing_style_state(self, item: DownloadItem) -> None:
        # Loading a video's style into the widgets is a programmatic refresh,
        # not a user edit, so suppress the per-account save signals throughout —
        # otherwise selecting a video would overwrite the account's stored
        # default style with that video's (or the gaming-meme fallback's) values.
        previous_suppress = self._suppress_processing_prefs_save
        self._suppress_processing_prefs_save = True
        try:
            title_preset = item.title_style_preset or "clean_hook"
            config: dict[str, object] = {}
            if item.title_style_config:
                try:
                    config = json.loads(item.title_style_config)
                except json.JSONDecodeError:
                    config = {}
                if not isinstance(config, dict):
                    config = {}

            # Template, caption-style, and prompt-title-style combos always
            # reflect the account's saved preferences so they never jump
            # unexpectedly when clicking between inbox videos.
            if not self._apply_processing_preferences_for_account(self._current_account_id):
                self._apply_processing_template("gaming_meme_black")

            if config:
                # Video has saved visual details — overlay font/size/colour/
                # background/layout on top of the account's combo settings.
                self._processing_title_font_size.setValue(
                    int(config.get("font_size", self._processing_title_font_size.value()))
                )
                font_index = self._processing_title_font_combo.findData(
                    str(config.get("font_name", self._processing_title_font_combo.currentData()))
                )
                self._processing_title_font_combo.setCurrentIndex(
                    font_index if font_index >= 0 else 0
                )
                self._processing_title_color_input.setText(
                    str(config.get("text_color", self._processing_title_color_input.text()))
                )
                background_index = self._processing_title_background_combo.findData(
                    str(config.get("background", self._processing_title_background_combo.currentData()))
                )
                self._processing_title_background_combo.setCurrentIndex(
                    background_index if background_index >= 0 else 0
                )
                layout_index = self._processing_title_layout_combo.findData(
                    str(config.get("layout", self._processing_title_layout_combo.currentData()))
                )
                self._processing_title_layout_combo.setCurrentIndex(
                    layout_index if layout_index >= 0 else 0
                )
            else:
                # No per-video style saved — account prefs already applied
                # above; additionally apply the item's title-style preset
                # visual settings if one is recorded.
                if item.title_style_preset:
                    self._apply_title_style_preset(title_preset)
        finally:
            self._suppress_processing_prefs_save = previous_suppress

    def _load_processing_smart_drafts(self, item: DownloadItem) -> None:
        self._processing_smart_summary_label.setText(
            item.smart_summary or "Smart draft summary will appear here after Groq generation."
        )
        title_options = self._parse_saved_options(item.smart_title_options)
        caption_options = self._parse_saved_options(item.smart_caption_options)
        recommendation = self._smart_recommendation_from_meta(
            item.smart_generation_meta,
            title_options=title_options,
            caption_options=caption_options,
        )
        self._set_processing_smart_recommendation(recommendation)
        self._set_processing_smart_options(
            title_options,
            caption_options,
            option_notes=recommendation.option_notes if recommendation else None,
            option_tiers=recommendation.option_tiers if recommendation else None,
            recommended_index=recommendation.title_index if recommendation else None,
        )
        self._set_processing_eval_state(
            provider_label=item.smart_provider_label,
            generation_meta=item.smart_generation_meta,
            vision_payload=item.smart_vision_payload,
            generated_at=item.smart_generated_at,
        )

    @staticmethod
    def _parse_saved_options(raw_value: str | None) -> list[str]:
        if not raw_value:
            return []
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if str(item).strip()]

    @staticmethod
    def _format_processing_eval_json(payload: object) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        except TypeError:
            return str(payload)

    @staticmethod
    def _parse_processing_vision_payload(raw_value: object) -> dict[str, object] | None:
        if isinstance(raw_value, dict):
            return raw_value
        if not isinstance(raw_value, str) or not raw_value.strip():
            return None
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _chat_prompt_section(label: str, value: str | None) -> str:
        cleaned = (value or "").strip()
        return f"{label}: {cleaned if cleaned else '(none)'}"

    @staticmethod
    def _truncate_chat_prompt_context(value: str, *, max_chars: int = 6000) -> str:
        cleaned = value.strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[:max_chars].rstrip() + "\n...[truncated for prompt]"

    def _processing_chat_prompt_style_contract(self) -> str:
        caption_style = self._processing_caption_style()
        title_style = self._processing_title_style()
        prompt_profile = self._processing_prompt_profile()
        profile_block = _profile_style_block(prompt_profile)
        # Niche-aware so the copied chat prompt matches what live generation
        # would do — history accounts auto-route to the history title rules.
        account = self._active_account()
        niche_label = account.niche_label if account else None
        title_rules = effective_title_rules(
            title_style, caption_style, niche_label, prompt_profile
        )
        title_rule_text = "\n".join(title_rules)

        return "\n".join(
            [
                "Style contract copied from NicheFlow smart drafts:",
                self._chat_prompt_section("Prompt profile", prompt_profile),
                self._chat_prompt_section("Profile voice", profile_block.get("style")),
                self._chat_prompt_section("Profile caption guidance", profile_block.get("caption")),
                self._chat_prompt_section("Caption word target", _caption_word_target(caption_style)),
                self._chat_prompt_section(
                    "Caption hashtag target", _caption_hashtag_target(caption_style)
                ),
                "Caption style rule:",
                _caption_style_line(caption_style),
                "",
                "Caption structure rule:",
                _caption_paragraph_rule(caption_style),
                "",
                "Title rules:",
                title_rule_text,
                "",
                "Quality bar:",
                "- Do not write tiny one-line captions unless the style contract explicitly asks for it.",
                "- Do not use generic filler hashtags like #fyp, #viral, #genz, or #explore as padding.",
                "- Make every option meaningfully different: different angle, different setup, different caption opener.",
                "- If the clip premise and visible video disagree, trust the visible video and mention the uncertainty.",
                "- Do not copy the source title or caption; transform the idea into this account's style.",
            ]
        )

    def _processing_visual_style_recommendation_contract(self) -> str:
        caption_style = self._processing_caption_style()
        prompt_profile = self._processing_prompt_profile()
        template_key = str(self._processing_template_combo.currentData() or "")
        if not (
            prompt_profile in {"cinema_study", "cinematic_study"}
            or caption_style == "cinema_hook"
            or template_key.startswith("cinema")
            or template_key.startswith("cinematic")
        ):
            return ""
        return "\n".join(
            [
                "Cinema visual style recommendation:",
                "- Recommend exactly one render style for each title option: Editorial Italic, Cinema Normal, or Bold Rounded.",
                "- Editorial Italic = use Cinematic Study / Cinematic Soft Italic. Best for twist scenes, emotional scenes, rewatch details, quiet dialogue, tragic reveals, and reflective movie moments.",
                "- Cinema Normal = use Cinema Normal / Cinema Georgia Clean. Best for calm re-watch content, character studies, understated observations, and scenes where restrained elegance beats drama.",
                "- Bold Rounded = use Cinema Viral Bold / Cinema Bold Rounded. Best for beautiful cinematography, visually stunning transitions, iconic shots, simple broad hooks, and high-readability viral discovery posts.",
                "- For a 3-post pilot, prefer: twist scene -> Editorial Italic; beautiful cinematography -> Bold Rounded; character/calm re-watch -> Cinema Normal.",
                "- Do not choose based on personal taste only; choose based on what the clip needs viewers to notice first: feeling/story = Editorial Italic, quiet elegance = Cinema Normal, visual spectacle/readability = Bold Rounded.",
            ]
        )

    def _processing_generation_chat_prompt(self, item: DownloadItem) -> str:
        path = Path(item.file_path or "").expanduser().resolve()
        account = self._active_account()
        account_voice = self._processing_account_voice_config(account)
        voice_lines = [
            self._chat_prompt_section("Tone", account_voice.get("tone")),
            self._chat_prompt_section("Target audience", account_voice.get("target_audience")),
            self._chat_prompt_section("Hook style", account_voice.get("hook_style")),
            self._chat_prompt_section("Banned phrases", account_voice.get("banned_phrases")),
            self._chat_prompt_section("Account title rules", account_voice.get("title_style")),
            self._chat_prompt_section("Account caption rules", account_voice.get("caption_style")),
            self._chat_prompt_section("Clip premise", account_voice.get("clip_context")),
        ]
        context_text = self._truncate_chat_prompt_context(
            self._processing_transcript_input.toPlainText()
        )
        title_style_label = self._processing_prompt_title_style_combo.currentText().strip()
        if self._processing_title_style() is None:
            title_style_label = "Auto (match caption style)"
        visual_style_contract = self._processing_visual_style_recommendation_contract()
        visual_style_task_lines = (
            [
                "- For each option, include one recommended render style: Editorial Italic, Cinema Normal, or Bold Rounded."
            ]
            if visual_style_contract
            else []
        )
        visual_style_return_lines = (
            ["Recommended Style 1:"]
            if visual_style_contract
            else []
        )
        visual_style_return_lines_2 = (
            ["Recommended Style 2:"]
            if visual_style_contract
            else []
        )
        visual_style_return_lines_3 = (
            ["Recommended Style 3:"]
            if visual_style_contract
            else []
        )

        return "\n".join(
            [
                "Please analyze this local NicheFlow video and generate Instagram-ready drafts.",
                "",
                "You can access the media directly from this local path:",
                f'"{path}"',
                f"File URL: {QUrl.fromLocalFile(str(path)).toString()}",
                "",
                "Account / niche context:",
                self._chat_prompt_section("Account", account.name if account else None),
                self._chat_prompt_section("Platform", account.platform if account else None),
                self._chat_prompt_section("Niche", account.niche_label if account else None),
                self._chat_prompt_section("Source title", item.title),
                self._chat_prompt_section("Source URL", item.source_url),
                self._chat_prompt_section("Source description", item.source_description),
                self._chat_prompt_section(
                    "Processing template",
                    self._processing_template_combo.currentText(),
                ),
                self._chat_prompt_section("Prompt profile", self._processing_prompt_profile()),
                self._chat_prompt_section(
                    "Caption style",
                    f"{self._processing_caption_style_combo.currentText()} "
                    f"({self._processing_caption_style()})",
                ),
                self._chat_prompt_section(
                    "Title style",
                    f"{title_style_label} ({self._processing_title_style() or 'auto'})",
                ),
                "",
                "Account voice fields:",
                *voice_lines,
                "",
                "Existing transcript / context from NicheFlow:",
                context_text or "(none)",
                "",
                self._processing_chat_prompt_style_contract(),
                visual_style_contract,
                "",
                "Task:",
                "- Inspect the local video file if possible before writing.",
                "- Generate 3 on-screen title options and 3 caption options.",
                "- After the 3 options, recommend the strongest title/caption pair for this account and explain why in 1-2 sentences.",
                "- Add a short selection note for each option so the user understands when to choose it.",
                *visual_style_task_lines,
                "- Follow the Style contract exactly; it overrides any generic caption habits.",
                "- Use the clip premise as guidance, but do not invent unsupported facts.",
                "- Keep the result ready to paste back into NicheFlow.",
                "",
                "Return format:",
                "Title Option 1:",
                *visual_style_return_lines,
                "Caption Option 1:",
                "Title Option 2:",
                *visual_style_return_lines_2,
                "Caption Option 2:",
                "Title Option 3:",
                *visual_style_return_lines_3,
                "Caption Option 3:",
                "Recommended Pick:",
                "Why:",
                "Selection Notes:",
            ]
        )

    def _on_copy_generation_chat_prompt_clicked(self) -> None:
        item = self._processing_selected_item()
        if item is None:
            self._notify("Select a downloaded video first.", Tone.WARNING)
            return
        if not item.file_path:
            self._notify("Selected video has no local file path yet.", Tone.WARNING)
            return
        QApplication.clipboard().setText(self._processing_generation_chat_prompt(item))
        self._notify("Copied chat-ready generation prompt.", Tone.SUCCESS)

    def _processing_context_text(self, item: DownloadItem, transcript_text: str) -> str:
        transcript_block = (
            transcript_text.strip()
            if transcript_text.strip()
            else "No speech transcript is available for this video yet."
        )
        return (
            f"Video title: {item.title or '(untitled)'}\n"
            f"Source URL: {item.source_url or '(unknown)'}\n\n"
            f"Speech / transcript context:\n{transcript_block}"
        )

    def _set_processing_smart_recommendation(
        self, recommendation: DraftRecommendation | None
    ) -> None:
        if recommendation is None or recommendation.title_index is None:
            self._processing_smart_recommendation_label.setText("")
            self._processing_smart_recommendation_label.setVisible(False)
            return
        title_number = recommendation.title_index + 1
        caption_number = (
            recommendation.caption_index + 1
            if recommendation.caption_index is not None
            else title_number
        )
        pick_text = (
            f"Recommended Pick: Title Option {title_number} + "
            f"Caption Option {caption_number}"
        )
        if recommendation.reason:
            pick_text = f"{pick_text}\nWhy: {recommendation.reason}"
        self._processing_smart_recommendation_label.setText(pick_text)
        self._processing_smart_recommendation_label.setVisible(True)

    @staticmethod
    def _valid_smart_option_index(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            index = int(value)
        except (TypeError, ValueError):
            return None
        if 0 <= index < SMART_DRAFT_OPTION_COUNT:
            return index
        if 1 <= index <= SMART_DRAFT_OPTION_COUNT:
            return index - 1
        return None

    def _smart_recommendation_from_payload(
        self,
        payload: dict,
        *,
        title_options: list[str],
        caption_options: list[str],
    ) -> DraftRecommendation | None:
        title_index = self._valid_smart_option_index(payload.get("recommended_title_index"))
        caption_index = self._valid_smart_option_index(payload.get("recommended_caption_index"))
        reason = str(payload.get("recommendation_reason") or "").strip() or None
        raw_notes = payload.get("option_notes") or []
        option_notes = [str(note).strip() for note in raw_notes if str(note).strip()]
        return self._normalize_smart_recommendation(
            title_index=title_index,
            caption_index=caption_index,
            reason=reason,
            option_notes=option_notes,
            option_tiers=self._clean_saved_option_tiers(payload.get("option_tiers")),
            title_options=title_options,
            caption_options=caption_options,
        )

    @staticmethod
    def _clean_saved_option_tiers(value: object) -> list[str] | None:
        """Keep only valid green/yellow/red tier words from a saved payload.

        Tiers are already normalized at generation time; this just guards
        against malformed persisted data. Returns None when nothing usable
        remains so the UI shows no badge rather than a wrong one."""
        if not isinstance(value, (list, tuple)):
            return None
        tiers = [
            str(item).strip().casefold()
            for item in value
            if str(item).strip().casefold() in {"green", "yellow", "red"}
        ]
        return tiers[:SMART_DRAFT_OPTION_COUNT] or None

    def _smart_recommendation_from_meta(
        self,
        raw_meta: str | None,
        *,
        title_options: list[str],
        caption_options: list[str],
    ) -> DraftRecommendation | None:
        if not raw_meta:
            return None
        try:
            meta = json.loads(raw_meta)
        except json.JSONDecodeError:
            return None
        if not isinstance(meta, dict):
            return None
        raw_notes = meta.get("option_notes") or []
        option_notes = [str(note).strip() for note in raw_notes if str(note).strip()]
        return self._normalize_smart_recommendation(
            title_index=self._valid_smart_option_index(
                meta.get("recommended_title_option_index")
            ),
            caption_index=self._valid_smart_option_index(
                meta.get("recommended_caption_option_index")
            ),
            reason=str(meta.get("recommendation_reason") or "").strip() or None,
            option_notes=option_notes,
            option_tiers=self._clean_saved_option_tiers(meta.get("option_tiers")),
            title_options=title_options,
            caption_options=caption_options,
        )

    def _normalize_smart_recommendation(
        self,
        *,
        title_index: int | None,
        caption_index: int | None,
        reason: str | None,
        option_notes: list[str],
        title_options: list[str],
        caption_options: list[str],
        option_tiers: list[str] | None = None,
    ) -> DraftRecommendation | None:
        max_options = max(len(title_options), len(caption_options))
        if max_options <= 0:
            return None
        if title_index is not None and title_index >= len(title_options):
            title_index = None
        if caption_index is not None and caption_index >= len(caption_options):
            caption_index = None
        if title_index is None and caption_index is not None:
            title_index = caption_index if caption_index < len(title_options) else None
        if caption_index is None and title_index is not None:
            caption_index = title_index if title_index < len(caption_options) else None
        if title_index is None and caption_index is None and not option_notes and not option_tiers:
            return None
        return DraftRecommendation(
            title_index=title_index,
            caption_index=caption_index,
            reason=reason,
            option_notes=option_notes[:SMART_DRAFT_OPTION_COUNT] or None,
            option_tiers=(option_tiers or None),
        )

    # On-screen risk badge for each title's hook tier. 🟢 green = no checkable
    # claim (auto-post safe), 🟡 yellow = grounded factual claim worth a glance,
    # 🔴 red = unverifiable overclaim. Mirrors the HOOK FRAMING prompt tiers.
    _OPTION_TIER_BADGES = {
        "green": "🟢 Green",
        "yellow": "🟡 Yellow",
        "red": "🔴 Red",
    }

    def _set_processing_smart_options(
        self,
        title_options: list[str],
        caption_options: list[str],
        *,
        option_notes: list[str] | None = None,
        option_tiers: list[str] | None = None,
        recommended_index: int | None = None,
    ) -> None:
        self._processing_smart_option_pairs = []
        max_options = max(
            len(title_options),
            len(caption_options),
            SMART_DRAFT_OPTION_COUNT if title_options or caption_options else 0,
        )
        for index in range(max_options):
            title_option = title_options[index] if index < len(title_options) else None
            caption_option = caption_options[index] if index < len(caption_options) else None
            self._processing_smart_option_pairs.append((title_option, caption_option))

        while len(self._processing_smart_option_pairs) < SMART_DRAFT_OPTION_COUNT:
            self._processing_smart_option_pairs.append((None, None))

        self._suppress_processing_text_dirty = True
        try:
            for index, button in enumerate(self._processing_smart_option_buttons):
                title_input = self._processing_smart_option_title_inputs[index]
                caption_input = self._processing_smart_option_caption_inputs[index]
                note_label = self._processing_smart_option_note_labels[index]
                button.blockSignals(True)
                button.setChecked(False)
                title_option, caption_option = self._processing_smart_option_pairs[index]
                note_text = (
                    option_notes[index].strip()
                    if option_notes and index < len(option_notes)
                    else ""
                )
                tier = (
                    option_tiers[index]
                    if option_tiers and index < len(option_tiers)
                    else None
                )
                badge = self._OPTION_TIER_BADGES.get(tier or "", "")
                if badge:
                    note_text = f"{badge} · {note_text}" if note_text else badge
                has_content = bool(title_option or caption_option)
                if has_content:
                    title_input.setText(title_option or "")
                    caption_input.setPlainText(caption_option or "")
                    note_label.setText(note_text)
                    note_label.setVisible(bool(note_text))
                    button.setEnabled(not self._processing_in_progress)
                    button.setText(
                        "Apply Recommended"
                        if recommended_index is not None and index == recommended_index
                        else f"Apply Option {index + 1}"
                    )
                else:
                    title_input.setText("")
                    caption_input.setPlainText("")
                    note_label.setText("")
                    note_label.setVisible(False)
                    button.setEnabled(False)
                    button.setText(f"Option {index + 1} unavailable")
                button.blockSignals(False)
        finally:
            self._suppress_processing_text_dirty = False

        if any(title or caption for title, caption in self._processing_smart_option_pairs):
            self._processing_smart_cards_status_label.setText(
                "Each card includes its own editable title and caption. Apply one option when it looks right."
            )
        else:
            self._processing_smart_cards_status_label.setText("")

    def _sync_processing_smart_option_pairs_from_inputs(self) -> None:
        self._processing_smart_option_pairs = [
            (
                self._processing_smart_option_title_inputs[index].text().strip() or None,
                self._caption_for_save(
                    self._processing_smart_option_caption_inputs[index].toPlainText()
                ),
            )
            for index in range(len(self._processing_smart_option_buttons))
        ]

    def _on_paste_smart_draft_clicked(self) -> None:
        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text.strip():
            self._notify(
                "Clipboard is empty — copy the generated draft first.", Tone.WARNING
            )
            return
        draft = parse_pasted_smart_draft(text)
        has_content = any(option.strip() for option in draft.title_options) or any(
            option.strip() for option in draft.caption_options
        )
        if not has_content:
            self._notify(
                "No 'Title Option' or 'Caption Option' sections found in the pasted text.",
                Tone.WARNING,
            )
            return
        option_count = max(
            len(draft.title_options),
            len(draft.caption_options),
            len(draft.option_notes),
            len(draft.recommended_styles),
        )
        display_notes: list[str] = []
        for index in range(option_count):
            style = (
                draft.recommended_styles[index]
                if index < len(draft.recommended_styles)
                else ""
            )
            note = draft.option_notes[index] if index < len(draft.option_notes) else ""
            if style and note:
                display_notes.append(f"Style: {style}. {note}")
            elif style:
                display_notes.append(f"Style: {style}.")
            else:
                display_notes.append(note)
        self._set_processing_smart_options(
            draft.title_options,
            draft.caption_options,
            option_notes=display_notes,
            recommended_index=draft.recommended_title_index,
        )
        caption_index = (
            draft.recommended_caption_index
            if draft.recommended_caption_index is not None
            else draft.recommended_title_index
        )
        recommendation = DraftRecommendation(
            title_index=draft.recommended_title_index,
            caption_index=caption_index,
            reason=draft.reason,
            option_notes=[note for note in display_notes if note] or None,
        )
        self._set_processing_smart_recommendation(recommendation)
        # Persist the pick + per-option notes through generation meta so a
        # reload rebuilds them exactly like a Groq-generated draft would.
        self._processing_generation_meta_text = json.dumps(
            {
                "recommended_title_option_index": draft.recommended_title_index,
                "recommended_caption_option_index": caption_index,
                "recommendation_reason": draft.reason,
                "option_notes": display_notes,
                "source": "manual_paste",
            },
            ensure_ascii=False,
        )
        item = self._processing_selected_item()
        if item is None:
            self._notify(
                "Pasted draft into the option cards. Select a downloaded video to save it.",
                Tone.INFO,
            )
            return
        self._persist_processing_draft_state(item.id)
        self._notify("Pasted draft saved. Apply the option you want to use.", Tone.SUCCESS)

    def _persist_processing_draft_state(self, item_id: int) -> None:
        self._sync_processing_smart_option_pairs_from_inputs()
        with get_session() as session:
            item_row = session.get(DownloadItem, item_id)
            if item_row is None:
                return
            item_row.title_draft = self._processing_title_draft_input.text().strip() or None
            item_row.caption_draft = self._caption_for_save(
                self._processing_caption_draft_input.toPlainText()
            )
            item_row.transcript_text = self._processing_raw_transcript_text.strip() or None
            item_row.smart_summary = self._processing_smart_summary_label.text().strip() or None
            item_row.smart_title_options = json.dumps(
                [
                    self._processing_smart_option_title_inputs[index].text().strip()
                    for index in range(len(self._processing_smart_option_pairs))
                    if self._processing_smart_option_title_inputs[index].text().strip()
                ],
                ensure_ascii=False,
            )
            item_row.smart_caption_options = json.dumps(
                [
                    self._processing_smart_option_caption_inputs[index].toPlainText()
                    for index in range(len(self._processing_smart_option_pairs))
                    if self._processing_smart_option_caption_inputs[index].toPlainText().strip()
                ],
                ensure_ascii=False,
            )
            item_row.smart_provider_label = self._processing_provider_label_text or None
            item_row.smart_generation_meta = self._processing_generation_meta_text or None
            item_row.smart_vision_payload = self._processing_vision_payload_text or None
            item_row.smart_generated_at = (
                dt.datetime.fromisoformat(self._processing_generated_at_text)
                if self._processing_generated_at_text
                else None
            )
            session.commit()
        if self._processing_dirty_item_id == item_id:
            self._processing_dirty_item_id = None

    def _set_processing_eval_state(
        self,
        *,
        provider_label: str | None = None,
        generation_meta: str | None = None,
        vision_payload: str | None = None,
        generated_at: dt.datetime | None = None,
    ) -> None:
        self._processing_provider_label_text = provider_label or ""
        self._processing_generation_meta_text = generation_meta or ""
        self._processing_vision_payload_text = vision_payload or ""
        self._processing_vision_payload = self._parse_processing_vision_payload(vision_payload)
        self._processing_generated_at_text = (
            generated_at.isoformat() if generated_at is not None else ""
        )

        label_parts: list[str] = []
        if provider_label:
            label_parts.append(provider_label)
        if generated_at is not None:
            label_parts.append(
                generated_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            )
        self._processing_eval_provider_label.setText(
            " | ".join(label_parts)
            if label_parts
            else "Provider metadata will appear here after smart generation."
        )
        self._processing_eval_meta_input.setPlainText(generation_meta or "")
        self._processing_eval_vision_input.setPlainText(vision_payload or "")
        self._refresh_processing_usage_label()

    def _refresh_processing_usage_label(self) -> None:
        profile = _groq_limit_profile()
        summary = self._processing_monthly_usage_summary()
        estimated_cost = self._processing_generation_meta_cost(
            self._processing_generation_meta_text
        )
        cost_text = f"${summary['cost']:.4f} / ${profile['monthly_budget_usd']:.2f}"
        count_text = f"{summary['count']} / {profile['monthly_video_cap']} videos"
        parts = [
            f"Month usage: {cost_text}",
            count_text,
            f"daily cap {profile['daily_video_cap']}",
            f"{profile['max_frames_per_video']} frames/video",
        ]
        if estimated_cost > 0:
            parts.append(f"selected estimate ${estimated_cost:.4f}")
        if summary["cost"] >= profile["budget_warn_at_usd"]:
            parts.append(f"warning threshold ${profile['budget_warn_at_usd']:.2f}")
        self._processing_usage_label.setText(" | ".join(parts))

    def _processing_monthly_usage_summary(self) -> dict[str, float | int]:
        now = dt.datetime.now(dt.timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total_cost = 0.0
        generated_count = 0
        with get_session() as session:
            rows = (
                session.query(DownloadItem.smart_generation_meta, DownloadItem.smart_generated_at)
                .filter(DownloadItem.smart_generation_meta.isnot(None))
                .all()
            )
        for raw_meta, generated_at in rows:
            normalized_generated_at = self._as_utc_datetime(generated_at)
            if normalized_generated_at is None or normalized_generated_at < month_start:
                continue
            cost = self._processing_generation_meta_cost(raw_meta)
            if cost <= 0:
                continue
            total_cost += cost
            generated_count += 1
        return {"cost": total_cost, "count": generated_count}

    @staticmethod
    def _as_utc_datetime(value: dt.datetime | None) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    @staticmethod
    def _processing_generation_meta_cost(raw_meta: str | None) -> float:
        if not raw_meta:
            return 0.0
        try:
            payload = json.loads(raw_meta)
        except json.JSONDecodeError:
            return 0.0
        if not isinstance(payload, dict):
            return 0.0
        raw_cost = payload.get("estimated_cost_usd")
        if isinstance(raw_cost, bool):
            return 0.0
        if isinstance(raw_cost, (int, float)):
            return max(0.0, float(raw_cost))
        return 0.0

    @staticmethod
    def _processing_generation_meta_errors(raw_meta: str | None) -> list[str]:
        if not raw_meta:
            return []
        try:
            payload = json.loads(raw_meta)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        raw_errors = payload.get("errors")
        if not isinstance(raw_errors, list):
            return []
        return [str(error) for error in raw_errors if str(error).strip()]

    def _smart_generation_budget_guard_message(self) -> str | None:
        profile = _groq_limit_profile()
        summary = self._processing_monthly_usage_summary()
        if summary["cost"] >= profile["monthly_budget_usd"]:
            return (
                f"Smart generation budget reached: ${summary['cost']:.4f} / "
                f"${profile['monthly_budget_usd']:.2f} this month."
            )
        if summary["count"] >= profile["monthly_video_cap"]:
            return (
                f"Smart generation monthly video cap reached: "
                f"{summary['count']} / {profile['monthly_video_cap']} videos."
            )
        return None

    def _title_style_config_payload(self) -> str:
        payload = {
            "template": self._processing_template_combo.currentData(),
            "prompt_profile": self._processing_prompt_profile(),
            "font_size": self._processing_title_font_size.value(),
            "font_name": str(self._processing_title_font_combo.currentData() or "segoe_ui"),
            "text_color": self._processing_title_color_input.text().strip() or "#FFFFFF",
            "background": self._processing_title_background_combo.currentData(),
            "layout": self._processing_title_layout_combo.currentData(),
        }
        return json.dumps(payload, sort_keys=True)

    def _processing_crop_settings(self) -> CropSettings:
        return CropSettings(
            left=self._processing_auto_crop.left,
            top=(
                self._processing_top_crop_spin.value()
                if self._processing_top_crop_spin.value() > 0
                else self._processing_auto_crop.top
            ),
            right=self._processing_auto_crop.right,
            bottom=(
                self._processing_bottom_crop_spin.value()
                if self._processing_bottom_crop_spin.value() > 0
                else self._processing_auto_crop.bottom
            ),
        )

    def _on_processing_title_layout_changed(self) -> None:
        if self._processing_applying_ai_layout_suggestion:
            return
        current_layout = str(self._processing_title_layout_combo.currentData() or "")
        if (
            self._processing_ai_suggested_layout is not None
            and current_layout != self._processing_ai_suggested_layout
        ):
            self._processing_using_ai_layout_crop = False
            self._processing_suggestion_label.setText(
                "Manual title layout override active. Export will run automatic crop detection for this layout."
            )
            self._refresh_processing_output_preview()

    def _apply_ai_layout_suggestion(self, vision_payload: object) -> None:
        if not isinstance(vision_payload, dict):
            return
        if self._processing_probe is None:
            return

        layout = str(vision_payload.get("suggested_title_layout") or "").strip()
        layout_applied = False
        if layout in {"no_title", "top_band", "overlay"}:
            layout_index = self._processing_title_layout_combo.findData(layout)
            if layout_index >= 0:
                try:
                    self._processing_applying_ai_layout_suggestion = True
                    self._processing_title_layout_combo.setCurrentIndex(layout_index)
                finally:
                    self._processing_applying_ai_layout_suggestion = False
                layout_applied = True
                self._processing_ai_suggested_layout = layout

        # The crop is decided at export time by the heuristic content-rectangle
        # detector (suggest_title_replacement_crop / detect_content_rectangle),
        # which is far more reliable than the vision content_box. Do NOT pre-fill
        # the crop fields from the vision payload here: that would register as a
        # manual crop override and block the heuristic from ever running.
        self._processing_using_ai_layout_crop = layout_applied

        reason = str(vision_payload.get("crop_reason") or "").strip()
        top_type = str(vision_payload.get("top_text_type") or "none")
        bottom_type = str(vision_payload.get("bottom_text_type") or "none")
        layout_label = self._processing_title_layout_combo.currentText()
        reason_suffix = f" {reason}" if reason else ""
        self._processing_suggestion_label.setText(
            f"AI layout suggestion: {layout_label}; top={top_type}, bottom={bottom_type}. "
            f"Crop is detected automatically on export.{reason_suffix}"
        )
        self._refresh_processing_output_preview()

    def _refresh_processing_output_preview(self) -> None:
        item = self._processing_selected_item()
        probe = self._processing_probe
        if item is None or probe is None:
            self._processing_preview_meta_label.setText(
                "Select a downloaded video to preview it here."
            )
            self._refresh_processing_latest_output_state(None)
            self._set_processing_controls_enabled(False)
            return

        try:
            width, height = output_dimensions(probe, self._processing_crop_settings())
        except ValueError as exc:
            self._processing_preview_meta_label.setText(str(exc))
            self._processing_summary_label.setText("Crop values need adjustment before export.")
            self._refresh_processing_latest_output_state(None)
            self._set_processing_controls_enabled(False)
            return

        output_path = self._new_processed_output_path_for_item(item)
        if output_path is None:
            output_path = self._processed_output_path_for_item(item)
        # Resolve the "Open Video" / "Add to Schedule" target strictly per-item.
        # Earlier this used the cross-account folder mtime fallback and an
        # ``or self._processing_last_output_path`` carryover, both of which
        # leaked another item's most-recent export into this item's panel and
        # made Open Video open the wrong file when switching between items.
        self._refresh_processing_latest_output_state(
            self._existing_processed_output_path_for_item(item)
        )
        preview_path = self._processing_preview_target_path(Path(item.file_path))
        preview_path_changed = self._processing_preview_path != preview_path
        self._processing_preview_path = preview_path
        if preview_path_changed or self._processing_preview_container is None:
            self._load_processing_preview(preview_path)
            self._processing_preview_position_slider.setValue(0)
        self._processing_preview_meta_label.setText(
            (
                f"{item.title or '(untitled)'} • "
                f"{probe.width} x {probe.height} • "
                f"{probe.duration_seconds:.2f}s • "
                f"Crop output: {width} x {height} • "
                f"{output_path.name}"
            )
        )
        if not self._processing_in_progress:
            self._processing_summary_label.setText(
                "The app will auto-crop this video and render your applied title into the export."
            )
        self._set_processing_controls_enabled(True)

    def _refresh_processing_latest_output_state(self, path: Path | None) -> None:
        if path is None or not path.exists():
            # Clear the cache too — without this, switching from an exported
            # item to a never-exported item leaves the previous item's path
            # behind, and the preview "output" mode / Open Video click can
            # still target a file that doesn't belong to the new selection.
            self._processing_last_output_path = None
            self._processing_latest_output_label.setText("No processed output yet in this session.")
            self._processing_open_latest_output_button.setEnabled(False)
            self._processing_open_latest_output_button.setVisible(False)
            self._processing_add_to_schedule_button.setEnabled(False)
            self._processing_add_to_schedule_button.setVisible(False)
            output_index = self._processing_preview_mode_combo.findData("output")
            if output_index >= 0:
                self._processing_preview_mode_combo.model().item(output_index).setEnabled(False)  # type: ignore[attr-defined]
                if self._processing_preview_mode == "output":
                    self._processing_preview_mode = "source"
                    self._processing_preview_mode_combo.blockSignals(True)
                    self._processing_preview_mode_combo.setCurrentIndex(0)
                    self._processing_preview_mode_combo.blockSignals(False)
            return

        self._processing_last_output_path = path
        self._processing_latest_output_label.setText(path.name)
        self._processing_open_latest_output_button.setEnabled(True)
        self._processing_open_latest_output_button.setVisible(True)
        self._processing_add_to_schedule_button.setEnabled(True)
        self._processing_add_to_schedule_button.setVisible(True)
        output_index = self._processing_preview_mode_combo.findData("output")
        if output_index >= 0:
            self._processing_preview_mode_combo.model().item(output_index).setEnabled(True)  # type: ignore[attr-defined]

    def _processing_preview_target_path(self, source_path: Path) -> Path:
        if (
            self._processing_preview_mode == "output"
            and self._processing_last_output_path is not None
            and self._processing_last_output_path.exists()
        ):
            return self._processing_last_output_path
        return source_path

    def _on_processing_preview_mode_changed(self) -> None:
        self._processing_preview_mode = str(
            self._processing_preview_mode_combo.currentData() or "source"
        )
        item = self._processing_selected_item()
        if item is None or not item.file_path:
            return
        preview_path = self._processing_preview_target_path(
            Path(item.file_path).expanduser().resolve()
        )
        self._processing_preview_path = preview_path
        self._load_processing_preview(preview_path)
        self._processing_toggle_preview_button.setText("Play Full Video")
        self._processing_preview_position_slider.setValue(0)

    def _on_processing_item_changed(self) -> None:
        self._selected_processing_item_id = self._processing_item_combo.currentData()
        # Drop the previously selected item's cached output path so the new
        # item starts from a clean slate — the refresh below resolves the
        # right path per-item, but this guards against any UI control that
        # reads the cache before the refresh completes.
        self._processing_last_output_path = None
        self._refresh_processing_selection()

    def _on_processing_inbox_filter_changed(self) -> None:
        if self._current_page != "processing":
            return
        self._refresh_processing_inbox(self._processing_available_items())

    def _on_processing_inbox_selection_changed(self) -> None:
        selected_items = self._processing_inbox_table.selectedItems()
        if not selected_items:
            return
        item_id = selected_items[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(item_id, int) or item_id == self._selected_processing_item_id:
            return
        self._selected_processing_item_id = item_id
        combo_index = self._processing_item_combo.findData(item_id)
        self._processing_item_combo.blockSignals(True)
        self._processing_item_combo.setCurrentIndex(combo_index if combo_index >= 0 else 0)
        self._processing_item_combo.blockSignals(False)
        self._processing_probe_item_id = None
        self._processing_last_output_path = None
        self._refresh_processing_selection()

    def _on_title_style_preset_changed(self) -> None:
        preset_key = self._processing_title_style_combo.currentData()
        if isinstance(preset_key, str):
            self._apply_title_style_preset(preset_key)
            self._processing_style_status_label.setText(
                "Applied the template title style."
            )

            self._processing_style_status_label.setText(
                "Applied the template title style."
            )

    def _on_processing_template_changed(self) -> None:
        template_key = self._processing_template_combo.currentData()
        if isinstance(template_key, str):
            self._apply_processing_template(template_key)
            self._processing_style_status_label.setText(
                "Applied the processing template."
            )

    def _start_suggest_crop_job(self, job: SuggestCropJobConfig) -> None:
        if self._processing_in_progress:
            self._notify("A processing task is already running.", Tone.WARNING)
            return

        self._processing_in_progress = True
        self._processing_busy_mode = "suggest"
        self._processing_progress_label.setText(
            "Analyzing the video for automatic crop suggestions..."
        )
        self._processing_progress_bar.setVisible(True)
        self._processing_progress_bar.setRange(0, 0)
        self._processing_progress_bar.setFormat("Analyzing...")
        self._show_activity_progress(
            "Analyzing the video for automatic crop suggestions...",
            maximum=0,
            fmt="Analyzing...",
        )
        self._set_processing_controls_enabled(False)

        thread = QThread(self)
        worker = SuggestCropWorker(job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_suggest_crop_completed)
        worker.failed.connect(self._on_suggest_crop_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._suggest_thread = thread
        self._suggest_worker = worker
        thread.start()

    def _start_transcript_draft_job(self, job: TranscriptDraftJobConfig) -> None:
        if self._processing_in_progress:
            self._notify("A processing task is already running.", Tone.WARNING)
            return

        self._processing_in_progress = True
        self._processing_busy_mode = "drafts"
        self._start_processing_loading_state("Generating drafts")
        self._processing_progress_label.setText(
            "Generate Drafts: step 1 of 2. Transcribing speech and preparing context..."
        )
        self._processing_progress_bar.setVisible(True)
        self._processing_progress_bar.setRange(0, 2)
        self._processing_progress_bar.setValue(1)
        self._processing_progress_bar.setFormat("Step 1/2: Transcribing")
        self._show_activity_progress(
            "Generate Drafts: step 1 of 2. Transcribing speech and preparing context...",
            maximum=2,
            value=1,
            text_visible=True,
            fmt="Step 1/2: Transcribing",
        )
        self._processing_draft_status_label.setText(
            "Transcribing audio first. If there is no speech, draft generation will fall back to metadata."
        )
        self._set_processing_controls_enabled(False)

        thread = QThread(self)
        worker = TranscriptDraftWorker(job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_transcript_draft_completed)
        worker.failed.connect(self._on_transcript_draft_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._draft_thread = thread
        self._draft_worker = worker
        thread.start()

    def _start_smart_draft_job(self, job: SmartDraftJobConfig) -> None:
        if self._processing_in_progress:
            self._notify("A processing task is already running.", Tone.WARNING)
            return

        self._processing_in_progress = True
        self._processing_busy_mode = "smart_drafts"
        self._start_processing_loading_state("Generating drafts")
        self._processing_progress_label.setText(
            "Generate Drafts: step 2 of 2. Generating title and caption options..."
        )
        self._processing_progress_bar.setVisible(True)
        self._processing_progress_bar.setRange(0, 2)
        self._processing_progress_bar.setValue(2)
        self._processing_progress_bar.setFormat("Step 2/2: Generating drafts")
        self._show_activity_progress(
            "Generate Drafts: step 2 of 2. Generating title and caption options...",
            maximum=2,
            value=2,
            text_visible=True,
            fmt="Step 2/2: Generating drafts",
        )
        if job.transcript_available:
            status_text = (
                "Using transcript context, metadata, and sampled video frames when supported "
                "to generate smarter title and caption options..."
            )
        else:
            status_text = (
                "Using visual-first generation from metadata and sampled video frames. "
                "Use transcription later only when spoken dialogue is important."
            )
        self._processing_draft_status_label.setText(status_text)
        self._set_processing_controls_enabled(False)

        thread = QThread(self)
        worker = SmartDraftWorker(job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_smart_draft_completed)
        worker.failed.connect(self._on_smart_draft_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._smart_draft_thread = thread
        self._smart_draft_worker = worker
        thread.start()

    def _start_processing_job(self, job: ProcessJobConfig) -> None:
        if self._processing_in_progress:
            self._notify("A processing export is already running.", Tone.WARNING)
            return

        self._processing_in_progress = True
        self._processing_busy_mode = "export"
        self._start_processing_loading_state("Rendering output")
        self._processing_progress_label.setText("Processing cropped video...")
        self._processing_progress_bar.setVisible(True)
        self._processing_progress_bar.setRange(0, 0)
        self._processing_progress_bar.setFormat("Rendering output...")
        self._show_activity_progress(
            "Processing cropped video...",
            maximum=0,
            fmt="Rendering output...",
        )
        self._set_processing_controls_enabled(False)

        thread = QThread(self)
        worker = ProcessWorker(job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_processing_completed)
        worker.failed.connect(self._on_processing_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._process_thread = thread
        self._process_worker = worker
        thread.start()

    def _finish_processing_job(self) -> None:
        self._processing_in_progress = False
        self._processing_busy_mode = None
        self._process_thread = None
        self._process_worker = None
        self._suggest_thread = None
        self._suggest_worker = None
        self._draft_thread = None
        self._draft_worker = None
        self._smart_draft_thread = None
        self._smart_draft_worker = None
        self._processing_progress_bar.setRange(0, 1)
        self._processing_progress_bar.setValue(0)
        self._processing_progress_bar.setFormat("")
        self._processing_progress_bar.setVisible(False)
        self._hide_activity_progress()
        self._stop_processing_loading_state()
        self._refresh_processing_output_preview()

    def _on_suggest_crop_completed(self, payload: dict) -> None:
        crop: CropSettings = payload["crop"]
        self._processing_auto_crop = crop

        self._finish_processing_job()
        reason_parts = list(payload.get("reasons") or ["automatic crop updated"])
        ocr_diagnostics = payload.get("ocr_diagnostics")
        if isinstance(ocr_diagnostics, PreprocessingOcrDiagnostics):
            reason_parts.append(self._format_preprocessing_ocr_summary(ocr_diagnostics))
        elif payload.get("ocr_diagnostics_error"):
            reason_parts.append("OCR diagnostics skipped after an OCR error")
        reason_text = "; ".join(reason_parts)
        ocr_unavailable = not (
            isinstance(ocr_diagnostics, PreprocessingOcrDiagnostics)
            and ocr_diagnostics.ffmpeg_available
            and ocr_diagnostics.tesseract_available
        )
        if not payload.get("used_ocr", False) and ocr_unavailable:
            reason_text = f"{reason_text}. Install Tesseract to add text-aware OCR suggestions."
        self._processing_suggestion_label.setText(reason_text)
        self._processing_progress_label.setText("Automatic crop suggestion applied.")
        pending_job = self._processing_pending_job
        if pending_job is not None:
            self._processing_pending_job = None
            auto_job = ProcessJobConfig(
                input_path=pending_job.input_path,
                output_path=pending_job.output_path,
                crop=self._processing_auto_crop,
                title_text=pending_job.title_text,
                title_font_size=pending_job.title_font_size,
                title_font_name=pending_job.title_font_name,
                title_color=pending_job.title_color,
                title_background=pending_job.title_background,
                title_layout=pending_job.title_layout,
                enable_bold_keywords=pending_job.enable_bold_keywords,
                watermark_replacement_text=pending_job.watermark_replacement_text,
                audio_mode=pending_job.audio_mode,
            )
            self._start_processing_job(auto_job)
            return
        self._notify("Applied automatic crop suggestion.", Tone.SUCCESS)

    def _on_suggest_crop_failed(self, message: str) -> None:
        self._finish_processing_job()
        self._processing_pending_job = None
        self._processing_progress_label.setText("Automatic crop suggestion failed.")
        self._hide_activity_bar_if_idle()
        self._notify(f"Automatic crop suggestion failed: {message}", Tone.ERROR)

    @staticmethod
    def _format_preprocessing_ocr_summary(
        diagnostics: PreprocessingOcrDiagnostics,
    ) -> str:
        if not diagnostics.ffmpeg_available or not diagnostics.tesseract_available:
            return "OCR diagnostics skipped because ffmpeg or Tesseract is unavailable"
        detected_regions: list[str] = []
        if diagnostics.top_text_detected:
            detected_regions.append("top")
        if diagnostics.bottom_text_detected:
            detected_regions.append("bottom")
        if not detected_regions:
            return f"OCR diagnostics sampled {diagnostics.sample_count} frames and found no top/bottom text"

        snippets = list(diagnostics.top_snippets[:2] + diagnostics.bottom_snippets[:2])
        snippet_text = ", ".join(f'"{snippet}"' for snippet in snippets[:3])
        confidence_text = (
            f" at {diagnostics.average_confidence:.1f}% average confidence"
            if diagnostics.average_confidence is not None
            else ""
        )
        return (
            f"OCR diagnostics detected {' and '.join(detected_regions)} text"
            f"{confidence_text}: {snippet_text}"
        )

    def _on_generate_text_drafts_clicked(self) -> None:
        item = self._processing_selected_item()
        if item is None or not item.file_path:
            self._notify("Select a downloaded video first.", Tone.WARNING)
            return
        if can_generate_smart_drafts():
            if self._try_start_followup_smart_drafts(
                transcript_text="",
                transcript_failure=(
                    "Visual-first generation skips speech transcription by default."
                ),
            ):
                return
        self._start_transcript_draft_job(
            TranscriptDraftJobConfig(
                input_path=Path(item.file_path),
                fallback_title=item.title,
            )
        )

    def _on_generate_smart_drafts_clicked(self) -> None:
        item = self._processing_selected_item()
        if item is None:
            self._notify("Select a downloaded video first.", Tone.WARNING)
            return
        guard_message = self._smart_generation_budget_guard_message()
        if guard_message is not None:
            self._refresh_processing_usage_label()
            self._notify(guard_message, Tone.WARNING)
            return

        transcript_text = self._processing_transcript_input.toPlainText().strip()
        if not transcript_text:
            self._notify("Generate text drafts first so the video has a transcript.", Tone.WARNING)
            return

        account = self._active_account()
        recent_titles, recent_captions = self._recent_smart_drafts_for_account(
            account_id=item.account_id,
            exclude_item_id=item.id,
        )
        self._start_smart_draft_job(
            SmartDraftJobConfig(
                transcript_text=transcript_text,
                source_title=item.title,
                niche_label=account.niche_label if account is not None else None,
                source_description=item.source_description,
                input_path=Path(item.file_path) if item.file_path else None,
                transcript_available=bool(transcript_text),
                account_voice=self._processing_account_voice_config(account),
                prompt_profile=self._processing_prompt_profile(),
                caption_style=self._processing_caption_style(),
                title_style=self._processing_title_style(),
                recent_titles=recent_titles,
                recent_captions=recent_captions,
            )
        )

    def _on_transcript_draft_completed(self, payload: dict) -> None:
        self._finish_processing_job()
        self._processing_raw_transcript_text = payload["transcript_text"]
        self._processing_title_draft_input.setText(payload["title_draft"])
        self._processing_caption_draft_input.setPlainText(payload["caption_draft"])
        item = self._processing_selected_item()
        if item is not None:
            self._processing_transcript_input.setPlainText(
                self._processing_context_text(item, self._processing_raw_transcript_text)
            )
        item = self._processing_selected_item()
        if item is not None:
            self._persist_processing_draft_state(item.id)
        if self._try_start_followup_smart_drafts(transcript_text=payload["transcript_text"]):
            return
        self._processing_draft_status_label.setText(
            "Generated drafts from the transcript. Review them, edit if needed, then save."
        )
        self._processing_progress_label.setText("Drafts generated.")
        self._hide_activity_bar_if_idle()
        self._notify("Generated transcript, title, and caption drafts.", Tone.SUCCESS)

    def _on_transcript_draft_failed(self, message: str) -> None:
        self._finish_processing_job()
        self._processing_raw_transcript_text = ""
        item = self._processing_selected_item()
        if item is not None:
            self._processing_transcript_input.setPlainText(self._processing_context_text(item, ""))
        if self._try_start_followup_smart_drafts(transcript_text="", transcript_failure=message):
            return
        self._processing_draft_status_label.setText("Could not generate drafts.")
        self._processing_progress_label.setText("Draft generation failed.")
        self._hide_activity_bar_if_idle()
        self._notify(f"Draft generation failed: {message}", Tone.ERROR)

    def _on_smart_draft_completed(self, payload: dict) -> None:
        self._finish_processing_job()
        title_options = [str(option) for option in payload.get("title_options") or []]
        caption_options = [str(option) for option in payload.get("caption_options") or []]
        provider_label = str(payload.get("provider_label") or "Smart generation")
        used_fallback = bool(payload.get("used_fallback"))
        generation_meta_text = self._format_processing_eval_json(payload.get("generation_meta"))
        vision_payload_text = self._format_processing_eval_json(payload.get("vision_payload"))
        self._processing_smart_summary_label.setText(payload.get("summary") or "(no summary)")
        self._set_processing_eval_state(
            provider_label=provider_label,
            generation_meta=generation_meta_text,
            vision_payload=vision_payload_text,
            generated_at=dt.datetime.now(dt.timezone.utc),
        )
        self._processing_vision_payload = self._parse_processing_vision_payload(
            payload.get("vision_payload")
        )
        self._apply_ai_layout_suggestion(payload.get("vision_payload"))
        recommendation = self._smart_recommendation_from_payload(
            payload,
            title_options=title_options,
            caption_options=caption_options,
        )
        self._set_processing_smart_recommendation(recommendation)
        apply_index = (
            recommendation.title_index
            if recommendation is not None and recommendation.title_index is not None
            else 0
        )
        self._set_processing_smart_options(
            title_options,
            caption_options,
            option_notes=recommendation.option_notes if recommendation else None,
            option_tiers=recommendation.option_tiers if recommendation else None,
            recommended_index=apply_index if title_options or caption_options else None,
        )

        if title_options:
            current_title = self._processing_title_draft_input.text().strip()
            title_was_manually_edited = (
                bool(current_title)
                and current_title not in self._processing_last_generated_titles
            )
            if not title_was_manually_edited:
                title_index = apply_index if apply_index < len(title_options) else 0
                self._processing_title_draft_input.setText(title_options[title_index])
            self._processing_last_generated_titles = list(title_options)
        if caption_options:
            caption_index = (
                recommendation.caption_index
                if recommendation is not None and recommendation.caption_index is not None
                else apply_index
            )
            if caption_index >= len(caption_options):
                caption_index = 0
            self._processing_caption_draft_input.setPlainText(caption_options[caption_index])
        if self._processing_smart_option_buttons and (title_options or caption_options):
            checked_index = apply_index if apply_index < len(self._processing_smart_option_buttons) else 0
            self._processing_smart_option_buttons[checked_index].setChecked(True)

        if used_fallback:
            errors = self._processing_generation_meta_errors(generation_meta_text)
            error_hint = f" Reason: {errors[0]}" if errors else ""
            self._processing_draft_status_label.setText(
                f"Generated drafts with {provider_label}. The result may be less grounded than the primary provider path.{error_hint}"
            )
            self._notify(
                f"Generated fallback drafts because the primary provider failed.{error_hint}",
                Tone.WARNING,
            )
        else:
            self._processing_draft_status_label.setText(
                f"Generated smart draft options with {provider_label} and applied the recommended title/caption."
            )
            self._notify(
                f"Generated smart title and caption options with {provider_label}.", Tone.SUCCESS
            )
        self._processing_progress_label.setText("Smart drafts generated.")
        item = self._processing_selected_item()
        if item is not None:
            self._persist_processing_draft_state(item.id)
        self._hide_activity_bar_if_idle()

    def _on_smart_draft_failed(self, message: str) -> None:
        self._finish_processing_job()
        self._processing_draft_status_label.setText("Could not generate smart drafts.")
        self._processing_progress_label.setText("Smart draft generation failed.")
        self._hide_activity_bar_if_idle()
        self._notify(f"Smart draft generation failed: {message}", Tone.ERROR)

    def _try_start_followup_smart_drafts(
        self,
        *,
        transcript_text: str,
        transcript_failure: str | None = None,
    ) -> bool:
        if not can_generate_smart_drafts():
            return False

        item = self._processing_selected_item()
        if item is None:
            return False
        guard_message = self._smart_generation_budget_guard_message()
        if guard_message is not None:
            self._refresh_processing_usage_label()
            self._processing_draft_status_label.setText(guard_message)
            self._notify(guard_message, Tone.WARNING)
            return False

        account = self._active_account()
        recent_titles, recent_captions = self._recent_smart_drafts_for_account(
            account_id=item.account_id,
            exclude_item_id=item.id,
        )
        transcript_available = bool(transcript_text.strip())
        if transcript_available:
            self._processing_progress_label.setText(
                "Generate Drafts: transcript ready. Moving to smart generation..."
            )
            self._processing_draft_status_label.setText(
                "Transcript context is ready. Generating smart title and caption options automatically..."
            )
        else:
            reason_text = (
                transcript_failure.strip() if transcript_failure else "No transcript was available."
            )
            self._processing_progress_label.setText(
                "Generate Drafts: using visual-first smart generation..."
            )
            self._processing_draft_status_label.setText(
                f"{reason_text} Using source metadata and sampled frames to generate title and caption options. "
                "Review the result against the exact visible moment before saving."
            )

        self._start_smart_draft_job(
            SmartDraftJobConfig(
                transcript_text=transcript_text.strip(),
                source_title=item.title,
                niche_label=account.niche_label if account is not None else None,
                source_description=item.source_description,
                input_path=Path(item.file_path) if item.file_path else None,
                transcript_available=transcript_available,
                account_voice=self._processing_account_voice_config(account),
                prompt_profile=self._processing_prompt_profile(),
                caption_style=self._processing_caption_style(),
                title_style=self._processing_title_style(),
                recent_titles=recent_titles,
                recent_captions=recent_captions,
            )
        )
        return True

    def _on_processing_smart_option_clicked(self, option_index: int) -> None:
        if option_index >= len(self._processing_smart_option_pairs):
            return
        self._sync_processing_smart_option_pairs_from_inputs()
        title_option = self._processing_smart_option_title_inputs[option_index].text().strip()
        caption_option = (
            self._processing_smart_option_caption_inputs[option_index].toPlainText().strip()
        )
        if not (title_option or caption_option):
            return
        for index, button in enumerate(self._processing_smart_option_buttons):
            button.blockSignals(True)
            button.setChecked(index == option_index)
            button.blockSignals(False)
        if title_option:
            self._processing_title_draft_input.setText(title_option)
        if caption_option:
            self._processing_caption_draft_input.setPlainText(caption_option)
        item = self._processing_selected_item()
        if item is not None:
            self._persist_processing_draft_state(item.id)

    def _on_save_text_drafts_clicked(self) -> None:
        item = self._processing_selected_item()
        if item is None:
            self._notify("Select a downloaded video first.", Tone.WARNING)
            return

        self._sync_processing_smart_option_pairs_from_inputs()
        with get_session() as session:
            item_row = session.get(DownloadItem, item.id)
            if item_row is None:
                self._notify("Could not find the selected video.", Tone.ERROR)
                return
            item_row.title_draft = self._processing_title_draft_input.text().strip() or None
            item_row.caption_draft = self._caption_for_save(
                self._processing_caption_draft_input.toPlainText()
            )
            item_row.transcript_text = self._processing_raw_transcript_text.strip() or None
            item_row.title_style_preset = self._processing_title_style_combo.currentData()
            item_row.title_style_config = self._title_style_config_payload()
            item_row.smart_summary = self._processing_smart_summary_label.text().strip() or None
            item_row.smart_title_options = json.dumps(
                [
                    title
                    for title, _caption in self._processing_smart_option_pairs
                    if isinstance(title, str) and title.strip()
                ],
                ensure_ascii=False,
            )
            item_row.smart_caption_options = json.dumps(
                [
                    caption
                    for _title, caption in self._processing_smart_option_pairs
                    if isinstance(caption, str) and caption.strip()
                ],
                ensure_ascii=False,
            )
            item_row.smart_provider_label = self._processing_provider_label_text or None
            item_row.smart_generation_meta = self._processing_generation_meta_text or None
            item_row.smart_vision_payload = self._processing_vision_payload_text or None
            item_row.smart_generated_at = (
                dt.datetime.fromisoformat(self._processing_generated_at_text)
                if self._processing_generated_at_text
                else None
            )
            session.commit()
        if self._processing_dirty_item_id == item.id:
            self._processing_dirty_item_id = None

        self._processing_draft_status_label.setText(
            "Saved text drafts and style settings for this video."
        )
        self._notify_and_refresh("Saved text drafts.", Tone.SUCCESS, preserve_status=True)

    def _on_process_video_clicked(self) -> None:
        item = self._processing_selected_item()
        if item is None or not item.file_path:
            self._notify("Select a downloaded video first.", Tone.WARNING)
            return

        try:
            input_path = Path(item.file_path)
            output_path = self._new_processed_output_path_for_item(item)
            if output_path is None:
                self._notify(
                    "Select a downloaded video with a local file before processing.",
                    Tone.WARNING,
                )
                return
            title_layout = str(self._processing_title_layout_combo.currentData() or "top_band")
            title_text = (
                None
                if title_layout == "no_title"
                else self._processing_title_draft_input.text().strip() or item.title or None
            )
            probe = probe_video(input_path)
            if self._should_use_title_replacement_crop(item):
                try:
                    replacement_crop = suggest_title_replacement_crop(input_path, probe)
                except Exception:  # noqa: BLE001
                    replacement_crop = CropSettings()
                if replacement_crop != CropSettings():
                    self._processing_auto_crop = replacement_crop
                    self._set_processing_manual_crop_values(
                        replacement_crop.top, replacement_crop.bottom
                    )
                    self._processing_using_ai_layout_crop = True
                    self._processing_suggestion_label.setText(
                        "Black Canvas replacement crop applied automatically: "
                        f"t{replacement_crop.top}/b{replacement_crop.bottom}/"
                        f"l{replacement_crop.left}/r{replacement_crop.right}px."
                    )
            pending_job = ProcessJobConfig(
                input_path=input_path,
                output_path=output_path,
                crop=self._processing_crop_settings(),
                title_text=title_text,
                title_font_size=self._processing_title_font_size.value(),
                title_font_name=str(self._processing_title_font_combo.currentData() or "segoe_ui"),
                title_color=self._processing_title_color_input.text().strip() or "#FFFFFF",
                title_background=str(
                    self._processing_title_background_combo.currentData() or "none"
                ),
                title_layout=title_layout,
                enable_bold_keywords=self._current_template_enables_bold(),
                watermark_replacement_text=self._processing_watermark_replacement_text(item),
                audio_mode=(
                    "alter"
                    if self._processing_alter_audio_checkbox.isChecked()
                    else "keep"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Could not start processing: {exc}", Tone.ERROR)
            return

        if self._processing_using_ai_layout_crop:
            self._start_processing_job(pending_job)
            return
        if self._has_manual_crop_override():
            self._start_processing_job(pending_job)
            return
        self._processing_pending_job = pending_job
        self._start_suggest_crop_job(SuggestCropJobConfig(input_path=Path(item.file_path)))

    def _on_toggle_processing_preview_clicked(self) -> None:
        if self._processing_preview_path is None:
            self._notify("Select a downloaded video first.", Tone.WARNING)
            return

        if self._processing_preview_timer.isActive():
            self._processing_preview_timer.stop()
            self._processing_toggle_preview_button.setText("Play Full Video")
            return

        if self._processing_preview_frame_iter is None:
            self._load_processing_preview(self._processing_preview_path)
        self._processing_preview_timer.start(1)
        self._processing_toggle_preview_button.setText("Pause Video")

    def _on_processing_preview_seek(self, position: int) -> None:
        self._seek_processing_preview(position)

    def _shift_processing_preview(self, delta_ms: int) -> None:
        duration = self._processing_effective_duration_ms()
        if duration <= 0:
            return
        was_playing = self._processing_preview_timer.isActive()
        next_position = max(0, min(self._processing_preview_position_ms + delta_ms, duration))
        self._seek_processing_preview(next_position)
        if was_playing:
            self._processing_preview_timer.start(1)
            self._processing_toggle_preview_button.setText("Pause Video")

    def _load_processing_preview(self, path: Path) -> None:
        self._stop_processing_preview()
        try:
            container = av.open(str(path))
            stream = container.streams.video[0]
        except Exception as exc:  # noqa: BLE001
            self._processing_preview_container = None
            self._processing_preview_stream = None
            self._processing_preview_frame_iter = None
            self._processing_preview_duration_ms = 0
            self._processing_preview_meta_label.setText(f"Could not open preview: {exc}")
            self._processing_video_widget.setText("Preview unavailable")
            return

        self._processing_preview_container = container
        self._processing_preview_stream = stream
        if stream.duration is not None and stream.time_base is not None:
            stream_duration_ms = int(float(stream.duration * stream.time_base) * 1000)
        else:
            stream_duration_ms = 0
        probe_duration_ms = (
            max(int(self._processing_probe.duration_seconds * 1000), 0)
            if self._processing_probe
            else 0
        )
        self._processing_preview_duration_ms = max(stream_duration_ms, probe_duration_ms)
        self._processing_preview_position_slider.setRange(
            0, max(self._processing_preview_duration_ms, 0)
        )
        self._seek_processing_preview(0)

    def _seek_processing_preview(self, position_ms: int) -> None:
        container = self._processing_preview_container
        stream = self._processing_preview_stream
        if container is None or stream is None:
            return
        was_playing = self._processing_preview_timer.isActive()
        self._processing_preview_timer.stop()
        if stream.time_base is not None:
            target_pts = int((position_ms / 1000) / float(stream.time_base))
            try:
                container.seek(max(target_pts, 0), stream=stream, any_frame=False, backward=True)
            except Exception:  # noqa: BLE001
                pass
        self._processing_preview_frame_iter = container.decode(video=0)
        self._processing_preview_position_ms = max(position_ms, 0)
        self._processing_preview_last_frame_ms = None
        self._render_processing_frame_at_or_after(position_ms / 1000)
        if was_playing:
            self._processing_preview_timer.start(1)
            self._processing_toggle_preview_button.setText("Pause Video")
        else:
            self._processing_toggle_preview_button.setText("Play Full Video")

    def _render_processing_frame_at_or_after(self, target_seconds: float) -> None:
        if self._processing_preview_frame_iter is None:
            return
        for frame in self._processing_preview_frame_iter:
            frame_time = float(frame.time) if frame.time is not None else 0.0
            if frame_time + 0.001 < target_seconds:
                continue
            self._display_processing_frame(frame)
            self._processing_preview_position_ms = int(frame_time * 1000)
            self._processing_preview_position_slider.setValue(self._processing_preview_position_ms)
            self._processing_preview_time_label.setText(
                f"{self._format_media_time(self._processing_preview_position_ms)} / {self._format_media_time(self._processing_effective_duration_ms())}"
            )
            return

    def _advance_processing_preview(self) -> None:
        if self._processing_preview_frame_iter is None:
            self._processing_preview_timer.stop()
            self._processing_toggle_preview_button.setText("Play Full Video")
            return
        try:
            frame = next(self._processing_preview_frame_iter)
        except StopIteration:
            self._processing_preview_timer.stop()
            self._processing_toggle_preview_button.setText("Play Full Video")
            return
        self._display_processing_frame(frame)
        frame_time = float(frame.time) if frame.time is not None else 0.0
        previous_frame_ms = self._processing_preview_last_frame_ms
        self._processing_preview_position_ms = int(frame_time * 1000)
        self._processing_preview_last_frame_ms = self._processing_preview_position_ms
        self._processing_preview_position_slider.setValue(self._processing_preview_position_ms)
        self._processing_preview_time_label.setText(
            f"{self._format_media_time(self._processing_preview_position_ms)} / {self._format_media_time(self._processing_effective_duration_ms())}"
        )
        delay_ms = self._processing_next_frame_delay(previous_frame_ms)
        self._processing_preview_timer.start(delay_ms)

    def _processing_next_frame_delay(self, previous_frame_ms: int | None) -> int:
        if previous_frame_ms is None:
            return 16
        frame_delta = self._processing_preview_position_ms - previous_frame_ms
        if frame_delta <= 0:
            return 16
        return max(16, min(frame_delta, 80))

    def _display_processing_frame(self, frame) -> None:  # noqa: ANN001
        rgb_frame = frame.to_rgb()
        array = rgb_frame.to_ndarray()
        height, width, _channels = array.shape
        bytes_per_line = width * 3
        image = QImage(
            array.tobytes(),
            width,
            height,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        )
        pixmap = QPixmap.fromImage(image.copy())
        scaled = pixmap.scaled(
            self._processing_video_widget.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._processing_video_widget.setPixmap(scaled)
        self._processing_video_widget.setText("")

    def _stop_processing_preview(self) -> None:
        self._processing_preview_timer.stop()
        if self._processing_preview_container is not None:
            try:
                self._processing_preview_container.close()
            except Exception:  # noqa: BLE001
                pass
        self._processing_preview_container = None
        self._processing_preview_stream = None
        self._processing_preview_frame_iter = None
        self._processing_preview_duration_ms = 0
        self._processing_preview_position_ms = 0
        self._processing_preview_last_frame_ms = None

    def _processing_effective_duration_ms(self) -> int:
        probe_duration = (
            max(int(self._processing_probe.duration_seconds * 1000), 0)
            if self._processing_probe
            else 0
        )
        return max(self._processing_preview_duration_ms, probe_duration)

    @staticmethod
    def _format_media_time(milliseconds: int) -> str:
        total_seconds = max(int(milliseconds / 1000), 0)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _on_processing_completed(self, payload: dict) -> None:
        self._finish_processing_job()
        output_path = Path(payload["output_path"])
        self._processing_last_output_path = output_path
        # Persist the new processed_path BEFORE refreshing the preview. The
        # refresh reads item.processed_path back from the DB to resolve the
        # "Open Video" target; if we refresh first, it sees the previous
        # export's path, clobbers _processing_last_output_path, and the
        # Open Video button ends up opening the prior file instead of the
        # one we just produced.
        self._save_processed_output_for_selected_item(output_path)
        self._refresh_processing_output_preview()
        if payload.get("watermark_replaced"):
            detected = str(payload.get("watermark_detected_text") or "detected watermark")
            replacement = str(payload.get("watermark_replacement_text") or "account handle")
            self._processing_progress_label.setText(
                f"Processing complete. Replaced {detected} with {replacement}."
            )
        else:
            self._processing_progress_label.setText("Processing complete.")
        self._notify(f"Processed video saved to {output_path.name}.", Tone.SUCCESS)

    def _save_processed_output_for_selected_item(self, output_path: Path) -> None:
        item_id = self._selected_processing_item_id
        if item_id is None:
            return
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is None:
                return
            item.processed_path = str(output_path)
            session.commit()
        # Keep ``_displayed_items`` in sync so the next per-item resolution
        # (e.g. _existing_processed_output_path_for_item, called immediately
        # after this from _refresh_processing_output_preview) sees the new
        # path. Without this, the cached DownloadItem still reports its old
        # ``processed_path`` and Open Video / the latest-output label keep
        # pointing at the previous export.
        for cached in self._displayed_items:
            if cached.id == item_id:
                cached.processed_path = str(output_path)
                break

    def _processing_watermark_replacement_text(self, item: DownloadItem) -> str | None:
        account_id = item.account_id or self._current_account_id
        if account_id is None:
            return None
        with get_session() as session:
            account = session.get(Account, account_id)
            if account is None:
                return None
            if (account.platform or "").casefold() != "instagram":
                return None
            handle = (account.login_identifier or "").strip()
            if not handle:
                return None
            return handle if handle.startswith("@") else f"@{handle}"

    def _on_processing_failed(self, message: str) -> None:
        self._finish_processing_job()
        self._processing_progress_label.setText("Processing failed.")
        self._notify(f"Processing failed: {message}", Tone.ERROR)

    @staticmethod
    def _safe_account_folder_name(account: Account | None) -> str:
        raw_name = (account.name if account is not None else "").strip()
        if not raw_name:
            return "unassigned"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._-")
        return safe_name or "unassigned"

    def _processed_output_dir_for_account_id(self, account_id: int | None) -> Path:
        if account_id is None:
            return processed_dir() / "unassigned"
        with get_session() as session:
            account = session.get(Account, account_id)
            return processed_dir() / self._safe_account_folder_name(account)

    def _processed_output_dir_for_current_account(self) -> Path:
        return self._processed_output_dir_for_account_id(self._current_account_id)

    def _processed_output_path_for_item(self, item: DownloadItem) -> Path | None:
        if not item.file_path:
            return None
        return processed_output_path(
            Path(item.file_path),
            self._processed_output_dir_for_account_id(item.account_id),
        )

    def _new_processed_output_path_for_item(self, item: DownloadItem) -> Path | None:
        if not item.file_path:
            return None
        output_dir = self._processed_output_dir_for_account_id(item.account_id)
        return self._next_incremental_processed_output_path(output_dir)

    def _next_incremental_processed_output_path(self, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        numbered_ids: list[int] = []
        for path in output_dir.glob("reel_*.mp4"):
            match = re.fullmatch(r"reel_(\d+)\.mp4", path.name)
            if match:
                numbered_ids.append(int(match.group(1)))

        if numbered_ids:
            next_id = max(numbered_ids) + 1
        else:
            next_id = sum(1 for path in output_dir.glob("*.mp4") if path.is_file()) + 1

        while True:
            candidate = output_dir / f"reel_{next_id:03d}.mp4"
            if not candidate.exists():
                return candidate
            next_id += 1

    def _latest_numbered_processed_output_path_for_account_id(
        self,
        account_id: int | None,
    ) -> Path | None:
        output_dir = self._processed_output_dir_for_account_id(account_id)
        candidates = [
            path for path in output_dir.glob("reel_*.mp4") if path.is_file()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _existing_processed_output_path_for_item(
        self,
        item: DownloadItem,
        *,
        allow_latest_numbered_fallback: bool = False,
    ) -> Path | None:
        if not item.file_path:
            return None
        if item.processed_path:
            processed_path = Path(item.processed_path)
            if processed_path.exists():
                return processed_path
        with get_session() as session:
            job = (
                session.query(UploadJob)
                .filter(UploadJob.download_item_id == item.id)
                .order_by(UploadJob.created_at.desc())
                .first()
            )
            if job is not None:
                processed_path = Path(job.processed_path)
                if processed_path.exists():
                    return processed_path
        paths = [
            processed_output_path(
                Path(item.file_path),
                self._processed_output_dir_for_account_id(item.account_id),
            ),
            processed_output_path(Path(item.file_path), processed_dir()),
        ]
        existing_path = next((path for path in paths if path.exists()), None)
        if existing_path is not None:
            return existing_path
        if allow_latest_numbered_fallback:
            return self._latest_numbered_processed_output_path_for_account_id(item.account_id)
        return None

    def _on_open_processed_folder_clicked(self) -> None:
        path = self._processed_output_dir_for_current_account()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))
            self._notify("Opened the processed videos folder.", Tone.INFO)
        except OSError as exc:
            self._notify(f"Could not open the processed folder: {exc}", Tone.ERROR)

    def _on_open_latest_processed_output_clicked(self) -> None:
        # Always re-resolve from the currently selected item at click time so a
        # stale ``_processing_last_output_path`` (left over from another item or
        # an interim refresh) can't open the wrong file. Query the DB directly
        # by id so this also works before ``_displayed_items`` is populated.
        # Fall back to the cached path only when no item is selected.
        path: Path | None = None
        item = self._processing_selected_item()
        if item is None and self._selected_processing_item_id is not None:
            with get_session() as session:
                item = session.get(DownloadItem, self._selected_processing_item_id)
        if item is not None:
            path = self._existing_processed_output_path_for_item(item)
        if path is None:
            path = self._processing_last_output_path
        if path is None or not path.exists():
            self._notify("No processed output is available to open yet.", Tone.WARNING)
            return
        try:
            os.startfile(str(path))
            self._notify("Opening the latest processed output.", Tone.INFO)
        except OSError as exc:
            self._notify(f"Could not open the processed output: {exc}", Tone.ERROR)

    def _on_add_processed_to_schedule_clicked(self) -> None:
        item = self._processing_selected_item()
        if item is None:
            self._notify("Select a processed video first.", Tone.WARNING)
            return

        output_path = self._processing_last_output_path
        if (output_path is None or not output_path.exists()) and item.file_path:
            output_path = self._existing_processed_output_path_for_item(
                item,
                allow_latest_numbered_fallback=True,
            )
        if output_path is None or not output_path.exists():
            self._notify("Process the video before adding it to the schedule.", Tone.WARNING)
            return
        if item.account_id is None:
            self._notify("Assign the video to an account before scheduling it.", Tone.WARNING)
            return

        title = self._processing_title_draft_input.text().strip() or item.title or None
        description = self._caption_for_save(
            self._processing_caption_draft_input.toPlainText()
        )
        with get_session() as session:
            account = session.get(Account, item.account_id)
            if account is None:
                self._notify("The selected account no longer exists.", Tone.WARNING)
                return
            scheduled_at = self._next_upload_slot(account.upload_schedule_slots)
            status = "scheduled" if scheduled_at is not None else "draft"
            timezone_label = account.upload_timezone or "Asia/Bangkok"
            privacy_status = account.upload_default_privacy or "private"
            job = UploadJob(
                account_id=account.id,
                download_item_id=item.id,
                processed_path=str(output_path),
                title=title,
                description=description,
                scheduled_at=scheduled_at,
                timezone=timezone_label,
                privacy_status=privacy_status,
                made_for_kids=int(account.upload_made_for_kids or 0),
                contains_synthetic_media=int(account.upload_contains_synthetic_media or 0),
                status=status,
            )
            session.add(job)
            session.flush()
            self._last_created_schedule_job_id = job.id
            session.commit()

        self._refresh_schedule_page()
        self._notify_and_refresh("Added latest processed export to the publish queue.", Tone.SUCCESS)

    def _set_current_page(self, page_name: str) -> None:
        if page_name not in MODULE_PAGES:
            return
        self._current_page = page_name
        page_index = MODULE_PAGES.index(page_name)
        self._workspace_stack.setCurrentIndex(page_index)
        self._workspace_stack.updateGeometry()
        self._workspace_content.updateGeometry()
        self._refresh_runtime_fields()
        self._sync_sidebar_selection()
        self._sync_account_panel_visibility()
        self._apply_refresh(force=True, preserve_status=True)
        self._sync_workspace_page_size()
        self._reset_workspace_scroll_to_top()
        if page_name == "pooling":
            self._refresh_pooling_page()

    def _reset_workspace_scroll_to_top(self) -> None:
        self._set_workspace_scroll_to_top_now()
        if self._current_page == "processing":
            return
        for delay_ms in (0, 25, 100):
            QTimer.singleShot(delay_ms, self._set_workspace_scroll_to_top_now)

    def _set_workspace_scroll_to_top_now(self) -> None:
        try:
            self._scroll_area.verticalScrollBar().setValue(0)
        except RuntimeError:
            return

    def _sync_workspace_page_size(self) -> None:
        if self._current_page == "scraping" and self._workspace_content.isVisible():
            height = max(self._scroll_area.viewport().height(), 1)
            for widget in (
                self._scraping_page,
                self._workspace_stack,
                self._workspace_content,
                self._scroll_area.widget(),
            ):
                if widget is not None:
                    widget.setFixedHeight(height)
            self._resize_scrape_tabs_height()
            self._scroll_area.verticalScrollBar().setValue(0)
            return

        if self._current_page == "session_health" and self._workspace_content.isVisible():
            height = max(self._scroll_area.viewport().height(), 1)
            for widget in (
                self._session_health_page,
                self._workspace_stack,
                self._workspace_content,
                self._scroll_area.widget(),
            ):
                if widget is not None:
                    widget.setFixedHeight(height)
            self._scroll_area.verticalScrollBar().setValue(0)
            return

        if self._current_page == "uploads" and self._workspace_content.isVisible():
            height = max(self._scroll_area.viewport().height(), 1)
            for widget in (
                self._uploads_page,
                self._workspace_stack,
                self._workspace_content,
                self._scroll_area.widget(),
            ):
                if widget is not None:
                    widget.setFixedHeight(height)
            self._resize_schedule_table_height()
            self._scroll_area.verticalScrollBar().setValue(0)
            return

        if self._current_page == "downloads" and self._workspace_content.isVisible():
            height = max(
                self._downloads_page.sizeHint().height(),
                self._scroll_area.viewport().height(),
            )
            for widget in (
                self._downloads_page,
                self._workspace_stack,
                self._workspace_content,
                self._scroll_area.widget(),
            ):
                if widget is not None:
                    widget.setFixedHeight(height)
            self._scroll_area.verticalScrollBar().setValue(0)
            return

        if self._current_page == "processing":
            height = max(
                self._processing_page.sizeHint().height(),
                self._scroll_area.viewport().height() + 260,
                1080,
            )
            for widget in (
                self._processing_page,
                self._workspace_stack,
                self._workspace_content,
                self._scroll_area.widget(),
            ):
                if widget is not None:
                    widget.setFixedHeight(height)
            return

        for widget in (
            self._scraping_page,
            self._processing_page,
            self._downloads_page,
            self._uploads_page,
            self._session_health_page,
            self._workspace_stack,
            self._workspace_content,
            self._scroll_area.widget(),
        ):
            if widget is not None:
                widget.setMinimumHeight(0)
                widget.setMaximumHeight(16777215)
                widget.updateGeometry()

    def _resize_scrape_tabs_height(self) -> None:
        if not hasattr(self, "_scrape_intake_panel"):
            return

        layout = self._scrape_intake_panel.layout()
        margins = layout.contentsMargins()
        spacing = layout.spacing()

        # Walk every layout item above the tabs, summing real heights plus one
        # spacing per item (each is separated from the next, and the last from
        # the tabs, by `spacing`). The previous implementation collapsed the
        # five "fixed" widgets into a single block, under-reserving spacing
        # and causing the bottom of the tab content to clip below the viewport.
        reserved_height = margins.top() + margins.bottom()
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            if widget is self._scrape_tabs:
                continue
            if widget is not None and not widget.isVisible():
                continue
            reserved_height += item.sizeHint().height() + spacing

        target_height = max(
            420,
            self._scroll_area.viewport().height() - reserved_height,
        )
        self._scrape_tabs.setFixedHeight(target_height)

    def _sync_sidebar_selection(self) -> None:
        for page_name, button in self._module_buttons.items():
            is_selected = page_name == self._current_page
            button.setChecked(is_selected)
            button.setProperty("selected", is_selected)
            button.style().unpolish(button)
            button.style().polish(button)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._position_toast()
        if hasattr(self, "_scroll_area"):
            self._sync_workspace_page_size()

    def _position_toast(self) -> None:
        if not self._toast_label.isVisible():
            return
        self._toast_label.adjustSize()
        x = max(20, self.width() - self._toast_label.width() - 24)
        y = 22
        self._toast_label.move(x, y)
        self._toast_label.raise_()

    def _set_tone(self, widget: QWidget, tone: str) -> None:
        widget.setProperty("tone", tone)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _set_status(self, message: str, tone: str = Tone.INFO) -> None:
        self._status_label.setText(message)
        self._set_tone(self._status_label, tone)
        self._activity_status_label.setText(message)
        self._set_tone(self._activity_bar, tone)

    def _show_activity_progress(
        self,
        message: str,
        *,
        minimum: int = 0,
        maximum: int = 1,
        value: int = 0,
        text_visible: bool = False,
        fmt: str = "",
        tone: str = Tone.INFO,
    ) -> None:
        self._activity_bar.setVisible(True)
        self._activity_status_label.setText(message)
        self._set_tone(self._activity_bar, tone)
        self._activity_progress_bar.setVisible(True)
        self._activity_progress_bar.setTextVisible(text_visible)
        self._activity_progress_bar.setMinimum(minimum)
        self._activity_progress_bar.setMaximum(maximum)
        self._activity_progress_bar.setValue(value)
        self._activity_progress_bar.setFormat(fmt)

    def _hide_activity_progress(self) -> None:
        self._activity_progress_bar.setRange(0, 1)
        self._activity_progress_bar.setValue(0)
        self._activity_progress_bar.setFormat("")
        self._activity_progress_bar.setVisible(False)

    def _hide_activity_bar_if_idle(self) -> None:
        if (
            self._scrape_in_progress
            or self._instagram_discover_in_progress
            or self._processing_in_progress
        ):
            return
        self._hide_activity_progress()
        self._activity_bar.setVisible(False)

    def _refresh_runtime_fields(self) -> None:
        if not hasattr(self, "_runtime_fields"):
            return
        current_data_dir = data_dir()
        self._runtime_fields["data_dir"].setText(str(current_data_dir))
        self._runtime_fields["db_path"].setText(str(current_data_dir / "nicheflow.db"))
        self._runtime_fields["downloads_dir"].setText(str(downloads_dir()))
        self._runtime_fields["processed_dir"].setText(str(processed_dir()))
        self._runtime_fields["logs_dir"].setText(str(logs_dir()))
        self._runtime_fields["backups_dir"].setText(str(backups_dir()))
        latest_backup = self._latest_backup_path()
        if latest_backup is None:
            self._use_latest_backup_button.setEnabled(False)
        else:
            self._use_latest_backup_button.setEnabled(True)
            if not self._restore_backup_input.text().strip():
                self._restore_backup_input.setText(str(latest_backup))

    def _show_toast(self, message: str, tone: str = Tone.INFO) -> None:
        self._toast_label.setText(message)
        self._set_tone(self._toast_label, tone)
        self._toast_label.setVisible(True)
        self._position_toast()
        self._toast_timer.start(2600)

    def _notify(self, message: str, tone: str = Tone.INFO) -> None:
        self._set_status(message, tone)
        self._show_toast(message, tone)

    def _notify_and_refresh(
        self,
        message: str,
        tone: str = Tone.INFO,
        *,
        force: bool = True,
        preserve_status: bool = True,
    ) -> None:
        self._notify(message, tone)
        self._apply_refresh(force=force, preserve_status=preserve_status)

    def _hide_toast(self) -> None:
        self._toast_label.setVisible(False)

    def _create_runtime_backup(self) -> Path:
        backup_root = backups_dir()
        backup_root.mkdir(parents=True, exist_ok=True)
        source_root = data_dir()
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_root / f"nicheflow-backup-{timestamp}.zip"

        with zipfile.ZipFile(backup_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_root.rglob("*")):
                if not path.is_file():
                    continue
                if backup_root in path.parents:
                    continue
                archive.write(path, arcname=path.relative_to(source_root))

        return backup_path

    def _latest_backup_path(self) -> Path | None:
        candidates = sorted(
            backups_dir().glob("nicheflow-backup-*.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _restore_runtime_backup(self, backup_path: Path) -> None:
        resolved_backup = backup_path.expanduser().resolve()
        if not resolved_backup.exists():
            raise FileNotFoundError(f"Backup zip not found: {resolved_backup}")
        if not zipfile.is_zipfile(resolved_backup):
            raise ValueError("Selected file is not a valid backup zip.")

        runtime_root = data_dir()
        backup_root = backups_dir().resolve()

        reset_db_state()

        runtime_root.mkdir(parents=True, exist_ok=True)
        for path in runtime_root.iterdir():
            resolved_path = path.resolve()
            if resolved_path == backup_root:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

        with zipfile.ZipFile(resolved_backup) as archive:
            archive.extractall(runtime_root)

        init_db()

    def _on_open_data_folder_clicked(self) -> None:
        path = data_dir()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))
            self._notify("Opened the runtime data folder.", Tone.INFO)
        except OSError as exc:
            self._notify(f"Could not open the data folder: {exc}", Tone.ERROR)

    def _on_export_backup_clicked(self) -> None:
        try:
            backup_path = self._create_runtime_backup()
        except OSError as exc:
            self._notify(f"Could not create backup: {exc}", Tone.ERROR)
            return

        self._backup_summary_label.setText(f"Latest backup: {backup_path}")
        self._notify(f"Created backup zip at {backup_path}.", Tone.SUCCESS)

    def _on_use_latest_backup_clicked(self) -> None:
        latest_backup = self._latest_backup_path()
        if latest_backup is None:
            self._notify("No backup zip is available yet.", Tone.WARNING)
            return
        self._restore_backup_input.setText(str(latest_backup))
        self._notify("Loaded the latest backup path.", Tone.INFO)

    def _on_restore_backup_clicked(self) -> None:
        backup_value = self._restore_backup_input.text().strip()
        if not backup_value:
            self._notify("Enter a backup zip path first.", Tone.WARNING)
            return

        try:
            self._restore_runtime_backup(Path(backup_value))
        except (FileNotFoundError, ValueError, OSError) as exc:
            self._notify(f"Could not restore backup: {exc}", Tone.ERROR)
            return

        self._backup_summary_label.setText(
            f"Restored backup: {Path(backup_value).expanduser().resolve()}"
        )
        self._refresh_runtime_fields()
        self._refresh_account_controls()
        self._show_account_main()
        self._apply_refresh(force=True)
        self._notify("Restored runtime backup.", Tone.SUCCESS)

    @staticmethod
    def _parse_source_urls(raw_value: str | None) -> list[str]:
        if raw_value is None:
            return []

        normalized = raw_value.replace("\n", ",").replace(";", ",")
        return [part.strip() for part in normalized.split(",") if part.strip()]

    @staticmethod
    def _parse_keyword_phrases(raw_value: str | None) -> list[str]:
        if raw_value is None:
            return []

        normalized = raw_value.replace("\n", ",").replace(";", ",")
        return [part.strip() for part in normalized.split(",") if part.strip()]

    @staticmethod
    def _parse_optional_positive_int(raw_value: str, field_name: str) -> int | None:
        value = raw_value.strip()
        if not value:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number.") from exc
        if parsed < 1:
            raise ValueError(f"{field_name} must be at least 1.")
        return parsed

    @staticmethod
    def _parse_optional_nonnegative_int(raw_value: str, field_name: str) -> int | None:
        value = raw_value.strip()
        if not value:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number.") from exc
        if parsed < 0:
            raise ValueError(f"{field_name} cannot be negative.")
        return parsed

    @staticmethod
    def _discovery_weights(account: Account | None) -> DiscoveryWeights:
        return DiscoveryWeights(
            views=account.ranking_weight_views if account and account.ranking_weight_views else 35,
            likes=account.ranking_weight_likes if account and account.ranking_weight_likes else 20,
            recency=account.ranking_weight_recency
            if account and account.ranking_weight_recency
            else 25,
            keyword_match=(
                account.ranking_weight_keyword_match
                if account and account.ranking_weight_keyword_match
                else 20
            ),
        )

    def _account_scrape_config(
        self,
        account: Account | None,
    ) -> tuple[list[Source], list[str], int, int | None, str, int, int, int, DiscoveryWeights]:
        if account is None:
            return (
                [],
                [],
                20,
                None,
                "review_only",
                3,
                0,
                0,
                DiscoveryWeights(),
            )

        keywords = self._parse_keyword_phrases(account.discovery_keywords)
        max_items = account.scrape_max_items or 20
        max_age_days = account.scrape_max_age_days
        discovery_mode = account.discovery_mode or "review_only"
        auto_queue_limit = account.auto_queue_limit or 3
        min_view_count = account.min_view_count or 0
        min_like_count = account.min_like_count or 0
        return (
            self._load_sources_for_account(account.id),
            keywords,
            max_items,
            max_age_days,
            discovery_mode,
            auto_queue_limit,
            min_view_count,
            min_like_count,
            self._discovery_weights(account),
        )

    def _load_sources_for_account(self, account_id: int) -> list[Source]:
        with get_session() as session:
            sources = (
                session.query(Source)
                .filter(Source.account_id == account_id)
                .order_by(Source.priority.asc(), Source.label.asc(), Source.id.asc())
                .all()
            )
        return sources

    def _current_selected_source(self) -> Source | None:
        source_id = self._selected_source_id
        if source_id is None:
            source_id = self._source_id_for_current_row()
        if source_id is None:
            return None
        return next(
            (source for source in self._displayed_sources if source.id == source_id),
            None,
        )

    def _source_id_for_current_row(self) -> int | None:
        if not hasattr(self, "_source_table"):
            return None
        row = self._source_table.currentRow()
        if row < 0:
            return None
        for column in range(self._source_table.columnCount()):
            item = self._source_table.item(row, column)
            if item is None:
                continue
            source_id = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(source_id, int):
                return source_id
        return None

    @staticmethod
    def _normalize_source_for_platform(
        source_url: str,
        platform: str,
    ) -> tuple[str | None, str | None]:
        if platform == "instagram":
            return normalize_instagram_source_url(source_url)
        return normalize_youtube_source_url(source_url)

    @staticmethod
    def _infer_source_type_for_platform(source_url: str, platform: str) -> str:
        if platform == "instagram":
            return infer_instagram_source_type(source_url)
        return infer_youtube_source_type(source_url)

    @staticmethod
    def _source_label_for_platform(source_url: str, platform: str) -> str:
        if platform == "instagram":
            return instagram_source_label(source_url)
        return source_url.rstrip("/").rsplit("/", 1)[-1] or source_url

    def _ensure_source_rows(
        self,
        *,
        account_id: int,
        platform: str,
        source_urls: list[str],
    ) -> None:
        with get_session() as session:
            existing_sources = {
                source.source_url: source
                for source in session.query(Source).filter(Source.account_id == account_id).all()
            }
            changed = False
            for source_url in source_urls:
                if source_url in existing_sources:
                    continue
                session.add(
                    Source(
                        account_id=account_id,
                        platform=platform,
                        source_type=self._infer_source_type_for_platform(source_url, platform),
                        label=self._source_label_for_platform(source_url, platform),
                        source_url=source_url,
                        enabled=1,
                        priority=100,
                    )
                )
                changed = True
            if changed:
                session.commit()

    def _refresh_candidate_action_state(self) -> None:
        candidate = self._current_selected_candidate()
        selected_source = self._current_selected_source()
        workspace_enabled = self._current_account_id is not None
        scrape_controls_enabled = (
            workspace_enabled
            and not self._scrape_in_progress
            and not self._instagram_discover_in_progress
        )
        instagram_discover_enabled = (
            not self._scrape_in_progress and not self._instagram_discover_in_progress
        )
        can_queue = workspace_enabled and candidate is not None and candidate.state != "queued"
        can_ignore = workspace_enabled and candidate is not None and candidate.state != "ignored"
        can_restore = workspace_enabled and candidate is not None and candidate.state == "ignored"
        can_scrape_selected = (
            scrape_controls_enabled
            and selected_source is not None
            and bool(selected_source.enabled)
        )
        self._scrape_button.setEnabled(
            scrape_controls_enabled and any(source.enabled for source in self._displayed_sources)
        )
        self._scrape_selected_button.setEnabled(can_scrape_selected)
        self._scrape_source_input.setEnabled(scrape_controls_enabled)
        self._scrape_add_source_button.setEnabled(scrape_controls_enabled)
        self._instagram_discover_source_combo.setEnabled(instagram_discover_enabled)
        self._instagram_result_limit_input.setEnabled(instagram_discover_enabled)
        self._instagram_discover_min_likes_input.setEnabled(instagram_discover_enabled)
        self._candidate_sort_combo.setEnabled(workspace_enabled)
        self._candidate_sort_direction_combo.setEnabled(workspace_enabled)
        instagram_source_selected = self._instagram_discover_source_combo.currentData() is not None
        self._instagram_discover_button.setEnabled(
            instagram_discover_enabled and instagram_source_selected
        )
        self._instagram_archive_button.setEnabled(
            instagram_discover_enabled and instagram_source_selected
        )
        self._source_table.setEnabled(workspace_enabled)
        self._source_filter.setEnabled(workspace_enabled)
        self._source_sort.setEnabled(workspace_enabled)
        self._source_remove_button.setEnabled(
            scrape_controls_enabled and selected_source is not None
        )
        self._source_toggle_button.setEnabled(
            scrape_controls_enabled and selected_source is not None
        )
        if selected_source is None:
            self._source_toggle_button.setText("Disable")
        else:
            self._source_toggle_button.setText("Disable" if selected_source.enabled else "Enable")
        self._candidate_table.setEnabled(workspace_enabled)
        self._candidate_state_filter.setEnabled(workspace_enabled)
        self._candidate_source_filter.setEnabled(workspace_enabled)
        self._candidate_queue_button.setText(self._candidate_queue_button_text(candidate))
        self._candidate_queue_button.setEnabled(can_queue)
        self._candidate_ignore_button.setEnabled(can_ignore)
        self._candidate_restore_button.setEnabled(can_restore)
        self._candidate_action_hint.setText(self._candidate_action_hint_text(candidate))

    def _refresh_download_batch_action_state(self) -> None:
        selected_ids = self._selected_item_ids()
        workspace_enabled = self._current_account_id is not None
        has_selection = workspace_enabled and bool(selected_ids)
        self._batch_keep_button.setEnabled(has_selection)
        self._batch_ignore_button.setEnabled(has_selection)
        self._batch_return_button.setEnabled(has_selection)

    def _candidate_action_hint_text(self, candidate: ScrapeCandidate | None) -> str:
        if self._current_account_id is None:
            return "Select an account to review intake candidates."
        if candidate is None:
            if not self._displayed_candidates:
                selected_source = self._current_selected_source()
                if selected_source is not None:
                    return "No candidates yet. Go to Sources and click Scrape Selected."
                if self._displayed_sources:
                    return "No candidates yet. Select a source, then add candidates from it."
            return "Select a candidate to review it."

        normalized_state = "candidate" if candidate.state == "new" else candidate.state
        if normalized_state == "candidate":
            return "Ready to review. Queue it for download or ignore it for now."
        if normalized_state == "queued":
            return "Already queued for download. Wait for the download to finish or remove the history row to reopen it."
        if normalized_state == "downloaded":
            return (
                "Already in this account library. Queue it again here if you want to redownload it."
            )
        if normalized_state == "ignored":
            return "Ignored for now. Return it to review if you want to reconsider it."
        return "Select a candidate to review it."

    def _matches_filters(self, item: DownloadItem) -> bool:
        query = self._search_input.text().strip().lower()
        status_filter = self._status_filter.currentText()
        review_filter = self._review_filter.currentData()
        if self._current_account_id is None:
            return False
        if item.account_id != self._current_account_id:
            return False

        if status_filter != "All statuses" and item.status != status_filter:
            return False
        if review_filter not in {None, "all"} and item.review_state != review_filter:
            return False

        if not query:
            return True

        account_name = item.account.name if item.account is not None else ""
        haystacks = [
            item.title or "",
            item.source_url,
            account_name,
            item.extractor or "",
            item.video_id or "",
        ]
        return any(query in value.lower() for value in haystacks)

    def _item_exists(self, item: DownloadItem) -> bool:
        return bool(item.file_path) and Path(item.file_path).exists()

    def _file_info_text(self, item: DownloadItem) -> str:
        if not item.file_path:
            return "No local file yet."

        path = Path(item.file_path)
        if not path.exists():
            return "Missing from disk."

        size_bytes = path.stat().st_size
        size_kib = size_bytes / 1024
        return f"Present on disk, {size_kib:.1f} KiB"

    def _file_size_text(self, item: DownloadItem) -> str:
        if not item.file_path:
            return "-"

        path = Path(item.file_path)
        if not path.exists():
            return "Missing"

        size_bytes = path.stat().st_size
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GiB"
        if size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MiB"
        return f"{size_bytes / 1024:.1f} KiB"

    @staticmethod
    def _next_upload_slot(schedule_slots: str | None) -> dt.datetime | None:
        if not schedule_slots:
            return None

        now = dt.datetime.now().astimezone()
        candidates: list[dt.datetime] = []
        for raw_slot in schedule_slots.replace("\n", ",").replace(";", ",").split(","):
            slot = raw_slot.strip()
            if not slot:
                continue
            try:
                hour_text, minute_text = slot.split(":", 1)
                hour = int(hour_text)
                minute = int(minute_text)
                if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                    continue
            except ValueError:
                continue
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += dt.timedelta(days=1)
            candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates)

    @staticmethod
    def _upload_scheduled_text(job: UploadJob) -> str:
        if job.scheduled_at is None:
            return "(unscheduled)"
        scheduled_at = job.scheduled_at
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=dt.timezone.utc)
        timezone_text = f" {job.timezone}" if job.timezone else ""
        return f"{scheduled_at.astimezone().strftime('%Y-%m-%d %H:%M')}{timezone_text}"

    @staticmethod
    def _upload_status_text(job: UploadJob) -> str:
        if job.status == "failed":
            return "Failed"
        if not Path(job.processed_path).exists():
            return "Missing output"
        if job.posted_at is not None:
            return "Posted"
        if job.status == "posted":
            return "Posted"
        if job.status == "uploaded":
            return "Posted"
        if job.status == "uploading":
            return "Uploading"
        if job.status == "ready":
            return "Ready"
        if job.status == "skipped":
            return "Skipped"
        if job.status == "scheduled":
            return "Scheduled"
        return "Draft"

    @staticmethod
    def _datetime_to_qdatetime(value: dt.datetime) -> QDateTime:
        local_value = value.astimezone() if value.tzinfo is not None else value
        return QDateTime(
            QDate(local_value.year, local_value.month, local_value.day),
            QTime(local_value.hour, local_value.minute),
        )

    @staticmethod
    def _job_schedule_local_datetime(job: UploadJob | None) -> dt.datetime:
        if job is None or job.scheduled_at is None:
            now = dt.datetime.now().astimezone()
            return (now + dt.timedelta(hours=1)).replace(second=0, microsecond=0)
        scheduled_at = job.scheduled_at
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=dt.timezone.utc)
        return scheduled_at.astimezone().replace(second=0, microsecond=0)

    def _selected_schedule_job_id(self) -> int | None:
        selected_items = self._schedule_table.selectedItems()
        if not selected_items:
            return None
        job_id = selected_items[0].data(Qt.ItemDataRole.UserRole)
        return int(job_id) if job_id is not None else None

    def _on_schedule_selection_changed(self) -> None:
        has_selection = self._selected_schedule_job_id() is not None
        self._schedule_copy_caption_button.setEnabled(has_selection)
        self._schedule_open_output_button.setEnabled(has_selection)
        self._schedule_copy_path_button.setEnabled(has_selection)
        self._schedule_open_folder_button.setEnabled(has_selection)
        self._schedule_instagram_assist_button.setEnabled(has_selection)
        self._schedule_auto_publish_button.setEnabled(
            has_selection
            and not self._publish_in_progress
            and not self._publish_batch_active
        )
        self._schedule_status_combo.setEnabled(has_selection)
        self._schedule_datetime_edit.setEnabled(has_selection)
        self._schedule_save_time_button.setEnabled(has_selection)
        self._schedule_clear_time_button.setEnabled(has_selection)
        self._refresh_schedule_caption_preview()
        self._refresh_schedule_status_combo()
        self._refresh_schedule_time_editor()

    def _selected_schedule_job(self) -> UploadJob | None:
        job_id = self._selected_schedule_job_id()
        if job_id is None:
            return None
        with get_session() as session:
            job = (
                session.query(UploadJob)
                .options(joinedload(UploadJob.download_item))
                .filter(UploadJob.id == job_id)
                .one_or_none()
            )
            if job is None:
                return None
            try:
                session.expunge(job)
            except Exception:  # noqa: BLE001
                pass
            return job

    def _take_preferred_schedule_selection_id(self, fallback_job_id: int | None) -> int | None:
        if self._last_created_schedule_job_id is None:
            return fallback_job_id
        preferred_job_id = self._last_created_schedule_job_id
        self._last_created_schedule_job_id = None
        return preferred_job_id

    def _schedule_caption_text_for_job(self, job: UploadJob) -> str:
        title = (job.title or "").strip()
        caption = (job.description or "").strip()
        mode = str(self._schedule_caption_combo.currentData() or "caption")
        if mode == "title":
            return title
        if mode == "title_caption":
            return "\n\n".join(part for part in (title, caption) if part)
        return caption or title

    def _refresh_schedule_caption_preview(self) -> None:
        if not hasattr(self, "_schedule_caption_preview"):
            return
        job = self._selected_schedule_job()
        if job is None:
            self._schedule_caption_preview.clear()
            return
        self._schedule_caption_preview.setPlainText(self._schedule_caption_text_for_job(job))

    def _refresh_schedule_status_combo(self) -> None:
        if not hasattr(self, "_schedule_status_combo"):
            return
        job = self._selected_schedule_job()
        self._schedule_status_combo.blockSignals(True)
        if job is None:
            self._schedule_status_combo.setCurrentIndex(0)
        else:
            status = "posted" if job.posted_at is not None else (job.status or "draft")
            index = self._schedule_status_combo.findData(status)
            self._schedule_status_combo.setCurrentIndex(index if index >= 0 else 0)
        self._schedule_status_combo.blockSignals(False)

    def _refresh_schedule_time_editor(self) -> None:
        if not hasattr(self, "_schedule_datetime_edit"):
            return
        job = self._selected_schedule_job()
        self._schedule_datetime_edit.blockSignals(True)
        self._schedule_datetime_edit.setDateTime(
            self._datetime_to_qdatetime(self._job_schedule_local_datetime(job))
        )
        self._schedule_datetime_edit.blockSignals(False)

    def _on_schedule_status_combo_changed(self) -> None:
        job_id = self._selected_schedule_job_id()
        if job_id is None:
            return
        status = str(self._schedule_status_combo.currentData() or "draft")
        with get_session() as session:
            job = session.get(UploadJob, job_id)
            if job is None:
                return
            job.status = status
            # Only the PublishWorker should stamp posted_at (so the daily cap
            # only counts automation-posted jobs). Manually changing the status
            # to "posted" via the combo leaves posted_at untouched; changing
            # away from "posted" clears it so re-queueing works correctly.
            if status != "posted":
                job.posted_at = None
            job.error_message = None
            session.commit()
        self._refresh_schedule_page()
        self._notify("Updated publish status.", Tone.SUCCESS)

    def _on_save_schedule_time_clicked(self) -> None:
        job_id = self._selected_schedule_job_id()
        if job_id is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return

        selected_datetime = self._schedule_datetime_edit.dateTime().toPyDateTime()
        scheduled_at = selected_datetime.astimezone(dt.timezone.utc)
        with get_session() as session:
            job = session.get(UploadJob, job_id)
            if job is None:
                self._notify("The selected publish job no longer exists.", Tone.WARNING)
                self._refresh_schedule_page()
                return
            job.scheduled_at = scheduled_at
            if job.status in {None, "", "draft", "ready"}:
                job.status = "scheduled"
            job.error_message = None
            session.commit()

        self._refresh_schedule_page()
        self._notify("Updated scheduled time.", Tone.SUCCESS)

    def _on_clear_schedule_time_clicked(self) -> None:
        job_id = self._selected_schedule_job_id()
        if job_id is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return

        with get_session() as session:
            job = session.get(UploadJob, job_id)
            if job is None:
                self._notify("The selected publish job no longer exists.", Tone.WARNING)
                self._refresh_schedule_page()
                return
            job.scheduled_at = None
            if job.status == "scheduled":
                job.status = "draft"
            job.error_message = None
            session.commit()

        self._refresh_schedule_page()
        self._notify("Cleared scheduled time.", Tone.SUCCESS)

    def _on_auto_schedule_clicked(self) -> None:
        if self._current_account_id is None:
            self._notify("Select an account workspace first.", Tone.WARNING)
            return
        now_local = dt.datetime.now().astimezone()
        with get_session() as session:
            account = session.get(Account, self._current_account_id)
            if account is None:
                self._notify("Account no longer exists.", Tone.WARNING)
                return
            jobs = (
                session.query(UploadJob)
                .filter(
                    UploadJob.account_id == account.id,
                    UploadJob.posted_at.is_(None),
                    UploadJob.scheduled_at.is_(None),
                    UploadJob.status.in_(["draft", "ready"]),
                )
                .order_by(UploadJob.created_at.asc())
                .all()
            )
            if not jobs:
                self._notify("No unscheduled reels to schedule.", Tone.INFO)
                return
            times = upcoming_slot_times(account.upload_schedule_slots, len(jobs), after=now_local)
            if not times:
                self._notify(
                    "Set this account's schedule slots first (e.g. 09:00, 18:00).",
                    Tone.WARNING,
                )
                return
            timezone_label = account.upload_timezone
            for job, when in zip(jobs, times):
                job.scheduled_at = when.astimezone(dt.timezone.utc)
                job.status = "scheduled"
                if timezone_label:
                    job.timezone = timezone_label
                job.error_message = None
            scheduled_count = min(len(jobs), len(times))
            session.commit()
        self._refresh_schedule_page()
        self._notify(f"Auto-scheduled {scheduled_count} reel(s) with randomized times.", Tone.SUCCESS)

    def _on_copy_schedule_caption_clicked(self) -> None:
        job = self._selected_schedule_job()
        if job is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return

        QApplication.clipboard().setText(self._schedule_caption_text_for_job(job))
        self._notify("Copied caption.", Tone.SUCCESS)

    def _on_copy_schedule_path_clicked(self) -> None:
        job = self._selected_schedule_job()
        if job is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return
        QApplication.clipboard().setText(str(Path(job.processed_path)))
        self._notify("Copied video path.", Tone.SUCCESS)

    def _on_open_schedule_folder_clicked(self) -> None:
        job = self._selected_schedule_job()
        if job is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return
        output_path = Path(job.processed_path)
        folder = output_path.parent
        if not folder.exists():
            self._notify("The output folder is missing.", Tone.ERROR)
            return
        os.startfile(str(folder))
        self._notify("Opened output folder.", Tone.SUCCESS)

    def _on_open_schedule_output_clicked(self) -> None:
        job_id = self._selected_schedule_job_id()
        if job_id is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return

        with get_session() as session:
            job = session.get(UploadJob, job_id)
            if job is None:
                self._notify("The selected publish job no longer exists.", Tone.WARNING)
                self._refresh_schedule_page()
                return
            output_path = Path(job.processed_path)

        if not output_path.exists():
            self._notify("Reel output file is missing.", Tone.ERROR)
            return

        os.startfile(str(output_path))
        self._notify("Opened reel output.", Tone.SUCCESS)

    def _on_open_instagram_assist_clicked(self) -> None:
        job_id = self._selected_schedule_job_id()
        if job_id is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return

        with get_session() as session:
            job = (
                session.query(UploadJob)
                .options(joinedload(UploadJob.account))
                .filter(UploadJob.id == job_id)
                .one_or_none()
            )
            if job is None:
                self._notify("The selected publish job no longer exists.", Tone.WARNING)
                self._refresh_schedule_page()
                return
            output_path = Path(job.processed_path)
            profile_name = self._clean_profile(
                job.account.instagram_profile if job.account else None
            )
            caption_text = self._schedule_caption_text_for_job(job)

        if not profile_name:
            self._notify(
                "This account has no Instagram profile set. Assign one in account "
                "settings, then log in.",
                Tone.WARNING,
            )
            return
        if not output_path.exists():
            self._notify("Reel output file is missing.", Tone.ERROR)
            return

        QApplication.clipboard().setText(caption_text)
        try:
            os.startfile(str(output_path.parent))
            launch_instagram_upload_assist(profile_name=profile_name)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Could not open Instagram assisted upload: {exc}", Tone.ERROR)
            return

        self._notify(
            "Caption copied. Instagram opened; upload the Reel manually, then mark Posted.",
            Tone.SUCCESS,
        )

    def _account_posts_last_24h(self, session, account_id: int) -> int:  # noqa: ANN001
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
        return (
            session.query(UploadJob)
            .filter(
                UploadJob.account_id == account_id,
                UploadJob.posted_at.is_not(None),
                UploadJob.posted_at >= since,
            )
            .count()
        )

    def _account_publish_cooldown_until(self, account_id: int) -> dt.datetime | None:
        until = self._account_publish_cooldown.get(account_id)
        if until is None:
            return None
        if until <= dt.datetime.now(dt.timezone.utc):
            self._account_publish_cooldown.pop(account_id, None)
            return None
        return until

    def _on_auto_publish_selected_clicked(self) -> None:
        if self._publish_in_progress:
            self._notify("A publish is already running.", Tone.WARNING)
            return
        job_id = self._selected_schedule_job_id()
        if job_id is None:
            self._notify("Select a publish job first.", Tone.WARNING)
            return

        with get_session() as session:
            job = (
                session.query(UploadJob)
                .options(joinedload(UploadJob.account))
                .filter(UploadJob.id == job_id)
                .one_or_none()
            )
            if job is None:
                self._notify("The selected publish job no longer exists.", Tone.WARNING)
                self._refresh_schedule_page()
                return
            # Duplicate guard: never re-post something already posted.
            if job.posted_at is not None or job.status == "posted":
                self._notify("This reel is already posted.", Tone.WARNING)
                return
            account_id = job.account_id
            profile_name = self._clean_profile(
                job.account.instagram_profile if job.account else None
            )
            video_path = Path(job.processed_path)
            caption = self._schedule_caption_text_for_job(job)
            posts_recent = self._account_posts_last_24h(session, account_id)

        if not profile_name:
            self._notify(
                "This account has no Instagram profile set. Assign one in account "
                "settings, then log in before publishing.",
                Tone.WARNING,
            )
            return
        cooldown_until = self._account_publish_cooldown_until(account_id)
        if cooldown_until is not None:
            self._notify(
                f"This account is paused until {cooldown_until.astimezone():%H:%M} "
                "after a checkpoint.",
                Tone.WARNING,
            )
            return
        if posts_recent >= PUBLISH_DAILY_CAP:
            self._notify(
                f"Daily cap reached for this account ({posts_recent}/{PUBLISH_DAILY_CAP}).",
                Tone.WARNING,
            )
            return
        if not video_path.exists():
            self._notify("Reel output file is missing.", Tone.ERROR)
            return

        do_share = not self._schedule_dry_run_checkbox.isChecked()
        self._notify(
            "Safe mode: opening Instagram (will stop before posting)..."
            if not do_share
            else "Publishing... an Instagram window will open. Leave it alone.",
            Tone.INFO,
        )
        self._launch_publish_worker(job_id, profile_name, video_path, caption, do_share)

    def _launch_publish_worker(
        self,
        job_id: int,
        profile_name: str,
        video_path: Path,
        caption: str,
        do_share: bool,
    ) -> None:
        """Start one publish on a worker thread. Callers MUST gate concurrency."""
        self._publish_in_progress = True
        self._schedule_auto_publish_button.setEnabled(False)

        thread = QThread(self)
        worker = PublishWorker(
            PublishJobConfig(
                job_id=job_id,
                profile_name=profile_name,
                video_path=video_path,
                caption=caption,
                do_share=do_share,
            )
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._on_publish_completed)
        worker.failed.connect(self._on_publish_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._publish_thread = thread
        self._publish_worker = worker
        thread.start()

    # --- Batch ("Publish Due Now") across all accounts -------------------

    def _on_publish_due_now_clicked(self) -> None:
        if self._publish_batch_active:
            self._stop_publish_batch(user_cancelled=True)
            return
        if self._publish_in_progress:
            self._notify("A publish is already running.", Tone.WARNING)
            return

        job_id = self._selected_schedule_job_id()
        if job_id is None:
            self._notify("Select a reel to publish first.", Tone.WARNING)
            return

        # Show which account + Instagram profile will receive the post so the
        # user can catch wrong-account mistakes before anything is submitted.
        if not self._confirm_publish_target(job_id):
            return

        self._begin_publish_batch([job_id], info_message="Publishing selected reel...")

    def _begin_publish_batch(self, job_ids: list[int], *, info_message: str) -> None:
        """Start the sequential batch runner over ``job_ids``.

        Each job carries its own account/profile, so a queue spanning multiple
        accounts posts them one at a time with the randomized inter-post gap —
        never simultaneously. Shared by single-job and all-due publishing.
        """
        self._publish_batch_active = True
        self._publish_batch_queue = list(job_ids)
        self._publish_batch_posted = 0
        self._publish_batch_skipped = 0
        self._batch_skip_reasons = []
        self._schedule_publish_due_button.setText("Stop Publishing")
        self._schedule_auto_publish_button.setEnabled(False)
        self._schedule_publish_all_button.setEnabled(False)
        self._notify(info_message, Tone.INFO)
        self._publish_next_in_batch()

    def _on_publish_all_due_clicked(self) -> None:
        """Publish every due reel across all accounts, sequentially."""
        if self._publish_batch_active:
            self._notify(
                "A batch is already running. Use 'Stop Publishing' to cancel.", Tone.WARNING
            )
            return
        if self._publish_in_progress:
            self._notify("A publish is already running.", Tone.WARNING)
            return

        job_ids = self._gather_due_publish_job_ids()
        if not job_ids:
            self._notify("No due reels to publish across any account.", Tone.WARNING)
            return
        if not self._confirm_publish_batch(job_ids):
            return
        self._begin_publish_batch(
            job_ids,
            info_message=f"Publishing {len(job_ids)} reel(s) across accounts, one at a time...",
        )

    def _confirm_publish_batch(self, job_ids: list[int]) -> bool:
        """List every account + file that will be posted, with the gap notice.

        Returns True if the user confirms, False to cancel.
        """
        rows: list[tuple[str, str, str]] = []
        with get_session() as session:
            for job_id in job_ids:
                job = (
                    session.query(UploadJob)
                    .options(joinedload(UploadJob.account))
                    .filter(UploadJob.id == job_id)
                    .one_or_none()
                )
                if job is None:
                    continue
                account_name = (job.account.name if job.account else None) or "Unknown account"
                profile = (
                    self._clean_profile(job.account.instagram_profile if job.account else None)
                    or "(no profile)"
                )
                file_name = Path(job.processed_path).name if job.processed_path else "(unknown file)"
                rows.append((account_name, profile, file_name))
        if not rows:
            self._notify("Due jobs no longer exist.", Tone.WARNING)
            return False

        lines = "<br>".join(
            f"&bull; <b>{name}</b> (@{profile}) — {fname}" for name, profile, fname in rows
        )
        gap_lo, gap_hi = (g // 60 for g in PUBLISH_BATCH_GAP_RANGE_SECONDS)
        msg = QMessageBox(self)
        msg.setWindowTitle("Confirm Publish All Due")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText(
            f"<b>Posting {len(rows)} reel(s) across accounts</b>, one at a time "
            f"with a ~{gap_lo}-{gap_hi} min gap between posts:<br><br>{lines}"
        )
        publish_btn = msg.addButton(f"Publish {len(rows)}", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(publish_btn)
        msg.exec()
        return msg.clickedButton() is publish_btn

    def _confirm_publish_target(self, job_id: int) -> bool:
        """Show a quick confirmation popup with the target account and profile.

        Returns True if the user clicks Publish, False if they cancel.
        """
        with get_session() as session:
            job = (
                session.query(UploadJob)
                .options(joinedload(UploadJob.account))
                .filter(UploadJob.id == job_id)
                .one_or_none()
            )
            if job is None:
                self._notify("Job no longer exists.", Tone.WARNING)
                return False
            account_name = (job.account.name if job.account else None) or "Unknown account"
            raw_profile = job.account.instagram_profile if job.account else None
            profile = self._clean_profile(raw_profile) or "(no profile set)"
            file_name = Path(job.processed_path).name if job.processed_path else "(unknown file)"

        msg = QMessageBox(self)
        msg.setWindowTitle("Confirm Publish")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText(
            f"<b>Posting to:</b><br><br>"
            f"Account: &nbsp;&nbsp;<b>{account_name}</b><br>"
            f"Instagram: <b>@{profile}</b><br>"
            f"File: &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>{file_name}</b>"
        )
        publish_btn = msg.addButton("Publish", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(publish_btn)
        msg.exec()
        return msg.clickedButton() is publish_btn

    def _gather_due_publish_job_ids(self) -> list[int]:
        """Job ids approved (Ready) or scheduled-and-due, across all accounts."""
        now = dt.datetime.now(dt.timezone.utc)
        due: list[int] = []
        with get_session() as session:
            jobs = (
                session.query(UploadJob)
                .filter(UploadJob.posted_at.is_(None))
                .filter(UploadJob.status.in_(["ready", "scheduled"]))
                .order_by(
                    UploadJob.scheduled_at.is_(None),
                    UploadJob.scheduled_at.asc(),
                    UploadJob.created_at.asc(),
                )
                .all()
            )
            for job in jobs:
                scheduled_at = job.scheduled_at
                if scheduled_at is not None:
                    if scheduled_at.tzinfo is None:
                        scheduled_at = scheduled_at.replace(tzinfo=dt.timezone.utc)
                    if scheduled_at > now:
                        continue  # not due yet
                due.append(job.id)
        return due

    def _prepare_batch_job(self, job_id: int) -> BatchJobPrep:
        """Re-validate a queued job against live guards.

        Returns a :class:`BatchJobPrep`: either a ready-to-publish payload, or a
        ``skip_reason`` so the batch can tell the user WHY a job was dropped
        instead of silently skipping it.
        """
        with get_session() as session:
            job = (
                session.query(UploadJob)
                .options(joinedload(UploadJob.account))
                .filter(UploadJob.id == job_id)
                .one_or_none()
            )
            if job is None:
                return BatchJobPrep(skip_reason="a job that no longer exists")
            account_name = (job.account.name if job.account else None) or "account"
            file_name = Path(job.processed_path).name if job.processed_path else ""
            file_label = f"{account_name} ({file_name})" if file_name else account_name
            if job.posted_at is not None or job.status == "posted":
                return BatchJobPrep(skip_reason=f"{file_label}: already posted")
            account_id = job.account_id
            profile_name = self._clean_profile(
                job.account.instagram_profile if job.account else None
            )
            if not profile_name:
                return BatchJobPrep(
                    skip_reason=f"{account_name}: no Instagram profile set — assign one first"
                )
            video_path = Path(job.processed_path)
            caption = self._schedule_caption_text_for_job(job)
            posts_recent = self._account_posts_last_24h(session, account_id)
            # Hook-tier gate inputs: resolved from the originating item while the
            # session is open (the relationship lazy-loads).
            item = job.download_item
            applied_title_tier = self._resolve_applied_title_tier(
                applied_title=job.title,
                smart_title_options_json=item.smart_title_options if item else None,
                smart_generation_meta_json=item.smart_generation_meta if item else None,
            )

        # Cap and cooldown are account-level, so phrase them per account (no file
        # name) — that way 5 jobs blocked by the same cap collapse to one reason.
        cooldown_until = self._account_publish_cooldown_until(account_id)
        if cooldown_until is not None:
            return BatchJobPrep(
                skip_reason=(
                    f"{account_name}: paused until "
                    f"{cooldown_until.astimezone():%H:%M} after a checkpoint"
                )
            )
        if posts_recent >= PUBLISH_DAILY_CAP:
            return BatchJobPrep(
                skip_reason=(
                    f"{account_name}: daily cap reached "
                    f"({posts_recent}/{PUBLISH_DAILY_CAP} in 24h)"
                )
            )
        if not video_path.exists():
            return BatchJobPrep(skip_reason=f"{file_label}: output file is missing")
        if applied_title_tier in PUBLISH_AUTO_HOLD_TIERS:
            return BatchJobPrep(
                skip_reason=(
                    f"{file_label}: hook tagged {applied_title_tier} — held for "
                    "review (auto-publish posts Green hooks only)"
                )
            )
        return BatchJobPrep(payload=(account_id, profile_name, video_path, caption))

    @staticmethod
    def _resolve_applied_title_tier(
        *,
        applied_title: str | None,
        smart_title_options_json: str | None,
        smart_generation_meta_json: str | None,
    ) -> str | None:
        """Tier ('green'/'yellow'/'red') of the title actually being posted.

        Matches the job's applied title against the saved title options to find
        its index, then returns that option's tier. Returns ``None`` when the
        tier is unknown — no tier data (legacy items) or a manually edited
        title that no longer matches a generated option — so the caller can let
        those through instead of blocking the existing queue.
        """
        if not smart_generation_meta_json:
            return None
        try:
            meta = json.loads(smart_generation_meta_json)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(meta, dict):
            return None
        tiers = MainWindow._clean_saved_option_tiers(meta.get("option_tiers"))
        if not tiers:
            return None
        options: list[str] = []
        if smart_title_options_json:
            try:
                raw = json.loads(smart_title_options_json)
            except (json.JSONDecodeError, TypeError):
                raw = None
            if isinstance(raw, list):
                options = [str(option) for option in raw]
        applied = (applied_title or "").strip().casefold()
        if not applied:
            return None
        for index, option in enumerate(options):
            if option.strip().casefold() == applied and index < len(tiers):
                return tiers[index]
        # Edited title that matches no generated option: tier is genuinely
        # unknown — the user already took control, so don't guess.
        return None

    def _publish_next_in_batch(self) -> None:
        if not self._publish_batch_active:
            return
        while self._publish_batch_queue:
            job_id = self._publish_batch_queue.pop(0)
            prep = self._prepare_batch_job(job_id)
            if prep.payload is None:
                self._publish_batch_skipped += 1
                if prep.skip_reason and prep.skip_reason not in self._batch_skip_reasons:
                    self._batch_skip_reasons.append(prep.skip_reason)
                continue
            _account_id, profile_name, video_path, caption = prep.payload
            do_share = not self._schedule_dry_run_checkbox.isChecked()
            self._launch_publish_worker(job_id, profile_name, video_path, caption, do_share)
            return
        self._finish_publish_batch()

    def _schedule_next_batch_publish(self) -> None:
        if not self._publish_batch_active:
            return
        if not self._publish_batch_queue:
            self._finish_publish_batch()
            return
        gap_seconds = random.randint(*PUBLISH_BATCH_GAP_RANGE_SECONDS)
        self._notify(
            f"Next post in ~{round(gap_seconds / 60)} min ({len(self._publish_batch_queue)} left).",
            Tone.INFO,
        )
        QTimer.singleShot(gap_seconds * 1000, self._publish_next_in_batch)

    def _finish_publish_batch(self) -> None:
        posted = self._publish_batch_posted
        skipped = self._publish_batch_skipped
        reasons = self._batch_skip_reasons
        self._publish_batch_active = False
        self._publish_batch_queue = []
        self._batch_skip_reasons = []
        self._schedule_publish_due_button.setText("Publish Due Now")
        self._schedule_publish_all_button.setEnabled(True)
        message = f"Batch done: {posted} posted, {skipped} skipped."
        if reasons:
            message += " Skipped: " + "; ".join(reasons) + "."
        # Nothing posted while something was skipped is a warning, not a success.
        tone = Tone.WARNING if skipped and not posted else Tone.SUCCESS
        self._notify(message, tone)
        self._refresh_schedule_page()

    def _stop_publish_batch(self, *, user_cancelled: bool) -> None:
        self._publish_batch_active = False
        self._publish_batch_queue = []
        self._schedule_publish_due_button.setText("Publish Due Now")
        self._schedule_publish_all_button.setEnabled(True)
        if user_cancelled:
            self._notify(
                "Stopped batch publishing. A post already in progress will finish.",
                Tone.WARNING,
            )
        self._refresh_schedule_page()

    def _on_publish_completed(self, payload: dict) -> None:
        self._publish_in_progress = False
        self._publish_thread = None
        self._publish_worker = None
        job_id = int(payload.get("job_id"))
        status = str(payload.get("status"))
        posted_url = payload.get("posted_url")
        error_message = payload.get("error_message")

        if status == "posted":
            with get_session() as session:
                job = session.get(UploadJob, job_id)
                if job is not None:
                    job.status = "posted"
                    job.posted_at = dt.datetime.now(dt.timezone.utc)
                    job.posted_url = posted_url
                    job.error_message = None
                    session.commit()
            self._notify("Reel published.", Tone.SUCCESS)
        elif status == "dry_run":
            self._notify("Safe mode: reached Share without posting.", Tone.WARNING)
        elif status == "checkpoint":
            account_id: int | None = None
            with get_session() as session:
                job = session.get(UploadJob, job_id)
                if job is not None:
                    account_id = job.account_id
                    job.status = "failed"
                    job.error_message = error_message or "checkpoint detected"
                    session.commit()
            if account_id is not None:
                self._account_publish_cooldown[account_id] = (
                    dt.datetime.now(dt.timezone.utc) + PUBLISH_CHECKPOINT_COOLDOWN
                )
            self._notify(
                "Instagram flagged this account. Paused it and stopped publishing.",
                Tone.ERROR,
            )
        else:  # failed
            with get_session() as session:
                job = session.get(UploadJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error_message = error_message or "publish failed"
                    session.commit()
            self._notify(f"Publish failed: {error_message}", Tone.ERROR)

        self._refresh_schedule_page()
        self._advance_batch_after(status)

    def _on_publish_failed(self, message: str) -> None:
        self._publish_in_progress = False
        self._publish_thread = None
        self._publish_worker = None
        self._notify(f"Publish failed: {message}", Tone.ERROR)
        self._refresh_schedule_page()
        self._advance_batch_after("failed")

    @staticmethod
    def _next_scheduled_hint(jobs: list[UploadJob]) -> str:
        """Return ' Next: HH:MM (in N min).' for the soonest future scheduled job."""
        now = dt.datetime.now(dt.timezone.utc)
        upcoming: list[dt.datetime] = []
        for job in jobs:
            scheduled_at = job.scheduled_at
            if scheduled_at is None or job.posted_at is not None:
                continue
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=dt.timezone.utc)
            if scheduled_at > now:
                upcoming.append(scheduled_at)
        if not upcoming:
            return ""
        soonest = min(upcoming)
        minutes = int((soonest - now).total_seconds() // 60)
        relative = f"in {minutes} min" if minutes < 60 else f"in {minutes // 60}h {minutes % 60}m"
        return f" Next: {soonest.astimezone():%H:%M} ({relative})."

    def _update_due_badge(self) -> None:
        """No-op while a batch is running; button label is managed by the batch lifecycle."""
        pass

    def _advance_batch_after(self, status: str) -> None:
        """Continue the batch (if running) with a randomized gap. No-op otherwise."""
        if not self._publish_batch_active:
            return
        if status == "posted":
            self._publish_batch_posted += 1
        else:
            self._publish_batch_skipped += 1
        self._schedule_next_batch_publish()

    def _sync_processed_outputs_to_upload_jobs(self) -> None:
        if self._current_account_id is None:
            return

        with get_session() as session:
            account = session.get(Account, self._current_account_id)
            if account is None:
                return

            existing_paths = {
                job.processed_path
                for job in session.query(UploadJob).filter(UploadJob.account_id == account.id).all()
            }
            items = session.query(DownloadItem).filter(DownloadItem.account_id == account.id).all()
            created = False
            for item in items:
                if not item.file_path:
                    continue
                output_path = self._existing_processed_output_path_for_item(item)
                if output_path is None:
                    continue
                output_path_text = str(output_path)
                if not output_path.exists() or output_path_text in existing_paths:
                    continue

                scheduled_at = self._next_upload_slot(account.upload_schedule_slots)
                session.add(
                    UploadJob(
                        account_id=account.id,
                        download_item_id=item.id,
                        processed_path=output_path_text,
                        title=item.title_draft or item.title,
                        description=item.caption_draft,
                        scheduled_at=scheduled_at,
                        timezone=account.upload_timezone or "Asia/Bangkok",
                        privacy_status=account.upload_default_privacy or "private",
                        made_for_kids=int(account.upload_made_for_kids or 0),
                        contains_synthetic_media=int(account.upload_contains_synthetic_media or 0),
                        status="scheduled" if scheduled_at is not None else "draft",
                    )
                )
                existing_paths.add(output_path_text)
                created = True

            if created:
                session.commit()

    @staticmethod
    def _short_failure_reason(error_message: str | None, *, limit: int = 60) -> str:
        """First line of an error, trimmed to a compact inline hint."""
        if not error_message:
            return ""
        first_line = error_message.strip().splitlines()[0].strip()
        if len(first_line) <= limit:
            return first_line
        return first_line[: limit - 1].rstrip() + "…"

    def _refresh_schedule_page(self) -> None:
        if not hasattr(self, "_schedule_table"):
            return

        selected_job_id = self._take_preferred_schedule_selection_id(
            self._selected_schedule_job_id()
        )
        workspace_enabled = self._current_account_id is not None
        self._schedule_table.setEnabled(workspace_enabled)
        self._schedule_table.blockSignals(True)
        self._schedule_table.setRowCount(0)

        if not workspace_enabled:
            self._schedule_summary_label.setText(
                "Select an account workspace to review schedule drafts."
            )
            self._schedule_caption_preview.clear()
            self._schedule_status_combo.setEnabled(False)
            self._resize_schedule_table_height()
            self._schedule_table.blockSignals(False)
            return

        self._sync_processed_outputs_to_upload_jobs()

        with get_session() as session:
            schedule_jobs = (
                session.query(UploadJob)
                .options(joinedload(UploadJob.account), joinedload(UploadJob.download_item))
                .filter(UploadJob.account_id == self._current_account_id)
                .order_by(
                    UploadJob.scheduled_at.is_(None),
                    UploadJob.scheduled_at.asc(),
                    UploadJob.created_at.desc(),
                )
                .all()
            )

        selected_row = -1
        for job in schedule_jobs:
            row = self._schedule_table.rowCount()
            self._schedule_table.insertRow(row)
            if job.id == selected_job_id:
                selected_row = row
            video_title = (
                job.download_item.title
                if job.download_item is not None and job.download_item.title
                else Path(job.processed_path).stem
            )
            status_text = self._upload_status_text(job)
            values = [
                job.account.name if job.account else "Unassigned",
                video_title or "(untitled)",
                job.title or "(not drafted)",
                self._upload_scheduled_text(job),
                job.privacy_status,
                status_text,
                Path(job.processed_path).name,
            ]
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                table_item.setData(Qt.ItemDataRole.UserRole, job.id)
                if column == 5:
                    # Style off the plain label so "Failed" keeps its color,
                    # then surface the reason inline + full text on hover so the
                    # user can see *why* a post failed without digging.
                    self._apply_schedule_status_style(table_item, value)
                    if (job.status or "").lower() == "failed" and job.error_message:
                        reason = MainWindow._short_failure_reason(job.error_message)
                        if reason:
                            table_item.setText(f"Failed — {reason}")
                        table_item.setToolTip(job.error_message.strip())
                self._schedule_table.setItem(row, column, table_item)

        self._schedule_table.resizeRowsToContents()
        self._resize_schedule_table_height()
        self._schedule_table.blockSignals(False)
        if selected_row < 0 and self._schedule_table.rowCount() > 0:
            selected_row = 0
        if selected_row >= 0:
            self._schedule_table.selectRow(selected_row)
        self._on_schedule_selection_changed()
        self._update_due_badge()
        if schedule_jobs:
            scheduled_count = sum(1 for job in schedule_jobs if job.scheduled_at is not None)
            summary = (
                f"{len(schedule_jobs)} upload job(s) for this account. "
                f"{scheduled_count} have a scheduled time."
            )
            next_hint = self._next_scheduled_hint(schedule_jobs)
            self._schedule_summary_label.setText(summary + next_hint)
        else:
            self._schedule_summary_label.setText(
                "No publish jobs yet. Finish Preprocess, then use Add to Schedule."
            )

    def _apply_schedule_status_style(self, item: QTableWidgetItem, status_text: str) -> None:
        if status_text in {"Scheduled", "Uploaded", "Posted", "Ready"}:
            item.setForeground(QColor("#8ee6b1"))
            item.setBackground(QColor("#173222"))
        elif status_text == "Uploading":
            item.setForeground(QColor("#9fc6ff"))
            item.setBackground(QColor("#162a45"))
        elif status_text == "Draft":
            item.setForeground(QColor("#f5cd79"))
            item.setBackground(QColor("#342812"))
        elif status_text in {"Missing output", "Failed"}:
            item.setForeground(QColor("#ff9c9c"))
            item.setBackground(QColor("#35171c"))
        elif status_text == "Skipped":
            item.setForeground(QColor("#b9c8d8"))
            item.setBackground(QColor("#1b2430"))
        else:
            item.setForeground(QColor("#b9c8d8"))
            item.setBackground(QColor("#182332"))

    def _resize_schedule_table_height(self) -> None:
        if not hasattr(self, "_schedule_panel"):
            return

        layout = self._schedule_panel.layout()
        margins = layout.contentsMargins()
        spacing = layout.spacing()
        fixed_header_height = sum(
            widget.sizeHint().height()
            for widget in (
                self._schedule_title_label,
                self._schedule_message_label,
                self._schedule_summary_label,
                self._schedule_caption_preview,
            )
        )
        reserved_height = (
            margins.top()
            + margins.bottom()
            + fixed_header_height
            + self._schedule_copy_caption_button.sizeHint().height()
            + self._schedule_caption_combo.sizeHint().height()
            + (spacing * 5)
        )
        target_height = max(230, self._scroll_area.viewport().height() - reserved_height)
        self._schedule_table.setFixedHeight(target_height)

    def _created_text(self, item: DownloadItem) -> str:
        created_at = item.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        return created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def _set_detail_placeholder(self) -> None:
        placeholder = self._ui.detail_placeholder
        for field in self._detail_fields.values():
            field.setText(placeholder)
        self._detail_placeholder.setVisible(True)
        self._detail_review_hint.setText("Select a library item to review it.")
        self._toggle_detail_content(False)
        self._detail_panel.setVisible(False)

    def _refresh_account_action_labels(self, account: Account | None) -> None:
        creating_new = account is None
        self._account_save_button.setText(
            "Create Niche Account" if creating_new else "Save Niche Changes"
        )
        self._account_main_edit_button.setEnabled(bool(self._accounts))
        self._account_main_delete_button.setEnabled(bool(self._accounts))
        self._account_delete_button.setEnabled(bool(self._accounts))

    def _set_account_sidebar_visible(self, visible: bool) -> None:
        self._account_panel.setVisible(visible)
        self._sidebar_toggle_button.setChecked(visible)
        self._sidebar_toggle_button.setProperty("selected", visible)
        self._sidebar_toggle_button.style().unpolish(self._sidebar_toggle_button)
        self._sidebar_toggle_button.style().polish(self._sidebar_toggle_button)
        self._sidebar_toggle_button.setToolTip(
            "Hide account manager" if visible else "Open account manager"
        )

    def _sync_account_panel_visibility(self) -> None:
        should_show = self._current_account_id is None
        self._set_account_sidebar_visible(should_show)
        self._sidebar_toggle_button.setEnabled(self._current_account_id is not None)

    def _toggle_account_sidebar(self) -> None:
        if self._current_account_id is None:
            self._set_account_sidebar_visible(True)
            return
        self._set_account_sidebar_visible(not self._account_panel.isVisible())

    def _on_processing_debug_toggled(self, checked: bool) -> None:
        self._processing_debug_panel.setVisible(checked)
        self._processing_debug_toggle.setText(
            "Hide Generation Details" if checked else "Show Generation Details"
        )

    def _set_account_mode(
        self,
        mode: str,
        *,
        title: str,
        hint: str,
        show_main_actions: bool,
        show_picker: bool,
        show_form: bool,
        show_form_actions: bool,
        show_delete_panel: bool,
    ) -> None:
        self._account_mode = mode
        self._account_mode_label.setText(title)
        self._account_mode_hint.setText(hint)
        self._account_main_actions.setVisible(show_main_actions)
        self._account_picker_panel.setVisible(show_picker)
        self._account_form_scroll.setVisible(show_form)
        self._account_form_actions.setVisible(show_form_actions)
        self._account_delete_panel.setVisible(show_delete_panel)

    def _show_account_main(self) -> None:
        self._set_account_mode(
            "main",
            title="Main",
            hint="Choose what you want to do with saved accounts.",
            show_main_actions=True,
            show_picker=False,
            show_form=False,
            show_form_actions=False,
            show_delete_panel=False,
        )

    def _show_new_account_form(self) -> None:
        self._populate_account_form(None)
        self._account_picker.setCurrentIndex(0)
        self._set_account_mode(
            "new",
            title="New Account",
            hint="Create a fresh niche account, then return to niche tools.",
            show_main_actions=False,
            show_picker=False,
            show_form=True,
            show_form_actions=True,
            show_delete_panel=False,
        )

    def _show_edit_account_form(self) -> None:
        self._set_account_mode(
            "edit",
            title="Edit Account",
            hint="Pick a saved account, update the fields, then save your changes.",
            show_main_actions=False,
            show_picker=True,
            show_form=True,
            show_form_actions=True,
            show_delete_panel=False,
        )
        if self._accounts and self._account_picker.currentData() is None:
            self._account_picker.setCurrentIndex(1)
        else:
            self._populate_account_form(self._current_account())

    def _show_delete_account_panel(self) -> None:
        self._set_account_mode(
            "delete",
            title="Delete Niche Account",
            hint="Choose one saved niche account to remove, then return to niche tools.",
            show_main_actions=False,
            show_picker=False,
            show_form=False,
            show_form_actions=False,
            show_delete_panel=True,
        )

    def _toggle_detail_content(self, visible: bool) -> None:
        show_advanced = visible and self._detail_advanced_toggle.isChecked()
        for key, label_widget in self._detail_field_labels.items():
            field_visible = visible and (key not in self._detail_advanced_keys or show_advanced)
            label_widget.setVisible(field_visible)
            self._detail_fields[key].setVisible(field_visible)
        self._detail_advanced_toggle.setVisible(visible)
        self._detail_account_combo.setVisible(visible)
        self._detail_assign_button.setVisible(visible)
        for index in range(self._detail_action_row.count()):
            widget = self._detail_action_row.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(visible)

    def _on_detail_advanced_toggled(self, checked: bool) -> None:
        self._detail_advanced_toggle.setText(
            "Hide File Details" if checked else "Show File Details"
        )
        self._toggle_detail_content(self._selected_item_id is not None)

    def _update_detail_panel(self, item: DownloadItem | None) -> None:
        if item is None:
            self._set_detail_placeholder()
            return

        self._detail_panel.setVisible(False)
        self._detail_placeholder.setVisible(False)
        self._toggle_detail_content(True)
        self._detail_fields["title"].setText(item.title or "(untitled)")
        self._detail_fields["status"].setText(item.status)
        self._detail_fields["review"].setText(self._review_state_label(item.review_state))
        self._detail_fields["account"].setText(item.account.name if item.account else "Unassigned")
        self._detail_fields["created"].setText(self._created_text(item))
        self._detail_fields["extractor"].setText(item.extractor or "(unknown)")
        self._detail_fields["video_id"].setText(item.video_id or "(unknown)")
        self._detail_fields["source_url"].setText(item.source_url)
        self._detail_fields["file_path"].setText(item.file_path or "(pending)")
        self._detail_fields["file_info"].setText(self._file_info_text(item))
        self._detail_fields["error"].setText(item.error_message or "No error.")
        self._detail_fields["title"].setToolTip(item.title or "(untitled)")
        self._detail_fields["source_url"].setToolTip(item.source_url)
        self._detail_fields["file_path"].setToolTip(item.file_path or "(pending)")
        self._restore_combo_value(self._detail_account_combo, item.account_id)
        self._detail_keep_button.setEnabled(item.review_state != "kept")
        self._detail_reject_button.setEnabled(item.review_state != "rejected")
        self._detail_reset_button.setEnabled(item.review_state != "new")
        self._detail_open_button.setEnabled(self._item_exists(item))
        self._detail_reveal_button.setEnabled(bool(item.file_path))
        self._detail_retry_button.setText(self._download_retry_label(item.status))
        self._detail_retry_button.setEnabled(item.status in {"failed", "downloaded"})
        self._detail_remove_button.setEnabled(True)
        assignment_enabled = item.review_state == "kept"
        self._detail_account_combo.setEnabled(assignment_enabled)
        self._detail_assign_button.setEnabled(assignment_enabled)
        self._detail_review_hint.setText(self._download_review_hint_text(item))

    def _current_selected_item(self) -> DownloadItem | None:
        if self._selected_item_id is None:
            return None
        return next(
            (item for item in self._displayed_items if item.id == self._selected_item_id), None
        )

    def _current_selected_candidate(self) -> ScrapeCandidate | None:
        if self._selected_candidate_id is None:
            return None
        return next(
            (item for item in self._displayed_candidates if item.id == self._selected_candidate_id),
            None,
        )

    def _snapshot_signature(self, items: list[DownloadItem]) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                item.id,
                item.status,
                item.review_state,
                item.account_id,
                item.title,
                item.source_url,
                item.file_path,
                item.error_message,
            )
            for item in items
        )

    def _candidate_snapshot_signature(
        self,
        items: list[ScrapeCandidate],
    ) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                item.id,
                item.state,
                item.ranking_score,
                item.view_count,
                item.like_count,
                item.comment_count,
                item.duration_seconds,
                item.created_at,
                item.title,
                item.channel_name,
                item.published_at,
                item.match_reason,
            )
            for item in items
        )

    @staticmethod
    def _candidate_state_label(state: str) -> str:
        normalized_state = "candidate" if state == "new" else state
        labels = {
            "candidate": "ready",
            "queued": "queued",
            "downloaded": "downloaded",
            "ignored": "ignored",
        }
        return labels.get(normalized_state, normalized_state)

    @staticmethod
    def _candidate_queue_button_text(candidate: ScrapeCandidate | None) -> str:
        if candidate is not None:
            normalized_state = "candidate" if candidate.state == "new" else candidate.state
            if normalized_state == "downloaded":
                return "Redownload Candidate"
        return "Send To Download"

    @staticmethod
    def _candidate_source_filter_value(candidate: ScrapeCandidate) -> str:
        channel_name = (candidate.channel_name or "").strip()
        if channel_name:
            return channel_name.casefold()
        scrape_source_url = (candidate.scrape_source_url or "").strip()
        parsed = urlparse(scrape_source_url)
        path_part = parsed.path.strip("/").split("/", 1)[0]
        return (path_part or scrape_source_url or "(unknown)").casefold()

    @staticmethod
    def _candidate_source_filter_label(candidate: ScrapeCandidate) -> str:
        channel_name = (candidate.channel_name or "").strip()
        if channel_name:
            return channel_name
        scrape_source_url = (candidate.scrape_source_url or "").strip()
        parsed = urlparse(scrape_source_url)
        path_part = parsed.path.strip("/").split("/", 1)[0]
        return path_part or "(unknown)"

    def _refresh_candidate_source_filter_options(
        self,
        candidates: list[ScrapeCandidate],
    ) -> None:
        selected = self._candidate_source_filter.currentData() or "all"
        source_options: dict[str, str] = {}
        for candidate in candidates:
            value = self._candidate_source_filter_value(candidate)
            if value and value != "(unknown)":
                source_options.setdefault(value, self._candidate_source_filter_label(candidate))

        self._candidate_source_filter.blockSignals(True)
        self._candidate_source_filter.clear()
        self._candidate_source_filter.addItem("Source: All", "all")
        for value, label in sorted(source_options.items(), key=lambda item: item[1].casefold()):
            self._candidate_source_filter.addItem(f"Source: {label}", value)
        selected_index = self._candidate_source_filter.findData(selected)
        self._candidate_source_filter.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        self._candidate_source_filter.blockSignals(False)

    def _matches_candidate_state_filter(self, candidate: ScrapeCandidate) -> bool:
        min_likes = self._instagram_discover_min_likes_input.value()
        account = self._active_account()
        if (
            account is not None
            and account.platform == "instagram"
            and min_likes > 0
            and candidate.like_count is not None
            and candidate.like_count < min_likes
        ):
            return False
        selected_state = self._candidate_state_filter.currentData()
        if selected_state not in {None, "all"}:
            normalized_state = "candidate" if candidate.state == "new" else candidate.state
            if normalized_state != selected_state:
                return False
        selected_source = self._candidate_source_filter.currentData()
        if selected_source in {None, "all"}:
            return True
        return self._candidate_source_filter_value(candidate) == selected_source

    @staticmethod
    def _candidate_sort_datetime(value: dt.datetime | None) -> dt.datetime:
        if value is None:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def _candidate_sort_key(self, item: ScrapeCandidate) -> tuple[object, ...]:
        published_at = self._candidate_sort_datetime(item.published_at)
        sort_mode = self._candidate_sort_combo.currentData()
        if sort_mode == "likes":
            return (
                item.like_count or 0,
                item.comment_count or 0,
                item.ranking_score or 0,
                published_at,
            )
        if sort_mode == "comments":
            return (
                item.comment_count or 0,
                item.like_count or 0,
                item.ranking_score or 0,
                published_at,
            )
        if sort_mode == "newest":
            created_at = self._candidate_sort_datetime(item.created_at)
            return (
                created_at,
                published_at,
                item.like_count or 0,
                item.comment_count or 0,
                item.ranking_score or 0,
            )
        return (
            item.ranking_score or 0,
            item.like_count or 0,
            item.comment_count or 0,
            published_at,
        )

    def _mark_user_interacting(self) -> None:
        if self._suppress_interaction_tracking:
            return
        self._interaction_idle_timer.start(900)

    def _on_interaction_idle(self) -> None:
        if self._pending_refresh:
            if self._processing_has_unsaved_text_edits():
                return
            self._pending_refresh = False
            self._apply_refresh()

    def _on_search_changed(self) -> None:
        self._mark_user_interacting()
        self._apply_refresh(force=True)

    def _on_status_filter_changed(self) -> None:
        self._mark_user_interacting()
        self._apply_refresh(force=True)

    def _on_candidate_filter_changed(self) -> None:
        self._mark_user_interacting()
        self._refresh_candidates(force=True)

    def _on_candidate_min_likes_filter_changed(self) -> None:
        self._save_candidate_min_likes_filter()
        self._on_candidate_filter_changed()

    def _save_candidate_min_likes_filter(self) -> None:
        if self._current_account_id is None:
            return
        value = self._instagram_discover_min_likes_input.value()
        with get_session() as session:
            account = session.get(Account, self._current_account_id)
            if account is None:
                return
            account.candidate_min_like_filter = value
            session.commit()
        active_account = self._active_account()
        if active_account is not None:
            active_account.candidate_min_like_filter = value

    def _refresh_candidate_min_likes_filter(self) -> None:
        if not hasattr(self, "_instagram_discover_min_likes_input"):
            return
        account = self._active_account()
        value = (
            account.candidate_min_like_filter
            if account is not None and account.candidate_min_like_filter is not None
            else 20_000
        )
        self._instagram_discover_min_likes_input.blockSignals(True)
        self._instagram_discover_min_likes_input.setValue(int(value))
        self._instagram_discover_min_likes_input.blockSignals(False)

    def _load_accounts(self) -> list[Account]:
        with get_session() as session:
            return session.query(Account).order_by(Account.platform.asc(), Account.name.asc()).all()

    def _current_account(self) -> Account | None:
        account_id = self._account_picker.currentData()
        if account_id is None:
            return None
        return next((account for account in self._accounts if account.id == account_id), None)

    def _active_account(self) -> Account | None:
        if self._current_account_id is None:
            return None
        return next(
            (account for account in self._accounts if account.id == self._current_account_id), None
        )

    @staticmethod
    def _account_voice_config(account: Account | None) -> dict[str, str]:
        if account is None:
            return {}
        voice_config = {
            "tone": account.writing_tone or "",
            "target_audience": account.target_audience or "",
            "hook_style": account.hook_style or "",
            "banned_phrases": account.banned_phrases or "",
            "title_style": account.title_style_notes or "",
            "caption_style": account.caption_style_notes or "",
        }
        return {key: value for key, value in voice_config.items() if value.strip()}

    def _processing_account_voice_config(self, account: Account | None) -> dict[str, str]:
        voice_config = self._account_voice_config(account)
        premise = self._processing_clip_premise_input.toPlainText().strip()
        if premise:
            voice_config["clip_context"] = premise
        return voice_config

    @staticmethod
    def _restore_combo_value(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _refresh_account_controls(self) -> None:
        current_account_id = self._account_picker.currentData()
        current_active_account_id = self._current_account_id
        current_detail_account_id = self._detail_account_combo.currentData()
        current_delete_account_id = self._account_delete_picker.currentData()

        self._accounts = self._load_accounts()
        self._suppress_account_form_sync = True
        self._current_account_combo.blockSignals(True)
        self._sidebar_account_combo.blockSignals(True)
        self._account_picker.blockSignals(True)
        self._detail_account_combo.blockSignals(True)
        self._account_delete_picker.blockSignals(True)
        self._current_account_combo.clear()
        self._current_account_combo.addItem("Choose active niche...", None)
        self._sidebar_account_combo.clear()
        self._sidebar_account_combo.addItem("Choose account...", None)
        self._account_picker.clear()
        self._account_picker.addItem("Select niche account to edit...", None)
        self._detail_account_combo.clear()
        self._detail_account_combo.addItem("Unassigned", None)
        self._account_delete_picker.clear()
        self._account_delete_picker.addItem("Choose account to delete...", None)

        for account in self._accounts:
            label = f"{account.name} ({account.platform})"
            self._current_account_combo.addItem(label, account.id)
            self._sidebar_account_combo.addItem(label, account.id)
            self._account_picker.addItem(label, account.id)
            self._detail_account_combo.addItem(label, account.id)
            self._account_delete_picker.addItem(label, account.id)

        self._restore_combo_value(self._current_account_combo, current_active_account_id)
        self._restore_combo_value(self._sidebar_account_combo, current_active_account_id)
        self._restore_combo_value(self._account_picker, current_account_id)
        self._restore_combo_value(self._detail_account_combo, current_detail_account_id)
        self._restore_combo_value(self._account_delete_picker, current_delete_account_id)
        self._current_account_combo.blockSignals(False)
        self._sidebar_account_combo.blockSignals(False)
        self._account_picker.blockSignals(False)
        self._detail_account_combo.blockSignals(False)
        self._account_delete_picker.blockSignals(False)
        self._suppress_account_form_sync = False
        self._current_account_id = self._current_account_combo.currentData()
        self._refresh_candidate_min_likes_filter()
        if self._account_mode == "edit":
            self._populate_account_form(self._current_account())
        elif self._account_mode == "new":
            self._populate_account_form(None)
        self._refresh_account_action_labels(self._current_account())

    def _set_current_account_from_combo(self, combo: QComboBox) -> None:
        if self._suppress_account_form_sync:
            return
        self._current_account_id = combo.currentData()
        self._current_account_combo.blockSignals(True)
        self._sidebar_account_combo.blockSignals(True)
        self._restore_combo_value(self._current_account_combo, self._current_account_id)
        self._restore_combo_value(self._sidebar_account_combo, self._current_account_id)
        self._current_account_combo.blockSignals(False)
        self._sidebar_account_combo.blockSignals(False)
        self._refresh_candidate_min_likes_filter()
        self._sync_account_panel_visibility()
        self._clear_selection()
        self._clear_source_selection()
        self._clear_candidate_selection()
        self._apply_refresh(force=True)
        # Restore this account's saved Processing widget values (template,
        # caption style, title style, font, size, color, layout, etc.) so
        # switching between niche accounts doesn't require re-picking
        # niche-appropriate styles each time. No-op for new accounts with
        # no saved snapshot yet. Runs AFTER _apply_refresh because
        # _refresh_processing_page → _set_processing_placeholder_state (the
        # no-items branch) resets the template to "gaming_meme_black",
        # which would otherwise overwrite the snapshot we just applied.
        self._apply_processing_preferences_for_account(self._current_account_id)

    def _on_current_account_changed(self) -> None:
        self._set_current_account_from_combo(self._current_account_combo)

    def _on_sidebar_account_changed(self) -> None:
        self._set_current_account_from_combo(self._sidebar_account_combo)

    def _scrape_summary_text(self, account: Account | None) -> str:
        if account is None:
            return "Select an account to configure source intake."

        (
            sources,
            keywords,
            max_items,
            max_age_days,
            discovery_mode,
            auto_queue_limit,
            min_view_count,
            min_like_count,
            _weights,
        ) = self._account_scrape_config(account)
        enabled_sources = [source for source in sources if source.enabled]
        if not sources:
            return f"No sources configured for {account.name} yet."

        age_text = (
            f"last {max_age_days} day(s)" if max_age_days is not None else "all available dates"
        )
        mode_text = (
            "review only"
            if discovery_mode == "review_only"
            else f"auto-queue top {auto_queue_limit}"
        )
        return (
            f"{len(enabled_sources)} of {len(sources)} source(s) enabled, {len(keywords)} keyword(s), {mode_text} for {account.name}. "
            f"Fetch up to {max_items} candidate item(s) per run from {age_text}; "
            f"minimums: {min_view_count} views / {min_like_count} likes."
        )

    def _populate_account_form(self, account: Account | None) -> None:
        if account is None:
            self._account_name_input.clear()
            self._restore_combo_value(self._account_platform_combo, "youtube")
            self._account_niche_input.clear()
            self._account_login_input.clear()
            self._account_instagram_profile_input.clear()
            self._account_instagram_handle_input.clear()
            self._account_credential_input.clear()
            self._account_scrape_sources_input.clear()
            self._account_scrape_max_items_input.setText("20")
            self._account_scrape_max_age_days_input.clear()
            self._account_discovery_keywords_input.clear()
            self._restore_combo_value(self._account_discovery_mode_combo, "review_only")
            self._account_auto_queue_limit_input.setText("3")
            self._account_min_view_count_input.clear()
            self._account_min_like_count_input.clear()
            self._account_weight_views_input.setText("35")
            self._account_weight_likes_input.setText("20")
            self._account_weight_recency_input.setText("25")
            self._account_weight_keyword_input.setText("20")
            self._account_writing_tone_input.clear()
            self._account_target_audience_input.clear()
            self._account_hook_style_input.clear()
            self._account_banned_phrases_input.clear()
            self._account_title_style_notes_input.clear()
            self._account_caption_style_notes_input.clear()
            self._account_upload_timezone_input.setText("Asia/Jakarta")
            self._restore_combo_value(self._account_upload_privacy_combo, "private")
            self._account_upload_schedule_slots_input.clear()
            self._restore_combo_value(self._account_upload_made_for_kids_combo, 0)
            self._restore_combo_value(self._account_upload_synthetic_media_combo, 0)
            self._refresh_account_action_labels(None)
            return

        self._account_name_input.setText(account.name)
        self._restore_combo_value(self._account_platform_combo, account.platform)
        self._account_niche_input.setText(account.niche_label or "")
        self._account_login_input.setText(account.login_identifier or "")
        self._account_instagram_profile_input.setText(account.instagram_profile or "")
        self._account_instagram_handle_input.setText(account.instagram_handle or "")
        self._account_credential_input.setText(account.credential_blob or "")
        self._account_scrape_sources_input.setText(account.scrape_source_urls or "")
        self._account_scrape_max_items_input.setText(str(account.scrape_max_items or 20))
        self._account_scrape_max_age_days_input.setText(
            str(account.scrape_max_age_days) if account.scrape_max_age_days else ""
        )
        self._account_discovery_keywords_input.setText(account.discovery_keywords or "")
        self._restore_combo_value(
            self._account_discovery_mode_combo,
            account.discovery_mode or "review_only",
        )
        self._account_auto_queue_limit_input.setText(str(account.auto_queue_limit or 3))
        self._account_min_view_count_input.setText(
            str(account.min_view_count) if account.min_view_count else ""
        )
        self._account_min_like_count_input.setText(
            str(account.min_like_count) if account.min_like_count else ""
        )
        self._account_weight_views_input.setText(str(account.ranking_weight_views or 35))
        self._account_weight_likes_input.setText(str(account.ranking_weight_likes or 20))
        self._account_weight_recency_input.setText(str(account.ranking_weight_recency or 25))
        self._account_weight_keyword_input.setText(str(account.ranking_weight_keyword_match or 20))
        self._account_writing_tone_input.setText(account.writing_tone or "")
        self._account_target_audience_input.setText(account.target_audience or "")
        self._account_hook_style_input.setText(account.hook_style or "")
        self._account_banned_phrases_input.setText(account.banned_phrases or "")
        self._account_title_style_notes_input.setText(account.title_style_notes or "")
        self._account_caption_style_notes_input.setText(account.caption_style_notes or "")
        self._account_upload_timezone_input.setText(account.upload_timezone or "Asia/Jakarta")
        self._restore_combo_value(
            self._account_upload_privacy_combo,
            account.upload_default_privacy or "private",
        )
        self._account_upload_schedule_slots_input.setText(account.upload_schedule_slots or "")
        self._restore_combo_value(
            self._account_upload_made_for_kids_combo,
            int(account.upload_made_for_kids or 0),
        )
        self._restore_combo_value(
            self._account_upload_synthetic_media_combo,
            int(account.upload_contains_synthetic_media or 0),
        )
        self._refresh_account_action_labels(account)

    def _on_account_picker_changed(self) -> None:
        if self._suppress_account_form_sync:
            return
        if self._account_mode == "edit":
            self._populate_account_form(self._current_account())

    def _on_save_account_clicked(self) -> None:
        name = self._account_name_input.text().strip()
        if not name:
            self._notify("Account name is required.", Tone.WARNING)
            return
        scrape_max_items = 20
        scrape_max_age_days = None
        auto_queue_limit = 0
        min_view_count = 0
        min_like_count = 0
        ranking_weight_views = 35
        ranking_weight_likes = 20
        ranking_weight_recency = 25
        ranking_weight_keyword_match = 20

        platform = self._account_platform_combo.currentData() or "youtube"
        scrape_source_urls: list[str] = []
        normalized_source_count = 0

        selected = self._current_account() if self._account_mode == "edit" else None
        with get_session() as session:
            if selected is None:
                account = Account(name=name, platform=platform)
                session.add(account)
            else:
                account = session.get(Account, selected.id)
                assert account is not None
                account.name = name

            account.platform = platform
            account.niche_label = self._account_niche_input.toPlainText().strip() or None
            account.login_identifier = self._account_login_input.text().strip() or None
            # Blank stays blank (None) — never silently default to "main", which
            # would share whatever account 'main' is logged into.
            account.instagram_profile = self._account_instagram_profile_input.text().strip() or None
            account.instagram_handle = (
                self._account_instagram_handle_input.text().strip().lstrip("@") or None
            )
            account.credential_blob = self._account_credential_input.toPlainText().strip() or None
            account.scrape_source_urls = "\n".join(scrape_source_urls) or None
            account.scrape_max_items = scrape_max_items
            account.scrape_max_age_days = scrape_max_age_days
            account.discovery_keywords = None
            account.discovery_mode = "review_only"
            account.auto_queue_limit = auto_queue_limit
            account.min_view_count = min_view_count
            account.min_like_count = min_like_count
            account.ranking_weight_views = ranking_weight_views
            account.ranking_weight_likes = ranking_weight_likes
            account.ranking_weight_recency = ranking_weight_recency
            account.ranking_weight_keyword_match = ranking_weight_keyword_match
            account.writing_tone = self._account_writing_tone_input.toPlainText().strip() or None
            account.target_audience = (
                self._account_target_audience_input.toPlainText().strip() or None
            )
            account.hook_style = self._account_hook_style_input.toPlainText().strip() or None
            account.banned_phrases = (
                self._account_banned_phrases_input.toPlainText().strip() or None
            )
            account.title_style_notes = (
                self._account_title_style_notes_input.toPlainText().strip() or None
            )
            account.caption_style_notes = (
                self._account_caption_style_notes_input.toPlainText().strip() or None
            )
            account.upload_timezone = "Asia/Jakarta"
            account.upload_default_privacy = "private"
            account.upload_schedule_slots = None
            account.upload_made_for_kids = 0
            account.upload_contains_synthetic_media = 0
            session.commit()
            saved_account_id = account.id

        self._ensure_source_rows(
            account_id=saved_account_id,
            platform=platform,
            source_urls=scrape_source_urls,
        )
        self._sync_account_source_urls(saved_account_id)

        self._refresh_account_controls()
        self._current_account_id = saved_account_id
        self._restore_combo_value(self._current_account_combo, saved_account_id)
        self._restore_combo_value(self._account_picker, saved_account_id)
        self._restore_combo_value(self._account_delete_picker, saved_account_id)
        self._sync_account_panel_visibility()
        self._populate_account_form(self._current_account())
        self._clear_selection()
        if normalized_source_count > 0:
            self._notify_and_refresh(
                "Saved niche account and normalized source URLs to channel/profile roots.",
                Tone.SUCCESS,
            )
        else:
            self._notify_and_refresh("Saved niche account.", Tone.SUCCESS)
        self._show_account_main()

    def _on_delete_account_clicked(self) -> None:
        account_id = self._account_delete_picker.currentData()
        if account_id is None:
            self._notify("Choose an account to delete.", Tone.WARNING)
            return
        selected = next((account for account in self._accounts if account.id == account_id), None)
        if selected is None:
            return

        with get_session() as session:
            account = session.get(Account, selected.id)
            if account is None:
                return
            for item in (
                session.query(DownloadItem).filter(DownloadItem.account_id == selected.id).all()
            ):
                item.account_id = None
            for upload_job in (
                session.query(UploadJob).filter(UploadJob.account_id == selected.id).all()
            ):
                session.delete(upload_job)
            for candidate in (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == selected.id)
                .all()
            ):
                session.delete(candidate)
            for run in session.query(ScrapeRun).filter(ScrapeRun.account_id == selected.id).all():
                session.delete(run)
            for source in session.query(Source).filter(Source.account_id == selected.id).all():
                session.delete(source)
            session.delete(account)
            session.commit()

        if self._current_account_id == selected.id:
            self._current_account_id = None
            self._sync_account_panel_visibility()
        self._refresh_account_controls()
        self._clear_selection()
        self._notify_and_refresh("Deleted niche account.", Tone.SUCCESS)
        self._show_account_main()

    def _on_scroll_changed(self) -> None:
        self._mark_user_interacting()

    def _request_refresh(self) -> None:
        if self._processing_has_unsaved_text_edits():
            self._pending_refresh = True
            return
        if self._interaction_idle_timer.isActive():
            self._pending_refresh = True
            return
        self._apply_refresh()

    def _apply_refresh(self, force: bool = False, preserve_status: bool = False) -> None:
        current_selected_id = self._selected_item_id
        current_scroll = self._table.verticalScrollBar().value()
        with get_session() as session:
            items = (
                session.query(DownloadItem)
                .options(joinedload(DownloadItem.account))
                .order_by(DownloadItem.created_at.desc())
                .limit(200)
                .all()
            )

        filtered_items = [item for item in items if self._matches_filters(item)]
        signature = self._snapshot_signature(filtered_items)
        self._download_queue_summary.setText(
            f"{len(filtered_items)} item{'s' if len(filtered_items) != 1 else ''}"
        )
        self._download_drop_zone.setVisible(not filtered_items)
        if not force and signature == self._last_view_signature:
            self._displayed_items = filtered_items
            self._update_detail_panel(self._current_selected_item())
            if self._current_page == "processing":
                self._refresh_processing_page()
            if self._current_page == "uploads":
                self._refresh_schedule_page()
            return

        self._displayed_items = filtered_items
        self._last_view_signature = signature
        self._displayed_items = filtered_items
        if current_selected_id is not None and not any(
            item.id == current_selected_id for item in filtered_items
        ):
            self._selected_item_id = None

        if not preserve_status:
            active_account = self._active_account()
            if active_account is None:
                self._set_status(
                    "Create and select a niche account to use the library.", Tone.WARNING
                )
            elif filtered_items:
                item_count = len(filtered_items)
                self._set_status(
                    f"Showing {item_count} item{'s' if item_count != 1 else ''} for {active_account.name}.",
                    Tone.INFO,
                )
            else:
                self._set_status(f"No clips yet for {active_account.name}.", Tone.INFO)

        workspace_enabled = self._current_account_id is not None
        self._url_input.setEnabled(workspace_enabled)
        self._download_button.setEnabled(workspace_enabled)
        self._import_local_button.setEnabled(workspace_enabled)
        self._search_input.setEnabled(workspace_enabled)
        self._status_filter.setEnabled(workspace_enabled)
        self._review_filter.setEnabled(workspace_enabled)
        self._table.setEnabled(workspace_enabled)
        self._table.setColumnHidden(2, workspace_enabled)
        self._table.setColumnHidden(4, workspace_enabled)
        self._table.setColumnHidden(5, workspace_enabled)
        show_workspace = workspace_enabled
        self._library_gate_panel.setVisible(not workspace_enabled)
        self._workspace_content.setVisible(show_workspace)
        self._sync_account_panel_visibility()
        self._refresh_candidate_action_state()
        if not workspace_enabled:
            self._clear_selection()
            self._clear_source_selection()
            self._clear_candidate_selection()
            self._table.blockSignals(True)
            self._table.setRowCount(0)
            self._table.blockSignals(False)
            self._download_queue_summary.setText("Choose a niche")
            self._download_drop_zone.setVisible(True)
            self._refresh_sources()
            self._refresh_candidates(force=True)
            self._refresh_runs()
            if self._current_page == "processing":
                self._refresh_processing_page()
            if self._current_page == "uploads":
                self._refresh_schedule_page()
            self._refresh_download_batch_action_state()
            return

        self._suppress_interaction_tracking = True
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for item in filtered_items:
            row = self._table.rowCount()
            self._table.insertRow(row)

            status_item = QTableWidgetItem(item.status)
            status_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            status_item.setData(Qt.ItemDataRole.UserRole, item.id)
            background, foreground = self._status_colors(item.status)
            status_item.setBackground(background)
            status_item.setForeground(foreground)

            review_item = QTableWidgetItem(self._review_state_label(item.review_state))
            review_item.setData(Qt.ItemDataRole.UserRole, item.id)
            review_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            review_background, review_foreground = self._review_colors(item.review_state)
            review_item.setBackground(review_background)
            review_item.setForeground(review_foreground)

            account_item = QTableWidgetItem(item.account.name if item.account else "Unassigned")
            account_item.setData(Qt.ItemDataRole.UserRole, item.id)
            title_item = QTableWidgetItem(item.title or "(untitled)")
            title_item.setData(Qt.ItemDataRole.UserRole, item.id)
            title_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            source_item = QTableWidgetItem(item.source_url)
            source_item.setData(Qt.ItemDataRole.UserRole, item.id)
            file_item = QTableWidgetItem(self._output_text(item))
            file_item.setData(Qt.ItemDataRole.UserRole, item.id)
            size_item = QTableWidgetItem(self._file_size_text(item))
            size_item.setData(Qt.ItemDataRole.UserRole, item.id)
            added_item = QTableWidgetItem(self._created_text(item))
            added_item.setData(Qt.ItemDataRole.UserRole, item.id)

            self._table.setItem(row, 0, status_item)
            self._table.setItem(row, 1, review_item)
            self._table.setItem(row, 2, account_item)
            self._table.setItem(row, 3, title_item)
            self._table.setItem(row, 4, source_item)
            self._table.setItem(row, 5, file_item)
            self._table.setItem(row, 6, size_item)
            self._table.setItem(row, 7, added_item)
            self._table.setCellWidget(row, 0, self._queue_status_bar(item))

            if self._selected_item_id == item.id:
                self._table.selectRow(row)

        self._table.setShowGrid(False)
        self._table.resizeRowsToContents()
        for row in range(self._table.rowCount()):
            self._table.setRowHeight(row, 34)
        self._table.setColumnWidth(0, 132)
        self._table.setColumnWidth(1, 128)
        self._table.setColumnWidth(6, 96)
        self._table.setColumnWidth(7, 150)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.verticalScrollBar().setValue(current_scroll)
        self._table.blockSignals(False)
        self._suppress_interaction_tracking = False
        self._update_detail_panel(self._current_selected_item())
        self._refresh_sources()
        self._refresh_candidates(force=force)
        self._refresh_runs()
        if self._current_page == "processing":
            self._refresh_processing_page()
        if self._current_page == "uploads":
            self._refresh_schedule_page()
        self._refresh_download_batch_action_state()

    def _on_selection_changed(self) -> None:
        selected_ids = self._selected_item_ids()
        if not selected_ids:
            self._selected_item_id = None
            self._update_detail_panel(None)
            self._refresh_download_batch_action_state()
            return

        self._mark_user_interacting()
        self._selected_item_id = selected_ids[0]
        self._update_detail_panel(self._current_selected_item())
        self._refresh_download_batch_action_state()

    def _clear_selection(self) -> None:
        self._selected_item_id = None
        self._table.blockSignals(True)
        self._table.clearSelection()
        self._table.blockSignals(False)
        self._set_detail_placeholder()
        self._refresh_download_batch_action_state()

    def _clear_candidate_selection(self) -> None:
        self._selected_candidate_id = None
        self._candidate_table.blockSignals(True)
        self._candidate_table.clearSelection()
        self._candidate_table.blockSignals(False)
        self._refresh_candidate_action_state()

    def _clear_source_selection(self) -> None:
        self._selected_source_id = None
        self._source_table.blockSignals(True)
        self._source_table.clearSelection()
        self._source_table.blockSignals(False)
        self._refresh_source_summary()
        self._refresh_candidate_action_state()

    @staticmethod
    def _source_last_scraped_text(source: Source) -> str:
        if source.last_scraped_at is None:
            return "(never)"
        scraped_at = source.last_scraped_at
        if scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=dt.timezone.utc)
        return scraped_at.astimezone().strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _run_started_text(run: ScrapeRun) -> str:
        started_at = run.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
        return started_at.astimezone().strftime("%Y-%m-%d %H:%M")

    def _refresh_source_summary(self) -> None:
        if self._current_account_id is None:
            self._source_summary_label.setText("Select an account to manage sources.")
            return

        sources = self._displayed_sources
        selected_source = self._current_selected_source()
        if selected_source is not None:
            status_text = selected_source.last_run_status or "(idle)"
            enabled_text = "enabled" if selected_source.enabled else "disabled"
            self._source_summary_label.setText(
                (
                    f"Selected source: {selected_source.label} ({selected_source.source_type}, {enabled_text}). "
                    f"Last scrape: {self._source_last_scraped_text(selected_source)}. "
                    f"Last status: {status_text}. Use Scrape Selected to fill Candidates."
                )
            )
            return

        enabled_count = sum(1 for source in sources if source.enabled)
        disabled_count = len(sources) - enabled_count
        self._source_summary_label.setText(
            (
                f"Showing {len(sources)} source(s): "
                f"{enabled_count} enabled, {disabled_count} disabled. "
                f"Select a source, then add candidates from it."
            )
        )

    def _load_runs(self) -> list[ScrapeRun]:
        if self._current_account_id is None:
            return []
        with get_session() as session:
            return (
                session.query(ScrapeRun)
                .options(joinedload(ScrapeRun.source))
                .filter(ScrapeRun.account_id == self._current_account_id)
                .order_by(ScrapeRun.started_at.desc())
                .limit(30)
                .all()
            )

    def _refresh_sources(self) -> None:
        if self._current_account_id is None:
            self._displayed_sources = []
            self._source_table.blockSignals(True)
            self._source_table.setRowCount(0)
            self._source_table.blockSignals(False)
            self._refresh_instagram_discover_source_combo()
            self._clear_source_selection()
            return

        sources = self._load_sources_for_account(self._current_account_id)
        filter_value = self._source_filter.currentData()
        if filter_value == "enabled":
            sources = [source for source in sources if source.enabled]
        elif filter_value == "disabled":
            sources = [source for source in sources if not source.enabled]

        sort_value = self._source_sort.currentData()
        if sort_value == "status":
            sources = sorted(
                sources,
                key=lambda source: (
                    source.last_run_status or "(idle)",
                    source.label.lower(),
                    source.id,
                ),
            )
        elif sort_value == "last_scraped":
            sources = sorted(
                sources,
                key=lambda source: (
                    source.last_scraped_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                    source.label.lower(),
                    source.id,
                ),
                reverse=True,
            )
        elif sort_value == "label":
            sources = sorted(
                sources,
                key=lambda source: (source.label.lower(), source.id),
            )

        self._displayed_sources = sources
        if self._selected_source_id is not None and not any(
            source.id == self._selected_source_id for source in sources
        ):
            self._selected_source_id = None

        self._source_table.blockSignals(True)
        self._source_table.setRowCount(0)
        for source in sources:
            row = self._source_table.rowCount()
            self._source_table.insertRow(row)

            enabled_combo = NoScrollComboBox()
            enabled_combo.setObjectName("tableCombo")
            enabled_combo.addItem("Yes", 1)
            enabled_combo.addItem("No", 0)
            enabled_combo.setCurrentIndex(0 if source.enabled else 1)
            enabled_combo.currentIndexChanged.connect(
                lambda _index, source_id=source.id, combo=enabled_combo: self._on_source_enabled_changed(
                    source_id,
                    int(combo.currentData()),
                )
            )
            label_item = QTableWidgetItem(source.label)
            label_item.setData(Qt.ItemDataRole.UserRole, source.id)
            type_item = QTableWidgetItem(source.source_type)
            type_item.setData(Qt.ItemDataRole.UserRole, source.id)
            url_item = QTableWidgetItem(source.source_url)
            url_item.setData(Qt.ItemDataRole.UserRole, source.id)
            last_scraped_item = QTableWidgetItem(self._source_last_scraped_text(source))
            last_scraped_item.setData(Qt.ItemDataRole.UserRole, source.id)
            status_text = source.last_run_status or "(idle)"
            status_item = QTableWidgetItem(status_text)
            status_item.setData(Qt.ItemDataRole.UserRole, source.id)
            status_background, status_foreground = self._source_status_colors(status_text)
            status_item.setBackground(status_background)
            status_item.setForeground(status_foreground)

            self._source_table.setCellWidget(row, 0, enabled_combo)
            self._source_table.setItem(row, 1, label_item)
            self._source_table.setItem(row, 2, type_item)
            self._source_table.setItem(row, 3, url_item)
            self._source_table.setItem(row, 4, last_scraped_item)
            self._source_table.setItem(row, 5, status_item)

            if self._selected_source_id == source.id:
                self._source_table.selectRow(row)

        self._source_table.resizeRowsToContents()
        self._source_table.blockSignals(False)
        self._refresh_instagram_discover_source_combo()
        self._refresh_source_summary()
        self._refresh_candidate_action_state()

    def _refresh_instagram_discover_source_combo(self) -> None:
        if not hasattr(self, "_instagram_discover_source_combo"):
            return

        current_source_id = self._instagram_discover_source_combo.currentData()
        sources: list[Source] = []
        if self._current_account_id is not None:
            sources = [
                source
                for source in self._load_sources_for_account(self._current_account_id)
                if source.enabled
                and source.platform == "instagram"
                and source.source_type == "instagram_profile"
            ]

        self._instagram_discover_source_combo.blockSignals(True)
        self._instagram_discover_source_combo.clear()
        for source in sources:
            self._instagram_discover_source_combo.addItem(source.label, source.id)
        if sources:
            selected_index = self._instagram_discover_source_combo.findData(current_source_id)
            self._instagram_discover_source_combo.setCurrentIndex(
                selected_index if selected_index >= 0 else 0
            )
        self._instagram_discover_source_combo.blockSignals(False)

    def _refresh_runs(self) -> None:
        runs = self._load_runs()
        self._displayed_runs = runs
        self._run_table.blockSignals(True)
        self._run_table.setRowCount(0)
        for run in runs:
            row = self._run_table.rowCount()
            self._run_table.insertRow(row)

            started_item = QTableWidgetItem(self._run_started_text(run))
            source_item = QTableWidgetItem(
                run.source.label if run.source is not None else "(unknown)"
            )
            status_item = QTableWidgetItem(run.status)
            fetched_item = QTableWidgetItem(str(run.items_fetched))
            accepted_item = QTableWidgetItem(str(run.items_accepted))
            error_item = QTableWidgetItem(run.error_summary or "")

            self._run_table.setItem(row, 0, started_item)
            self._run_table.setItem(row, 1, source_item)
            self._run_table.setItem(row, 2, status_item)
            self._run_table.setItem(row, 3, fetched_item)
            self._run_table.setItem(row, 4, accepted_item)
            self._run_table.setItem(row, 5, error_item)

        self._run_table.resizeRowsToContents()
        self._run_table.blockSignals(False)

    @staticmethod
    def _candidate_date_text(value: dt.datetime | None) -> str:
        if value is None:
            return "(unknown)"
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone().strftime("%Y-%m-%d")

    @classmethod
    def _published_text(cls, candidate: ScrapeCandidate) -> str:
        return cls._candidate_date_text(candidate.published_at)

    @classmethod
    def _candidate_added_text(cls, candidate: ScrapeCandidate) -> str:
        return cls._candidate_date_text(candidate.created_at)

    @staticmethod
    def _candidate_number_text(value: int | None) -> str:
        if value is None:
            return "-"
        return f"{value:,}"

    @staticmethod
    def _candidate_duration_text(value: int | None) -> str:
        if value is None:
            return "-"
        minutes, seconds = divmod(max(value, 0), 60)
        if minutes:
            return f"{minutes}:{seconds:02d}"
        return f"{seconds}s"

    def _candidate_filter_text(self, shown_count: int, total_count: int) -> str:
        account = self._active_account()
        sort_label = self._candidate_sort_combo.currentText().replace("Sort: ", "")
        direction_label = self._candidate_sort_direction_combo.currentText().lower()
        source_label = ""
        selected_source = self._candidate_source_filter.currentData()
        if selected_source not in {None, "all"}:
            source_text = self._candidate_source_filter.currentText().replace("Source: ", "")
            source_label = f" from {source_text}"
        if account is None or account.platform != "instagram":
            return (
                f"Showing {shown_count} of {total_count} candidate(s){source_label}, "
                f"sorted by {sort_label} ({direction_label})."
            )

        min_likes = self._instagram_discover_min_likes_input.value()
        if min_likes <= 0:
            return (
                f"Showing all {shown_count} candidate(s){source_label}, "
                f"sorted by {sort_label} ({direction_label})."
            )
        hidden_count = max(total_count - shown_count, 0)
        return (
            f"Showing {shown_count} of {total_count} candidate(s){source_label} with "
            f"{min_likes:,}+ likes, sorted by {sort_label} ({direction_label}). "
            f"Lower Min likes to reveal {hidden_count} hidden candidate(s)."
        )

    def _sync_candidate_download_states(self, candidates: list[ScrapeCandidate]) -> None:
        linked_ids = [
            candidate.queued_download_item_id
            for candidate in candidates
            if candidate.queued_download_item_id is not None
        ]
        if not linked_ids:
            return

        with get_session() as session:
            linked_items = {
                item.id: item
                for item in session.query(DownloadItem)
                .filter(DownloadItem.id.in_(linked_ids))
                .all()
            }
            changed = False
            for candidate in candidates:
                if candidate.queued_download_item_id is None:
                    continue
                linked_item = linked_items.get(candidate.queued_download_item_id)
                if linked_item is None:
                    continue
                candidate_row = session.get(ScrapeCandidate, candidate.id)
                if candidate_row is None:
                    continue
                if linked_item.status == "downloaded" and candidate_row.state != "downloaded":
                    candidate_row.state = "downloaded"
                    changed = True
                elif linked_item.status == "failed" and candidate_row.state == "queued":
                    candidate_row.state = "candidate"
                    changed = True
            if changed:
                session.commit()

    def _load_candidates(self) -> list[ScrapeCandidate]:
        if self._current_account_id is None:
            return []

        all_candidates = self._load_all_candidates_for_current_account()
        self._sync_candidate_download_states(all_candidates)
        all_candidates = self._load_all_candidates_for_current_account()
        self._refresh_candidate_source_filter_options(all_candidates)

        filtered_candidates = [
            candidate
            for candidate in all_candidates
            if self._matches_candidate_state_filter(candidate)
        ]
        self._candidate_filter_label.setText(
            self._candidate_filter_text(
                shown_count=len(filtered_candidates),
                total_count=len(all_candidates),
            )
        )

        return sorted(
            filtered_candidates,
            key=self._candidate_sort_key,
            reverse=self._candidate_sort_direction_combo.currentData() != "asc",
        )

    def _load_all_candidates_for_current_account(self) -> list[ScrapeCandidate]:
        if self._current_account_id is None:
            return []

        with get_session() as session:
            return (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == self._current_account_id)
                .all()
            )

    def _refresh_candidates(self, force: bool = False) -> None:
        account = self._active_account()
        self._scrape_summary_label.setText(self._scrape_summary_text(account))

        if self._current_account_id is None:
            self._displayed_candidates = []
            self._candidate_table.blockSignals(True)
            self._candidate_table.setRowCount(0)
            self._candidate_table.blockSignals(False)
            self._clear_candidate_selection()
            return

        candidates = self._load_candidates()
        signature = self._candidate_snapshot_signature(candidates)
        if not force and signature == self._last_candidate_signature:
            self._displayed_candidates = candidates
            self._refresh_candidate_action_state()
            return

        self._displayed_candidates = candidates
        self._last_candidate_signature = signature
        if self._selected_candidate_id is not None and not any(
            item.id == self._selected_candidate_id for item in candidates
        ):
            self._selected_candidate_id = None

        self._candidate_table.blockSignals(True)
        self._candidate_table.setRowCount(0)
        for candidate in candidates:
            row = self._candidate_table.rowCount()
            self._candidate_table.insertRow(row)

            state_item = QTableWidgetItem(self._candidate_state_label(candidate.state))
            state_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            background, foreground = self._candidate_state_colors(state_item.text())
            state_item.setBackground(background)
            state_item.setForeground(foreground)
            score_item = QTableWidgetItem(self._candidate_number_text(candidate.ranking_score))
            score_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            likes_item = QTableWidgetItem(self._candidate_number_text(candidate.like_count))
            likes_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            comments_item = QTableWidgetItem(
                self._candidate_number_text(candidate.comment_count)
            )
            comments_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            duration_item = QTableWidgetItem(
                self._candidate_duration_text(candidate.duration_seconds)
            )
            duration_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            added_item = QTableWidgetItem(self._candidate_added_text(candidate))
            added_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            published_item = QTableWidgetItem(self._published_text(candidate))
            published_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            channel_item = QTableWidgetItem(candidate.channel_name or "(unknown)")
            channel_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            title_item = QTableWidgetItem(candidate.title or "(untitled)")
            title_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            match_item = QTableWidgetItem(candidate.match_reason or "(none)")
            match_item.setData(Qt.ItemDataRole.UserRole, candidate.id)

            self._candidate_table.setItem(row, 0, state_item)
            self._candidate_table.setItem(row, 1, score_item)
            self._candidate_table.setItem(row, 2, likes_item)
            self._candidate_table.setItem(row, 3, comments_item)
            self._candidate_table.setItem(row, 4, duration_item)
            self._candidate_table.setItem(row, 5, added_item)
            self._candidate_table.setItem(row, 6, published_item)
            self._candidate_table.setItem(row, 7, channel_item)
            self._candidate_table.setItem(row, 8, title_item)
            self._candidate_table.setItem(row, 9, match_item)

            if self._selected_candidate_id == candidate.id:
                self._candidate_table.selectRow(row)

        self._candidate_table.resizeRowsToContents()
        self._candidate_table.blockSignals(False)
        self._scroll_selected_candidate_into_view()
        self._refresh_candidate_action_state()

    def _scroll_selected_candidate_into_view(self) -> None:
        if self._selected_candidate_id is None:
            return
        for row in range(self._candidate_table.rowCount()):
            item = self._candidate_table.item(row, 0)
            if item is None:
                continue
            if item.data(Qt.ItemDataRole.UserRole) == self._selected_candidate_id:
                self._candidate_table.scrollToItem(
                    item,
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                return

    def _on_candidate_selection_changed(self) -> None:
        selected_items = self._candidate_table.selectedItems()
        if not selected_items:
            self._refresh_candidate_action_state()
            return

        self._selected_candidate_id = selected_items[0].data(Qt.ItemDataRole.UserRole)
        self._refresh_candidate_action_state()

    def _on_candidate_row_double_clicked(self, row: int, _column: int) -> None:
        """Open the candidate's source URL in the default browser.

        Lets the user quickly preview the original Instagram post before
        deciding to download it. The URL is read from the in-memory
        ``_displayed_candidates`` snapshot (kept in sync with the table) so
        no DB round-trip is needed.
        """
        if row < 0 or row >= len(self._displayed_candidates):
            return
        candidate = self._displayed_candidates[row]
        source_url = (candidate.source_url or "").strip()
        if not source_url:
            self._notify("This candidate has no source URL to open.", Tone.WARNING)
            return
        QDesktopServices.openUrl(QUrl(source_url))

    def _on_source_selection_changed(self) -> None:
        selected_items = self._source_table.selectedItems()
        if not selected_items:
            self._selected_source_id = self._source_id_for_current_row()
            self._refresh_source_summary()
            self._refresh_candidate_action_state()
            return

        self._selected_source_id = selected_items[0].data(Qt.ItemDataRole.UserRole)
        self._refresh_source_summary()
        self._refresh_candidate_action_state()

    def _on_source_filter_changed(self) -> None:
        self._mark_user_interacting()
        self._refresh_sources()

    def _on_source_enabled_changed(self, source_id: int, enabled_value: int) -> None:
        if self._scrape_in_progress:
            self._notify(
                "Wait for the current scrape to finish before changing source state.", Tone.WARNING
            )
            self._refresh_sources()
            return

        with get_session() as session:
            source_row = session.get(Source, source_id)
            if source_row is None:
                self._notify("Could not find the selected source.", Tone.ERROR)
                self._refresh_sources()
                return
            normalized_enabled = 1 if enabled_value else 0
            if source_row.enabled == normalized_enabled:
                return
            source_row.enabled = normalized_enabled
            session.commit()

        self._notify_and_refresh(
            "Enabled source." if enabled_value else "Disabled source.",
            Tone.INFO,
        )

    def _set_review_state_for_selected(self, review_state: str) -> None:
        item = self._current_selected_item()
        if item is None:
            return

        self._set_review_state_for_selection(review_state, item_ids=[item.id])

    def _set_review_state_for_selection(
        self,
        review_state: str,
        *,
        item_ids: list[int] | None = None,
    ) -> None:
        target_item_ids = item_ids or self._selected_item_ids()
        if not target_item_ids:
            return

        updated_count = 0

        with get_session() as session:
            rows = session.query(DownloadItem).filter(DownloadItem.id.in_(target_item_ids)).all()
            if not rows:
                return
            for item_row in rows:
                item_row.review_state = review_state
                if review_state != "kept":
                    item_row.account_id = None
                updated_count += 1
            session.commit()

        if updated_count == 1:
            message = self._review_state_message(review_state)
        else:
            plural_messages = {
                "new": f"Returned {updated_count} items to review.",
                "kept": f"Kept {updated_count} items for this account.",
                "rejected": f"Ignored {updated_count} items from this library.",
            }
            message = plural_messages.get(
                review_state,
                f"Updated {updated_count} items.",
            )

        self._notify_and_refresh(message, Tone.SUCCESS)

    def _on_detail_assign_clicked(self) -> None:
        item = self._current_selected_item()
        if item is None:
            return
        if item.review_state != "kept":
            self._notify("Keep the clip before assigning an account.", Tone.WARNING)
            return

        account_id = self._detail_account_combo.currentData()
        with get_session() as session:
            item_row = session.get(DownloadItem, item.id)
            if item_row is None:
                return
            item_row.account_id = account_id
            session.commit()

        if account_id is None:
            self._notify_and_refresh("Cleared account assignment.", Tone.INFO)
        else:
            account = next((entry for entry in self._accounts if entry.id == account_id), None)
            account_name = account.name if account is not None else "account"
            self._notify_and_refresh(f"Assigned item to {account_name}.", Tone.SUCCESS)

    def _on_scrape_clicked(self) -> None:
        job = self._build_scrape_job_for_all_enabled_sources()
        if job is None:
            return
        self._start_scrape_job(job)

    def _build_scrape_job_for_all_enabled_sources(self) -> ScrapeJobConfig | None:
        account = self._active_account()
        if account is None:
            self._notify("Create and select a niche account first.", Tone.WARNING)
            return None

        (
            sources,
            keywords,
            max_items,
            max_age_days,
            discovery_mode,
            auto_queue_limit,
            min_view_count,
            min_like_count,
            weights,
        ) = self._account_scrape_config(account)
        enabled_sources = [source for source in sources if source.enabled]
        if not enabled_sources:
            self._notify("Add at least one enabled YouTube source first.", Tone.WARNING)
            return None
        if account.platform == "instagram":
            max_items = min(max(max_items, 1), INSTAGRAM_MAX_RESULT_LIMIT)
            if not self._confirm_instagram_scrape(
                mode_label="Find Latest",
                result_limit=max_items,
                uses_latest_cursor=True,
            ):
                return None

        return ScrapeJobConfig(
            account_id=account.id,
            source_ids=[source.id for source in enabled_sources],
            keywords=keywords,
            max_items=max_items,
            max_age_days=max_age_days,
            discovery_mode=discovery_mode,
            auto_queue_limit=auto_queue_limit,
            min_view_count=min_view_count,
            min_like_count=min_like_count,
            weights=weights,
        )

    def _on_scrape_selected_clicked(self) -> None:
        account = self._active_account()
        source = self._current_selected_source()
        if account is None or source is None:
            self._notify("Select a source first.", Tone.WARNING)
            return

        (
            _sources,
            keywords,
            max_items,
            max_age_days,
            discovery_mode,
            auto_queue_limit,
            min_view_count,
            min_like_count,
            weights,
        ) = self._account_scrape_config(account)
        if account.platform == "instagram":
            max_items = min(max(max_items, 1), INSTAGRAM_MAX_RESULT_LIMIT)
            if not self._confirm_instagram_scrape(
                mode_label="Find Latest",
                result_limit=max_items,
                uses_latest_cursor=True,
            ):
                return

        self._start_scrape_job(
            ScrapeJobConfig(
                account_id=account.id,
                source_ids=[source.id],
                keywords=keywords,
                max_items=max_items,
                max_age_days=max_age_days,
                discovery_mode=discovery_mode,
                auto_queue_limit=auto_queue_limit,
                min_view_count=min_view_count,
                min_like_count=min_like_count,
                weights=weights,
            )
        )

    def _start_instagram_discover_for_source(self, account: Account, source: Source) -> bool:
        username = self._instagram_profile_username(source.source_url)
        if not username:
            self._notify("Use an Instagram profile source for Find Latest.", Tone.WARNING)
            return False

        requested_results = self._instagram_result_limit_input.value()
        if not self._confirm_instagram_scrape(
            mode_label="Find Latest",
            result_limit=requested_results,
            uses_latest_cursor=True,
        ):
            return False

        combo_index = self._instagram_discover_source_combo.findData(source.id)
        if combo_index >= 0:
            self._instagram_discover_source_combo.setCurrentIndex(combo_index)

        (
            _sources,
            keywords,
            _configured_max_items,
            max_age_days,
            discovery_mode,
            auto_queue_limit,
            min_view_count,
            min_like_count,
            weights,
        ) = self._account_scrape_config(account)
        self._append_instagram_discover_log(
            f"Find Latest for @{username}: requesting up to {requested_results} Apify result(s)."
        )

        self._start_scrape_job(
            ScrapeJobConfig(
                account_id=account.id,
                source_ids=[source.id],
                keywords=keywords,
                max_items=requested_results,
                max_age_days=max_age_days,
                discovery_mode=discovery_mode,
                auto_queue_limit=auto_queue_limit,
                min_view_count=min_view_count,
                min_like_count=min_like_count,
                weights=weights,
            )
        )
        return True

    def _on_instagram_archive_clicked(self) -> None:
        if self._scrape_in_progress or self._instagram_discover_in_progress:
            self._notify("A scrape job is already running.", Tone.WARNING)
            return

        account = self._active_account()
        if account is None:
            self._notify("Select your niche account first.", Tone.WARNING)
            return
        if account.platform != "instagram":
            self._notify("Select an Instagram niche account before archive backfill.", Tone.WARNING)
            return

        source_id = self._instagram_discover_source_combo.currentData()
        if source_id is None:
            self._notify("Add and enable an Instagram profile source first.", Tone.WARNING)
            return

        source = next(
            (source for source in self._load_sources_for_account(account.id) if source.id == int(source_id)),
            None,
        )
        if source is None:
            self._notify("The selected source no longer exists.", Tone.WARNING)
            return

        depth = self._instagram_result_limit_input.value()
        if not self._confirm_instagram_scrape(
            mode_label="Search Archive",
            result_limit=depth,
            uses_latest_cursor=False,
        ):
            return
        self._start_instagram_archive_backfill(account=account, source=source, depth=depth)

    def _confirm_instagram_scrape(
        self,
        *,
        mode_label: str,
        result_limit: int,
        uses_latest_cursor: bool,
    ) -> bool:
        estimated_cost = result_limit * 2.70 / 1000
        cursor_note = (
            "Find Latest uses the saved last-scraped date when available."
            if uses_latest_cursor
            else "Search Archive ignores the last-scraped date and dedupes locally."
        )
        response = QMessageBox.question(
            self,
            mode_label,
            (
                f"{mode_label} will request up to {result_limit} Apify results "
                f"(about ${estimated_cost:.2f} at Free-plan pricing).\n\n"
                f"{cursor_note}\n\n"
                "Existing candidates and downloads are deduped locally. Continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return response == QMessageBox.StandardButton.Yes

    def _start_instagram_archive_backfill(
        self,
        *,
        account: Account,
        source: Source,
        depth: int,
    ) -> bool:
        username = self._instagram_profile_username(source.source_url)
        if not username:
            self._notify("Use an Instagram profile source for archive backfill.", Tone.WARNING)
            return False

        combo_index = self._instagram_discover_source_combo.findData(source.id)
        if combo_index >= 0:
            self._instagram_discover_source_combo.setCurrentIndex(combo_index)

        (
            _sources,
            keywords,
            _configured_max_items,
            _max_age_days,
            discovery_mode,
            auto_queue_limit,
            _min_view_count,
            _min_like_count,
            weights,
        ) = self._account_scrape_config(account)
        self._append_instagram_discover_log(
            f"Search Archive for @{username}: requesting up to {depth} Apify result(s)."
        )
        self._start_scrape_job(
            ScrapeJobConfig(
                account_id=account.id,
                source_ids=[source.id],
                keywords=keywords,
                max_items=depth,
                max_age_days=None,
                discovery_mode=discovery_mode,
                auto_queue_limit=auto_queue_limit,
                min_view_count=0,
                min_like_count=0,
                weights=weights,
                archive_backfill=True,
            )
        )
        return True

    @staticmethod
    def _instagram_discovery_limit(min_new: int) -> int:
        return min(max(min_new, 1), INSTAGRAM_MAX_RESULT_LIMIT)

    @staticmethod
    def _instagram_discovery_scrolls(min_new: int) -> int:
        return 0

    @staticmethod
    def _instagram_profile_username(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        if cleaned.startswith("@"):
            return cleaned.lstrip("@").strip().strip("/")

        parsed = urlparse(cleaned if "://" in cleaned else f"https://instagram.com/{cleaned}")
        host = parsed.netloc.lower()
        if "instagram.com" not in host:
            return cleaned.strip().strip("/")

        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0].lower() in {"p", "reel", "tv", "stories"}:
            return ""
        return parts[0].lstrip("@").strip()

    def _on_instagram_discover_clicked(self) -> None:
        if self._scrape_in_progress or self._instagram_discover_in_progress:
            self._notify("A discovery job is already running.", Tone.WARNING)
            return

        account = self._active_account()
        if account is None:
            self._notify("Select your niche account first.", Tone.WARNING)
            return
        if account.platform != "instagram":
            self._notify("Select an Instagram niche account before discovering Instagram sources.", Tone.WARNING)
            return

        source_id = self._instagram_discover_source_combo.currentData()
        if source_id is None:
            self._notify("Add and enable an Instagram profile source first.", Tone.WARNING)
            return

        source = next(
            (source for source in self._load_sources_for_account(account.id) if source.id == int(source_id)),
            None,
        )
        if source is None:
            self._notify("The selected source no longer exists.", Tone.WARNING)
            return

        self._start_instagram_discover_for_source(account, source)

    def _start_instagram_discover_rank_job(self, job: InstagramDiscoverRankJobConfig) -> None:
        self._instagram_discover_in_progress = True
        self._instagram_discover_log.clear()
        self._append_instagram_discover_log("Starting Instagram Discover + Rank...")
        self._scrape_progress_label.setText("Running Instagram Discover + Rank...")
        self._scrape_progress_bar.setVisible(False)
        self._scrape_progress_bar.setMinimum(0)
        self._scrape_progress_bar.setMaximum(0)
        self._scrape_progress_bar.setFormat("Running Instagram Discover + Rank...")
        self._show_activity_progress(
            "Running Instagram Discover + Rank...",
            maximum=0,
            value=0,
            text_visible=True,
            fmt="Running Instagram Discover + Rank...",
        )
        self._refresh_candidate_action_state()

        thread = QThread(self)
        worker = InstagramDiscoverRankWorker(job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._append_instagram_discover_log)
        worker.completed.connect(self._on_instagram_discover_completed)
        worker.failed.connect(self._on_instagram_discover_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._instagram_discover_thread = thread
        self._instagram_discover_worker = worker
        thread.start()

    def _finish_instagram_discover_job(self) -> None:
        self._instagram_discover_in_progress = False
        self._instagram_discover_worker = None
        self._instagram_discover_thread = None
        self._scrape_progress_bar.setMaximum(1)
        self._scrape_progress_bar.setValue(1)
        self._scrape_progress_bar.setVisible(False)
        self._hide_activity_bar_if_idle()
        self._refresh_candidate_action_state()

    def _append_instagram_discover_log(self, message: str) -> None:
        self._instagram_discover_log.append(message)
        self._instagram_discover_log.moveCursor(QTextCursor.MoveOperation.End)
        self._set_status(message, Tone.INFO)

    def _on_instagram_discover_completed(self, payload: dict) -> None:
        username = str(payload.get("username") or "")
        target_account_name = str(payload.get("target_account_name") or "")
        self._finish_instagram_discover_job()
        self._scrape_progress_label.setText("")
        self._refresh_account_controls()
        self._scrape_tabs.setCurrentIndex(0)
        self._notify_and_refresh(
            f"Instagram Discover + Rank completed for @{username}"
            + (f" into {target_account_name}." if target_account_name else "."),
            Tone.SUCCESS,
        )

    def _on_instagram_discover_failed(self, message: str) -> None:
        self._finish_instagram_discover_job()
        self._scrape_progress_label.setText("")
        self._append_instagram_discover_log(f"Failed: {message}")
        self._notify_and_refresh(f"Instagram Discover + Rank failed: {message}", Tone.ERROR)

    def _start_scrape_job(self, job: ScrapeJobConfig) -> None:
        if self._scrape_in_progress:
            self._notify("A scrape is already running.", Tone.WARNING)
            return

        self._scrape_in_progress = True
        self._prepare_scrape_progress(total_sources=len(job.source_ids))
        self._scrape_progress_label.setText("Preparing scrape job...")
        self._scrape_progress_bar.setVisible(False)
        self._scrape_progress_bar.setFormat("Preparing scrape job...")
        self._show_activity_progress(
            "Preparing scrape job...",
            maximum=max(len(job.source_ids), 1),
            value=0,
            text_visible=True,
            fmt="Preparing scrape job...",
        )
        self._refresh_candidate_action_state()

        thread = QThread(self)
        worker = ScrapeWorker(self, job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_scrape_progress)
        worker.source_completed.connect(self._on_scrape_source_completed)
        worker.completed.connect(self._on_scrape_completed)
        worker.failed.connect(self._on_scrape_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._scrape_thread = thread
        self._scrape_worker = worker
        thread.start()

    def _finish_scrape_job(self) -> None:
        self._scrape_in_progress = False
        self._scrape_worker = None
        self._scrape_thread = None
        self._refresh_candidate_action_state()

    def _prepare_scrape_progress(self, *, total_sources: int) -> None:
        self._scrape_progress_bar.setVisible(False)
        self._scrape_progress_bar.setMinimum(0)
        self._scrape_progress_bar.setMaximum(max(total_sources, 1))
        self._scrape_progress_bar.setValue(0)

    def _on_scrape_progress(self, payload: dict) -> None:
        self._scrape_progress_label.setText(
            f"Scraping {payload['current']}/{payload['total']}: {payload['source_label']}"
        )
        self._scrape_progress_label.setVisible(False)
        self._scrape_progress_bar.setMaximum(max(payload["total"], 1))
        self._scrape_progress_bar.setValue(max(payload["current"] - 1, 0))
        self._scrape_progress_bar.setFormat(
            f"{max(payload['current'] - 1, 0)}/{payload['total']} sources complete"
        )
        self._show_activity_progress(
            self._scrape_progress_label.text(),
            maximum=max(payload["total"], 1),
            value=max(payload["current"] - 1, 0),
            text_visible=True,
            fmt=f"{max(payload['current'] - 1, 0)}/{payload['total']} sources complete",
        )
        self._set_status(self._scrape_progress_label.text(), Tone.INFO)

    def _on_scrape_source_completed(self, payload: dict) -> None:
        self._scrape_progress_label.setText(
            (
                f"Completed {payload['source_label']}: {payload['created']} new, "
                f"{payload['refreshed']} refreshed, {payload['skipped']} skipped, "
                f"{payload['rejected']} rejected."
            )
        )
        self._scrape_progress_label.setVisible(False)
        completed_sources = min(
            self._scrape_progress_bar.value() + 1,
            self._scrape_progress_bar.maximum(),
        )
        self._scrape_progress_bar.setValue(completed_sources)
        self._scrape_progress_bar.setFormat(
            f"{completed_sources}/{self._scrape_progress_bar.maximum()} sources complete"
        )
        self._show_activity_progress(
            self._scrape_progress_label.text(),
            maximum=self._scrape_progress_bar.maximum(),
            value=completed_sources,
            text_visible=True,
            fmt=f"{completed_sources}/{self._scrape_progress_bar.maximum()} sources complete",
        )
        self._refresh_sources()
        self._refresh_runs()
        self._refresh_candidates(force=True)

    def _on_scrape_completed(self, payload: dict) -> None:
        self._finish_scrape_job()
        self._scrape_progress_label.setText("")
        self._scrape_progress_bar.setValue(self._scrape_progress_bar.maximum())
        self._scrape_progress_bar.setFormat("Scrape complete")
        self._scrape_progress_bar.setVisible(False)
        self._hide_activity_bar_if_idle()
        if int(payload.get("created") or 0) + int(payload.get("refreshed") or 0) > 0:
            self._scrape_tabs.setCurrentIndex(0)
        self._notify_and_refresh(
            (
                f"Scraped {payload['sources']} source(s): {payload['created']} new, "
                f"{payload['refreshed']} refreshed, {payload['skipped']} skipped, "
                f"{payload['rejected']} rejected, auto-queued {payload['auto_queued']}."
            ),
            Tone.SUCCESS,
        )

    def _on_scrape_failed(self, message: str) -> None:
        self._finish_scrape_job()
        self._scrape_progress_label.setText("")
        self._scrape_progress_bar.setFormat("Scrape failed")
        self._scrape_progress_bar.setVisible(False)
        self._hide_activity_bar_if_idle()
        self._notify_and_refresh(f"Source intake failed: {message}", Tone.ERROR)

    def _run_scrape_for_source(
        self,
        *,
        account_id: int,
        source: Source,
        keywords: list[str],
        max_items: int,
        max_age_days: int | None,
        min_view_count: int,
        min_like_count: int,
        weights: DiscoveryWeights,
        archive_backfill: bool = False,
    ) -> tuple[int, int, int, int]:
        with get_session() as session:
            run = ScrapeRun(account_id=account_id, source_id=source.id, status="running")
            session.add(run)
            session.commit()
            run_id = run.id

        try:
            if source.platform == "instagram":
                # Cost-saving: latest scans ask Apify to skip posts older than
                # (last_scraped_at - 1 day). Archive backfills intentionally
                # leave since=None so they can mine older source history.
                since: dt.datetime | None = None
                if not archive_backfill and source.last_scraped_at is not None:
                    since = source.last_scraped_at - dt.timedelta(days=1)
                scraped = scrape_instagram_source_apify(
                    source_url=source.source_url,
                    max_items=max_items,
                    max_age_days=max_age_days,
                    since=since,
                )
            else:
                scraped = scrape_youtube_source(
                    source_url=source.source_url,
                    max_items=max_items,
                    max_age_days=max_age_days,
                )
            ranked_candidates = [
                rank_candidate(
                    candidate,
                    keywords=keywords,
                    weights=weights,
                    max_age_days=max_age_days,
                )
                for candidate in scraped
                if (candidate.view_count or 0) >= min_view_count
                and (candidate.like_count or 0) >= min_like_count
            ]
            rejected_count = max(len(scraped) - len(ranked_candidates), 0)
            ranked_candidates.sort(
                key=lambda candidate: (
                    candidate.ranking_score or 0,
                    candidate.view_count or 0,
                    candidate.like_count or 0,
                ),
                reverse=True,
            )

            created_count, refreshed_count, skipped_count = self._persist_scrape_candidates(
                account_id=account_id,
                source=source,
                scrape_run_id=run_id,
                candidates=ranked_candidates[:max_items],
            )

            with get_session() as session:
                source_row = session.get(Source, source.id)
                run_row = session.get(ScrapeRun, run_id)
                if source_row is not None:
                    source_row.last_scraped_at = dt.datetime.now(dt.timezone.utc)
                    source_row.last_seen_external_id = (
                        ranked_candidates[0].video_id if ranked_candidates else None
                    )
                    source_row.last_run_status = "completed"
                    source_row.last_error_summary = None
                if run_row is not None:
                    run_row.finished_at = dt.datetime.now(dt.timezone.utc)
                    run_row.status = "completed"
                    run_row.items_fetched = len(scraped)
                    run_row.items_accepted = created_count + refreshed_count
                    run_row.items_rejected = rejected_count
                    run_row.items_skipped = skipped_count
                    run_row.error_summary = None
                session.commit()
            return (created_count, refreshed_count, skipped_count, rejected_count)
        except Exception as exc:  # noqa: BLE001
            with get_session() as session:
                source_row = session.get(Source, source.id)
                run_row = session.get(ScrapeRun, run_id)
                if source_row is not None:
                    source_row.last_run_status = "failed"
                    source_row.last_error_summary = str(exc)[:200]
                if run_row is not None:
                    run_row.finished_at = dt.datetime.now(dt.timezone.utc)
                    run_row.status = "failed"
                    run_row.error_summary = str(exc)[:200]
                session.commit()
            raise

    def _on_add_scrape_source_clicked(self) -> None:
        account = self._active_account()
        if account is None:
            self._notify("Create and select a niche account first.", Tone.WARNING)
            return

        source_url = self._scrape_source_input.text().strip()
        if not source_url:
            self._notify("Paste a source URL, hashtag, or keyword first.", Tone.WARNING)
            return

        if account.platform == "instagram" and instagram_shortcode_from_url(source_url) is not None:
            self._add_manual_instagram_candidate(account=account, source_url=source_url)
            return

        normalized_source_url, validation_error = self._normalize_source_for_platform(
            source_url,
            account.platform,
        )
        if validation_error is not None:
            self._notify(validation_error, Tone.WARNING)
            return
        assert normalized_source_url is not None

        existing_urls = {source.source_url for source in self._load_sources_for_account(account.id)}
        if normalized_source_url in existing_urls:
            self._notify("This source is already configured for the current account.", Tone.WARNING)
            return

        with get_session() as session:
            source_row = Source(
                account_id=account.id,
                platform=account.platform,
                source_type=self._infer_source_type_for_platform(
                    normalized_source_url,
                    account.platform,
                ),
                label=self._source_label_for_platform(normalized_source_url, account.platform),
                source_url=normalized_source_url,
                enabled=1,
                priority=100,
            )
            session.add(source_row)
            session.commit()
            new_source_id = source_row.id

        self._selected_source_id = new_source_id
        self._sync_account_source_urls(account.id)
        self._scrape_source_input.clear()
        self._refresh_account_controls()
        self._restore_combo_value(self._current_account_combo, account.id)
        self._populate_account_form(self._active_account())
        self._refresh_sources()
        self._refresh_candidates(force=True)
        if normalized_source_url != source_url:
            self._notify(
                "Added source and normalized it to the channel/profile root URL.",
                Tone.SUCCESS,
            )
        else:
            self._notify("Added source to the current account.", Tone.SUCCESS)

    def _sync_account_source_urls(self, account_id: int) -> None:
        source_urls = [source.source_url for source in self._load_sources_for_account(account_id)]
        with get_session() as session:
            account_row = session.get(Account, account_id)
            if account_row is None:
                return
            account_row.scrape_source_urls = "\n".join(source_urls) or None
            session.commit()

    @staticmethod
    def _normalized_instagram_media_url(url: str) -> str | None:
        shortcode = instagram_shortcode_from_url(url)
        if shortcode is None:
            return None

        parsed = urlparse(url.strip())
        parts = [part for part in parsed.path.split("/") if part]
        kind = parts[0].lower() if parts else "p"
        # Map /reels/ (plural — web) to /reel/ (singular — canonical) so that
        # the same shortcode pasted in either form normalises to one URL and
        # the duplicate guard works correctly.
        if kind == "reels":
            kind = "reel"
        if kind not in {"p", "reel", "tv"}:
            kind = "p"
        return f"https://www.instagram.com/{kind}/{shortcode}/"

    def _add_manual_instagram_candidate(
        self,
        *,
        account: Account,
        source_url: str,
        clear_source_input: bool = True,
        success_message: str = "Added Instagram candidate URL.",
    ) -> None:
        normalized_url = self._normalized_instagram_media_url(source_url)
        if normalized_url is None:
            self._notify("Use an Instagram Reel or post URL.", Tone.WARNING)
            return

        shortcode = instagram_shortcode_from_url(normalized_url)
        if shortcode is None:
            self._notify("Use an Instagram Reel or post URL.", Tone.WARNING)
            return

        candidate_key = f"instagram:{shortcode}"
        duplicate_item = self._find_duplicate_for_account(normalized_url, account.id)
        if duplicate_item is not None and duplicate_item.status != "failed":
            self._selected_item_id = duplicate_item.id
            self._notify_and_refresh(
                "This Instagram URL is already in this account library.",
                Tone.WARNING,
            )
            return

        with get_session() as session:
            existing_candidate = next(
                (
                    candidate
                    for candidate in session.query(ScrapeCandidate)
                    .filter(ScrapeCandidate.account_id == account.id)
                    .all()
                    if self._candidate_video_key(candidate) == candidate_key
                ),
                None,
            )
            if existing_candidate is not None:
                self._selected_candidate_id = existing_candidate.id
                if clear_source_input:
                    self._scrape_source_input.clear()
                self._refresh_candidates(force=True)
                self._scrape_tabs.setCurrentIndex(0)
                self._notify("This Instagram URL is already in candidates.", Tone.WARNING)
                return

        candidate = self._manual_instagram_candidate_from_url(
            account=account,
            normalized_url=normalized_url,
            shortcode=shortcode,
        )

        with get_session() as session:
            candidate_row = ScrapeCandidate(
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
                account_id=account.id,
            )
            session.add(candidate_row)
            session.commit()
            self._selected_candidate_id = candidate_row.id

        if clear_source_input:
            self._scrape_source_input.clear()
        self._refresh_candidates(force=True)
        self._scrape_tabs.setCurrentIndex(0)
        self._notify(success_message, Tone.SUCCESS)

    def _manual_instagram_candidate_from_url(
        self,
        *,
        account: Account,
        normalized_url: str,
        shortcode: str,
    ) -> ScrapedVideoCandidate:
        (
            _sources,
            keywords,
            _max_items,
            max_age_days,
            _discovery_mode,
            _auto_queue_limit,
            _min_view_count,
            _min_like_count,
            weights,
        ) = self._account_scrape_config(account)

        # 1. DB cache: if this account has already seen this shortcode (as a
        #    candidate or as a download), reuse the stored metadata. This makes
        #    re-pasting the same URL free (no Apify call, no Instagram traffic).
        cached = self._cached_instagram_candidate(account_id=account.id, shortcode=shortcode)
        if cached is not None:
            return rank_candidate(
                cached,
                keywords=keywords,
                weights=weights,
                max_age_days=max_age_days,
            )

        # 2. Fresh fetch: call Apify (no Instagram account login involved).
        candidates: list[ScrapedVideoCandidate] = []
        try:
            candidates = scrape_instagram_urls_apify(
                [normalized_url],
                results_limit=1,
            )
        except (ApifyConfigError, ApifyScrapeError):
            # Apify not configured or the run failed. Fall through to the
            # stub-candidate path below so the UI still works without metadata.
            candidates = []

        if candidates:
            candidate = candidates[0]
            candidate = ScrapedVideoCandidate(
                scrape_source_url=normalized_url,
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
                discovery_query="manual",
                match_reason=candidate.match_reason,
                ranking_score=candidate.ranking_score,
            )
            return rank_candidate(
                candidate,
                keywords=keywords,
                weights=weights,
                max_age_days=max_age_days,
            )

        return ScrapedVideoCandidate(
            scrape_source_url=normalized_url,
            source_url=normalized_url,
            extractor="instagram",
            video_id=shortcode,
            title=f"Instagram media {shortcode}",
            channel_name=None,
            published_at=None,
            description=None,
            view_count=None,
            like_count=None,
            comment_count=None,
            duration_seconds=None,
            thumbnail_url=None,
            discovery_query="manual",
            match_reason="Manual Instagram URL",
            ranking_score=0,
        )

    def _on_remove_source_clicked(self) -> None:
        source = self._current_selected_source()
        if source is None:
            self._notify("Select a source first.", Tone.WARNING)
            return

        with get_session() as session:
            source_row = session.get(Source, source.id)
            if source_row is None:
                self._notify("Could not find the selected source.", Tone.ERROR)
                return
            for candidate in (
                session.query(ScrapeCandidate).filter(ScrapeCandidate.source_id == source.id).all()
            ):
                candidate.source_id = None
            for run in session.query(ScrapeRun).filter(ScrapeRun.source_id == source.id).all():
                session.delete(run)
            session.delete(source_row)
            session.commit()

        self._selected_source_id = None
        self._sync_account_source_urls(source.account_id)
        self._notify_and_refresh("Removed source.", Tone.SUCCESS)

    def _on_toggle_source_clicked(self) -> None:
        source = self._current_selected_source()
        if source is None:
            self._notify("Select a source first.", Tone.WARNING)
            return

        with get_session() as session:
            source_row = session.get(Source, source.id)
            if source_row is None:
                self._notify("Could not find the selected source.", Tone.ERROR)
                return
            source_row.enabled = 0 if source_row.enabled else 1
            enabled = bool(source_row.enabled)
            session.commit()

        self._notify_and_refresh("Enabled source." if enabled else "Disabled source.", Tone.INFO)

    def _persist_scrape_candidates(
        self,
        *,
        account_id: int,
        source: Source,
        scrape_run_id: int,
        candidates: list[ScrapedVideoCandidate],
    ) -> tuple[int, int, int]:
        created_count = 0
        refreshed_count = 0
        skipped_count = 0

        with get_session() as session:
            all_candidates = session.query(ScrapeCandidate).all()
            candidate_by_key = {
                self._candidate_video_key(candidate)
                if candidate.video_id
                else candidate.source_url: candidate
                for candidate in all_candidates
                if candidate.account_id == account_id
            }
            all_downloads = session.query(DownloadItem).all()
            download_keys_same_account = {
                self._video_key(item.source_url) or item.source_url
                for item in all_downloads
                if item.account_id == account_id
            }

            for candidate in candidates:
                candidate_key = self._candidate_video_key(candidate)
                existing_candidate = candidate_by_key.get(candidate_key)
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
                    existing_candidate.match_reason = (
                        f"{source.label}: {candidate.match_reason}"
                        if candidate.match_reason
                        else source.label
                    )
                    existing_candidate.ranking_score = candidate.ranking_score
                    existing_candidate.source_id = source.id
                    existing_candidate.scrape_run_id = scrape_run_id
                    refreshed_count += 1
                    continue

                if candidate_key in download_keys_same_account:
                    skipped_count += 1
                    continue

                candidate_row = ScrapeCandidate(
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
                    match_reason=f"{source.label}: {candidate.match_reason}"
                    if candidate.match_reason
                    else source.label,
                    ranking_score=candidate.ranking_score,
                    source_id=source.id,
                    scrape_run_id=scrape_run_id,
                    account_id=account_id,
                )
                session.add(candidate_row)
                candidate_by_key[candidate_key] = candidate_row
                created_count += 1
                continue

            session.commit()

        return (created_count, refreshed_count, skipped_count)

    def _auto_queue_top_candidates(self, *, account_id: int, limit: int) -> int:
        auto_queued_count = 0
        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(
                    ScrapeCandidate.account_id == account_id, ScrapeCandidate.state == "candidate"
                )
                .all()
            )

        candidates = sorted(
            candidates,
            key=lambda candidate: (
                candidate.ranking_score or 0,
                candidate.view_count or 0,
                candidate.like_count or 0,
            ),
            reverse=True,
        )
        for candidate in candidates[:limit]:
            duplicate_item = self._find_duplicate_for_account(candidate.source_url, account_id)
            if duplicate_item is not None:
                with get_session() as session:
                    candidate_row = session.get(ScrapeCandidate, candidate.id)
                    if candidate_row is not None:
                        candidate_row.state = "queued"
                        candidate_row.queued_download_item_id = duplicate_item.id
                        session.commit()
                continue
            item_id = QueueManager.enqueue_download(
                url=candidate.source_url,
                account_id=account_id,
                source_description=candidate.description,
            )
            with get_session() as session:
                candidate_row = session.get(ScrapeCandidate, candidate.id)
                if candidate_row is None:
                    continue
                candidate_row.state = "queued"
                candidate_row.queued_download_item_id = item_id
                session.commit()
            auto_queued_count += 1

        return auto_queued_count

    def _on_candidate_queue_clicked(self) -> None:
        candidate = self._current_selected_candidate()
        if candidate is None or self._current_account_id is None:
            self._notify("Select a candidate first.", Tone.WARNING)
            return

        duplicate_item = self._find_duplicate_for_account(
            candidate.source_url, self._current_account_id
        )
        with get_session() as session:
            candidate_row = session.get(ScrapeCandidate, candidate.id)
            if candidate_row is None:
                self._notify("Could not find the selected candidate.", Tone.ERROR)
                return

            if duplicate_item is not None:
                if duplicate_item.status == "downloaded":
                    session.commit()
                else:
                    candidate_row.state = "queued"
                    candidate_row.queued_download_item_id = duplicate_item.id
                    session.commit()
                    self._selected_item_id = duplicate_item.id
                    self._notify_and_refresh(
                        "This candidate is already in the current account library.",
                        Tone.WARNING,
                    )
                    return

        try:
            item_id = QueueManager.enqueue_download(
                url=candidate.source_url,
                account_id=self._current_account_id,
                source_description=candidate.description,
            )
            with get_session() as session:
                candidate_row = session.get(ScrapeCandidate, candidate.id)
                if candidate_row is not None:
                    candidate_row.state = "queued"
                    candidate_row.queued_download_item_id = item_id
                    session.commit()
            self._selected_item_id = item_id
            message = (
                "Queued candidate for redownload."
                if duplicate_item is not None and duplicate_item.status == "downloaded"
                else "Queued selected candidate."
            )
            self._notify_and_refresh(message, Tone.SUCCESS)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Could not queue candidate: {exc}", Tone.ERROR)

    def _on_candidate_ignore_clicked(self) -> None:
        candidate = self._current_selected_candidate()
        if candidate is None:
            self._notify("Select a candidate first.", Tone.WARNING)
            return

        with get_session() as session:
            candidate_row = session.get(ScrapeCandidate, candidate.id)
            if candidate_row is None:
                self._notify("Could not find the selected candidate.", Tone.ERROR)
                return
            candidate_row.state = "ignored"
            session.commit()

        self._notify_and_refresh("Ignored candidate for now.", Tone.INFO)

    def _on_candidate_restore_clicked(self) -> None:
        candidate = self._current_selected_candidate()
        if candidate is None:
            self._notify("Select a candidate first.", Tone.WARNING)
            return

        with get_session() as session:
            candidate_row = session.get(ScrapeCandidate, candidate.id)
            if candidate_row is None:
                self._notify("Could not find the selected candidate.", Tone.ERROR)
                return
            candidate_row.state = "candidate"
            session.commit()

        self._notify_and_refresh("Returned candidate to review.", Tone.SUCCESS)

    @staticmethod
    def _validate_youtube_url(url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return "Enter a full YouTube or Shorts URL."

        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]

        if host == "youtu.be":
            video_id = parsed.path.strip("/")
            if video_id:
                return None
            return "Enter a valid YouTube share URL."

        if host not in {"youtube.com", "m.youtube.com"}:
            return "Only YouTube and YouTube Shorts URLs are supported right now."

        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        if path == "/playlist" or "list" in query:
            return "Playlist URLs are not supported right now."

        if path == "/watch":
            video_id = query.get("v", [""])[0].strip()
            if video_id:
                return None
            return "Enter a valid YouTube watch URL."

        if path.startswith("/shorts/"):
            short_id = path.removeprefix("/shorts/").strip("/")
            if short_id:
                return None
            return "Enter a valid YouTube Shorts URL."

        if path in {"", "/"}:
            return "Use a YouTube watch, share, or Shorts URL."

        if path.startswith(("/channel/", "/c/", "/user/", "/@")):
            return "Channel and profile URLs are not supported right now."

        return "Use a YouTube watch, share, or Shorts URL."

    @staticmethod
    def _youtube_video_key(url: str) -> str | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]

        if host == "youtu.be":
            video_id = parsed.path.strip("/")
            return f"youtube:{video_id}" if video_id else None

        if host not in {"youtube.com", "m.youtube.com"}:
            return None

        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        if path == "/watch":
            video_id = query.get("v", [""])[0].strip()
            return f"youtube:{video_id}" if video_id else None

        if path.startswith("/shorts/"):
            short_id = path.removeprefix("/shorts/").strip("/")
            return f"youtube:{short_id}" if short_id else None

        return None

    @classmethod
    def _validate_supported_media_url(cls, url: str) -> str | None:
        if cls._youtube_video_key(url) is not None:
            return cls._validate_youtube_url(url)
        if cls._instagram_video_key(url) is not None:
            return validate_instagram_media_url(url)

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if "instagram.com" in host:
            return "Use an Instagram Reel or post URL."
        return cls._validate_youtube_url(url)

    @staticmethod
    def _instagram_video_key(url: str) -> str | None:
        shortcode = instagram_shortcode_from_url(url)
        return f"instagram:{shortcode}" if shortcode else None

    @classmethod
    def _video_key(cls, url: str) -> str | None:
        return cls._youtube_video_key(url) or cls._instagram_video_key(url)

    @classmethod
    def _candidate_video_key(cls, candidate: ScrapedVideoCandidate) -> str:
        if candidate.extractor == "instagram" and candidate.video_id:
            return f"instagram:{candidate.video_id}"
        if candidate.extractor == "youtube" and candidate.video_id:
            return f"youtube:{candidate.video_id}"
        return cls._video_key(candidate.source_url) or candidate.source_url

    def _cached_instagram_candidate(
        self, *, account_id: int, shortcode: str
    ) -> ScrapedVideoCandidate | None:
        """Return a previously scraped candidate for this shortcode, if any.

        Used to short-circuit the Apify call when the user re-pastes an
        Instagram URL we have already fetched metadata for. Apify charges
        per result, so this saves money on every repeat.
        """
        if not shortcode:
            return None

        with get_session() as session:
            row = (
                session.query(ScrapeCandidate)
                .filter(
                    ScrapeCandidate.account_id == account_id,
                    ScrapeCandidate.video_id == shortcode,
                )
                .order_by(ScrapeCandidate.updated_at.desc())
                .first()
            )

        if row is None:
            return None

        return ScrapedVideoCandidate(
            scrape_source_url=row.scrape_source_url,
            source_url=row.source_url,
            extractor=row.extractor,
            video_id=row.video_id,
            title=row.title,
            channel_name=row.channel_name,
            published_at=row.published_at,
            description=row.description,
            view_count=row.view_count,
            like_count=row.like_count,
            comment_count=row.comment_count,
            duration_seconds=row.duration_seconds,
            thumbnail_url=row.thumbnail_url,
            discovery_query="manual",
            match_reason=row.match_reason,
            ranking_score=row.ranking_score,
        )

    def _find_duplicate_for_account(self, url: str, account_id: int) -> DownloadItem | None:
        requested_key = self._video_key(url)
        if requested_key is None:
            return None

        with get_session() as session:
            items = (
                session.query(DownloadItem)
                .filter(DownloadItem.account_id == account_id)
                .order_by(DownloadItem.created_at.desc())
                .all()
            )

        return next(
            (item for item in items if self._video_key(item.source_url) == requested_key),
            None,
        )

    @staticmethod
    def _safe_local_import_stem(path: Path) -> str:
        safe = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_" for char in path.stem
        ).strip("._")
        return safe or "video"

    def _local_import_destination(self, source_path: Path) -> Path:
        import_dir = downloads_dir() / "local"
        import_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_stem = self._safe_local_import_stem(source_path)
        suffix = source_path.suffix.lower()
        destination = import_dir / f"local_{timestamp}_{safe_stem}{suffix}"
        counter = 1
        while destination.exists():
            destination = import_dir / f"local_{timestamp}_{safe_stem}_{counter}{suffix}"
            counter += 1
        return destination

    @staticmethod
    def _local_import_source_url(source_url: str | None, destination: Path) -> str:
        reference = (source_url or "").strip()
        if reference.lower().startswith(("http://", "https://")):
            return reference
        return f"local://{destination.name}"

    def _link_candidate_to_download(
        self,
        *,
        account_id: int,
        source_url: str,
        download_item_id: int,
    ) -> None:
        requested_key = self._video_key(source_url)
        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == account_id)
                .all()
            )
            candidate = next(
                (
                    row
                    for row in candidates
                    if (self._candidate_video_key(row) if row.video_id else row.source_url)
                    == (requested_key or source_url)
                ),
                None,
            )
            if candidate is None:
                return
            candidate.state = "queued"
            candidate.queued_download_item_id = download_item_id
            download_item = session.get(DownloadItem, download_item_id)
            if (
                download_item is not None
                and not download_item.source_description
                and candidate.description
            ):
                download_item.source_description = candidate.description
            session.commit()

    def _on_import_local_clicked(self) -> None:
        if self._current_account_id is None:
            self._notify("Create and select a niche account first.", Tone.WARNING)
            return

        # Start in the last-used folder, fall back to ~/Videos, then home.
        if self._last_import_dir is not None and self._last_import_dir.is_dir():
            start_dir = self._last_import_dir
        else:
            videos = Path.home() / "Videos"
            start_dir = videos if videos.is_dir() else Path.home()

        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import MP4",
            str(start_dir),
            "MP4 files (*.mp4);;All files (*.*)",
        )
        if not selected_path:
            return
        self._last_import_dir = Path(selected_path).parent

        self._import_local_video_path(
            Path(selected_path),
            source_url=self._url_input.text().strip() or None,
        )

    def _import_local_video_path(self, source_path: Path, *, source_url: str | None = None) -> None:
        if self._current_account_id is None:
            self._notify("Create and select a niche account first.", Tone.WARNING)
            return

        source_path = source_path.expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            self._notify("Choose an existing MP4 file to import.", Tone.WARNING)
            return
        if source_path.suffix.lower() != ".mp4":
            self._notify("Only MP4 files can be imported right now.", Tone.WARNING)
            return

        destination = self._local_import_destination(source_path)
        shutil.copy2(source_path, destination)
        stored_source_url = self._local_import_source_url(source_url, destination)
        account_id = self._current_account_id

        with get_session() as session:
            item = DownloadItem(
                source_url=stored_source_url,
                extractor="local",
                video_id=destination.stem,
                title=source_path.stem,
                file_path=str(destination),
                account_id=account_id,
                status="downloaded",
                review_state="new",
            )
            session.add(item)
            session.flush()
            item_id = item.id

            linked_candidate = (
                session.query(ScrapeCandidate)
                .filter(
                    ScrapeCandidate.account_id == account_id,
                    ScrapeCandidate.source_url == stored_source_url,
                )
                .order_by(ScrapeCandidate.created_at.desc())
                .first()
            )
            if linked_candidate is not None:
                linked_candidate.state = "downloaded"
                linked_candidate.queued_download_item_id = item_id
                item.source_description = linked_candidate.description

            session.commit()

        self._selected_item_id = item_id
        if source_url and source_url.strip() == self._url_input.text().strip():
            self._url_input.clear()
        self._refresh_candidates(force=True)
        self._notify_and_refresh("Imported local MP4.", Tone.SUCCESS)

    def _on_download_clicked(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            self._notify("Paste a URL first.", Tone.WARNING)
            return
        if self._current_account_id is None:
            self._notify("Create and select a niche account first.", Tone.WARNING)
            return

        account = self._active_account()
        if self._instagram_video_key(url):
            if account is None or account.platform != "instagram":
                self._notify(
                    "Select or create an Instagram niche account before adding Instagram URLs.",
                    Tone.WARNING,
                )
                return

        validation_error = self._validate_supported_media_url(url)
        if validation_error is not None:
            self._notify(validation_error, Tone.WARNING)
            return

        duplicate_item = self._find_duplicate_for_account(url, self._current_account_id)
        if duplicate_item is not None:
            self._selected_item_id = duplicate_item.id
            if duplicate_item.status in {"queued", "downloading"}:
                message = "This video is already queued for this account."
            elif duplicate_item.status == "downloaded":
                message = (
                    "This video is already in this account library. Use Redownload from history."
                )
            elif duplicate_item.status == "failed":
                if QueueManager.retry_item(duplicate_item.id):
                    self._selected_item_id = duplicate_item.id
                    self._url_input.clear()
                    self._notify_and_refresh("Retrying download.", Tone.INFO)
                    return
                message = "This video already failed for this account."
            else:
                message = "This video already exists in this account library."
            self._notify_and_refresh(message, Tone.WARNING)
            return

        self._download_button.setEnabled(False)
        try:
            item_id = QueueManager.enqueue_download(url=url, account_id=self._current_account_id)
            self._selected_item_id = item_id
            if account is not None and account.platform == "instagram":
                self._link_candidate_to_download(
                    account_id=account.id,
                    source_url=url,
                    download_item_id=item_id,
                )
            self._url_input.clear()
            self._notify_and_refresh("Queued download.", Tone.INFO)
        except Exception as exc:  # noqa: BLE001
            message = f"Queue failed: {exc}"
            self._notify(message, Tone.ERROR)
        finally:
            self._download_button.setEnabled(True)

    def _on_retry_clicked(self, item_id: int) -> None:
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is None:
                self._notify("Could not retry download.", Tone.ERROR)
                return
            is_redownload = item.status == "downloaded"

        if QueueManager.retry_item(item_id):
            self._selected_item_id = item_id
            message = "Redownloading video." if is_redownload else "Retrying download."
            self._notify_and_refresh(message, Tone.INFO)
        else:
            self._notify("Could not retry download.", Tone.ERROR)

    def _on_remove_clicked(self, item_id: int) -> None:
        with get_session() as session:
            item = session.get(DownloadItem, item_id)
            if item is None:
                self._notify("Could not remove history item.", Tone.ERROR)
                return
            linked_candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.queued_download_item_id == item_id)
                .all()
            )
            for candidate in linked_candidates:
                candidate.queued_download_item_id = None
                if candidate.state in {"queued", "downloaded"}:
                    candidate.state = "candidate"
            session.delete(item)
            session.commit()

        if self._selected_item_id == item_id:
            self._selected_item_id = None
        self._notify_and_refresh("Removed item from history.", Tone.SUCCESS)

    def _on_detail_open_clicked(self) -> None:
        item = self._current_selected_item()
        self._on_open_clicked(item.file_path if item else None)

    def _on_detail_reveal_clicked(self) -> None:
        item = self._current_selected_item()
        self._on_reveal_clicked(item.file_path if item else None)

    def _on_detail_retry_clicked(self) -> None:
        item = self._current_selected_item()
        if item is not None:
            self._on_retry_clicked(item.id)

    def _on_detail_remove_clicked(self) -> None:
        item = self._current_selected_item()
        if item is not None:
            self._on_remove_clicked(item.id)

    def _on_reveal_clicked(self, target: str | None) -> None:
        if not target:
            self._notify("No file to reveal yet.", Tone.WARNING)
            return

        path = Path(target)
        if not path.exists():
            self._notify("File missing.", Tone.ERROR)
            return

        self._notify("Opening file location.", Tone.SUCCESS)
        os.startfile(str(path.parent))

    def _on_open_clicked(self, target: str | None) -> None:
        if not target:
            self._notify("No file to open yet.", Tone.WARNING)
            return

        path = Path(target)
        if not path.exists():
            self._notify("File missing.", Tone.ERROR)
            return

        self._notify("Opening downloaded file.", Tone.SUCCESS)
        os.startfile(str(path))
