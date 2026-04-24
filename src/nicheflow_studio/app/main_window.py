from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import zipfile
import av
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import QObject, QSize, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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
from nicheflow_studio.db.models import Account, DownloadItem, ScrapeCandidate, ScrapeRun, Source
from nicheflow_studio.db.session import get_session, init_db, reset_db_state
from nicheflow_studio.processing.video import (
    CropSuggestion,
    CropSettings,
    VideoProbe,
    suggest_crop_settings,
    export_cropped_video,
    output_dimensions,
    probe_video,
    processed_output_path,
)
from nicheflow_studio.processing.transcription import generate_transcript_draft_in_subprocess
from nicheflow_studio.processing.smart_drafts import (
    SMART_DRAFT_OPTION_COUNT,
    SmartDrafts,
    _groq_limit_profile,
    can_generate_smart_drafts,
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


APP_STYLESHEET = """
QWidget {
    background: #101418;
    color: #e6edf3;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QFrame#panel {
    background: #161b22;
    border: 1px solid #273244;
    border-radius: 14px;
}
QFrame#sidebar {
    background: #12171e;
    border: 1px solid #273244;
    border-radius: 14px;
}
QLabel#eyebrow {
    color: #8aa0b8;
    font-size: 8pt;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
QLabel#headline {
    color: #f4f7fb;
    font-size: 14pt;
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
    background: #0f1720;
    border: 1px solid #2c3a4c;
    border-radius: 12px;
    color: #edf3f9;
    padding: 10px 14px;
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
    alternate-background-color: #151c25;
    border: 1px solid #273244;
    border-radius: 12px;
    gridline-color: #243041;
    selection-background-color: #223349;
    selection-color: #edf3f9;
}
QHeaderView::section {
    background: #161f2a;
    color: #9fb1c6;
    border: none;
    border-bottom: 1px solid #273244;
    padding: 12px 10px;
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
    background: #141d27;
    border: 1px solid #273244;
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
    background: #141d27;
    border: 1px solid #273244;
    color: #9fb2c8;
    padding: 0 10px;
    min-height: 44px;
    min-width: 92px;
    max-height: 44px;
    max-width: 112px;
    border-radius: 14px;
    text-align: left;
}
QComboBox#sidebarAccountCombo {
    background: #101820;
    border: 1px solid #2d3f55;
    border-radius: 12px;
    color: #edf3f9;
    padding: 8px 10px;
    min-height: 32px;
}
QLabel#sidebarAccountLabel {
    background: transparent;
    border: none;
    color: #8aa0b8;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.08em;
    padding: 2px 2px 0 2px;
}
QPushButton#sidebarToggle:hover {
    background: #1a2734;
    border-color: #35506d;
    color: #e6edf3;
}
QPushButton#sidebarToggle[selected="true"] {
    background: #1b2a3a;
    border-color: #4b88c7;
    color: #f4f8fc;
    border-left: 3px solid #76b7ff;
}
QPushButton#sidebarToggle:checked {
    background: #1b2a3a;
    border-color: #4b88c7;
    color: #f4f8fc;
    border-left: 3px solid #76b7ff;
}
QLabel#sidebarBrand {
    background: transparent;
    border: none;
    color: #8fa7c0;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.08em;
    padding: 2px 2px 8px 2px;
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
MODULE_PAGES = ("scraping", "downloads", "processing", "uploads")
TITLE_STYLE_PRESETS: dict[str, dict[str, object]] = {
    "clean_hook": {
        "label": "Clean Hook",
        "font_size": 54,
        "font_name": "segoe_ui",
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

TITLE_FONT_CHOICES: list[tuple[str, str]] = [
    ("Segoe UI", "segoe_ui"),
    ("Bahnschrift", "bahnschrift"),
    ("Arial Bold", "arial_bold"),
    ("Impact", "impact"),
    ("Lilita One Style", "lilita_one_style"),
    ("Grobold Style", "grobold_style"),
]


@dataclass(frozen=True)
class UiStrings:
    title: str = "NicheFlow Studio"
    eyebrow: str = "Download"
    headline: str = "Paste a YouTube link or use Scrape to build the queue"
    url_placeholder: str = "Paste a YouTube / Shorts URL..."
    add_button: str = "Download"
    history_title: str = "Download Queue"
    detail_title: str = "Selected Video"
    detail_placeholder: str = "Select a row to inspect it."


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


@dataclass(frozen=True)
class ProcessJobConfig:
    input_path: Path
    output_path: Path
    crop: CropSettings
    title_text: str | None = None
    title_font_size: int = 54
    title_font_name: str = "segoe_ui"
    title_color: str = "#FFFFFF"
    title_background: str = "none"


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
    input_path: Path | None = None
    transcript_available: bool = False
    account_voice: dict[str, str] | None = None


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
            )
            self.completed.emit({"output_path": str(output_path)})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class SuggestCropWorker(QObject):
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job: SuggestCropJobConfig) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            suggestion = suggest_crop_settings(self._job.input_path)
            self.completed.emit(
                {
                    "crop": suggestion.crop,
                    "reasons": list(suggestion.reasons),
                    "used_border_detection": suggestion.used_border_detection,
                    "used_ocr": suggestion.used_ocr,
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
                niche_label=self._job.niche_label,
                input_path=self._job.input_path,
                account_voice=self._job.account_voice,
            )
            self.completed.emit(
                {
                    "summary": drafts.summary,
                    "title_options": drafts.title_options,
                    "caption_options": drafts.caption_options,
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
        super().wheelEvent(event)
        event.accept()


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
        self._process_thread: QThread | None = None
        self._process_worker: ProcessWorker | None = None
        self._suggest_thread: QThread | None = None
        self._suggest_worker: SuggestCropWorker | None = None
        self._draft_thread: QThread | None = None
        self._draft_worker: TranscriptDraftWorker | None = None
        self._smart_draft_thread: QThread | None = None
        self._smart_draft_worker: SmartDraftWorker | None = None
        self._processing_in_progress = False
        self._processing_busy_mode: str | None = None
        self._selected_processing_item_id: int | None = None
        self._processing_item_probe_cache: dict[int, tuple[str, int, int, bool]] = {}
        self._processing_probe_item_id: int | None = None
        self._processing_probe: VideoProbe | None = None
        self._processing_last_output_path: Path | None = None
        self._processing_auto_crop = CropSettings()
        self._processing_pending_job: ProcessJobConfig | None = None
        self._processing_raw_transcript_text: str = ""
        self._processing_provider_label_text: str = ""
        self._processing_generation_meta_text: str = ""
        self._processing_vision_payload_text: str = ""
        self._processing_generated_at_text: str = ""
        self._processing_preview_path: Path | None = None
        self._processing_preview_container: av.container.InputContainer | None = None
        self._processing_preview_stream = None
        self._processing_preview_frame_iter = None
        self._processing_preview_duration_ms: int = 0
        self._processing_preview_position_ms: int = 0
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

        self._toast_label = QLabel(self)
        self._toast_label.setObjectName("toast")
        self._toast_label.setVisible(False)
        self._toast_label.setWordWrap(True)
        self._toast_label.setMinimumWidth(260)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText(self._ui.url_placeholder)

        self._download_button = QPushButton(self._ui.add_button)
        self._download_button.clicked.connect(self._on_download_clicked)
        self._sidebar_toggle_button = QPushButton()
        self._sidebar_toggle_button.setObjectName("sidebarToggle")
        self._sidebar_toggle_button.clicked.connect(self._toggle_account_sidebar)
        self._sidebar_toggle_button.setToolTip("Open account manager")
        self._sidebar_toggle_button.setCheckable(True)
        self._sidebar_toggle_button.setText("Manage")
        self._sidebar_toggle_button.setIcon(self._icon("account-manager"))
        self._sidebar_toggle_button.setIconSize(QSize(16, 16))
        self._sidebar_toggle_button.setProperty("selected", False)
        self._module_buttons: dict[str, QPushButton] = {}
        self._sidebar_account_combo = QComboBox()
        self._sidebar_account_combo.setObjectName("sidebarAccountCombo")
        self._sidebar_account_combo.currentIndexChanged.connect(self._on_sidebar_account_changed)
        self._sidebar_account_label = QLabel("Account")
        self._sidebar_account_label.setObjectName("sidebarAccountLabel")

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(self._url_input, stretch=1)
        top_row.addWidget(self._download_button)

        hero_panel = QFrame()
        hero_panel.setObjectName("panel")
        hero_layout = QVBoxLayout()
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_layout.setSpacing(8)
        hero_layout.addWidget(self._eyebrow_label)
        hero_layout.addWidget(self._headline_label)
        hero_layout.addLayout(top_row)
        hero_layout.addWidget(self._status_label)
        hero_panel.setLayout(hero_layout)

        account_title = QLabel("Account Targets")
        account_title.setObjectName("sectionTitle")

        workspace_title = QLabel("Current Workspace")
        workspace_title.setObjectName("sectionTitle")
        workspace_hint = QLabel(
            "Choose which account library you are currently working in. Downloads and review happen inside this workspace."
        )
        workspace_hint.setObjectName("subtleLabel")
        workspace_hint.setWordWrap(True)

        self._current_account_id: int | None = None
        self._current_account_combo = QComboBox()
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

        manage_title = QLabel("Manage Saved Accounts")
        manage_title.setObjectName("sectionTitle")
        manage_hint = QLabel(
            "Keep account setup separate from the active workspace. Pick a mode below, finish the action, then return to the main view."
        )
        manage_hint.setObjectName("subtleLabel")
        manage_hint.setWordWrap(True)

        self._account_picker = QComboBox()
        self._account_picker.currentIndexChanged.connect(self._on_account_picker_changed)
        self._account_name_input = QLineEdit()
        self._account_name_input.setPlaceholderText("Account name")
        self._account_platform_combo = QComboBox()
        self._account_platform_combo.addItem("youtube")
        self._account_platform_combo.setEnabled(False)
        self._account_niche_input = QLineEdit()
        self._account_niche_input.setPlaceholderText("Niche / category")
        self._account_login_input = QLineEdit()
        self._account_login_input.setPlaceholderText("Login identifier")
        self._account_credential_input = QLineEdit()
        self._account_credential_input.setPlaceholderText("Credential / session note")
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
        self._account_discovery_mode_combo = QComboBox()
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
        self._account_writing_tone_input = QLineEdit()
        self._account_writing_tone_input.setPlaceholderText("playful, direct, dramatic...")
        self._account_target_audience_input = QLineEdit()
        self._account_target_audience_input.setPlaceholderText(
            "Who this account is trying to reach"
        )
        self._account_hook_style_input = QLineEdit()
        self._account_hook_style_input.setPlaceholderText("reaction-first, curiosity, payoff...")
        self._account_banned_phrases_input = QLineEdit()
        self._account_banned_phrases_input.setPlaceholderText("Phrases to avoid")
        self._account_title_style_notes_input = QLineEdit()
        self._account_title_style_notes_input.setPlaceholderText("Short rules for titles")
        self._account_caption_style_notes_input = QLineEdit()
        self._account_caption_style_notes_input.setPlaceholderText("Short rules for captions")

        self._account_mode_label = QLabel("Main")
        self._account_mode_label.setObjectName("sectionTitle")
        self._account_mode_hint = QLabel(
            "Choose whether you want to create a new account, edit an existing one, or remove one."
        )
        self._account_mode_hint.setObjectName("subtleLabel")
        self._account_mode_hint.setWordWrap(True)

        self._account_main_new_button = QPushButton("New Account")
        self._account_main_new_button.clicked.connect(self._show_new_account_form)
        self._account_main_edit_button = QPushButton("Edit Account")
        self._account_main_edit_button.clicked.connect(self._show_edit_account_form)
        self._account_main_delete_button = QPushButton("Delete Account")
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

        self._account_picker_label = QLabel("Account")
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
        account_form.setVerticalSpacing(8)
        account_form.addWidget(QLabel("Name"), 0, 0)
        account_form.addWidget(self._account_name_input, 0, 1)
        account_form.addWidget(QLabel("Platform"), 1, 0)
        account_form.addWidget(self._account_platform_combo, 1, 1)
        account_form.addWidget(QLabel("Niche"), 2, 0)
        account_form.addWidget(self._account_niche_input, 2, 1)
        account_form.addWidget(QLabel("Identifier"), 3, 0)
        account_form.addWidget(self._account_login_input, 3, 1)
        account_form.addWidget(QLabel("Credential"), 4, 0)
        account_form.addWidget(self._account_credential_input, 4, 1)
        account_form.addWidget(QLabel("Scrape Sources"), 5, 0)
        account_form.addWidget(self._account_scrape_sources_input, 5, 1)
        account_form.addWidget(QLabel("Max Intake Items"), 6, 0)
        account_form.addWidget(self._account_scrape_max_items_input, 6, 1)
        account_form.addWidget(QLabel("Max Age Days"), 7, 0)
        account_form.addWidget(self._account_scrape_max_age_days_input, 7, 1)
        account_form.addWidget(QLabel("Discovery Keywords"), 8, 0)
        account_form.addWidget(self._account_discovery_keywords_input, 8, 1)
        account_form.addWidget(QLabel("Discovery Mode"), 9, 0)
        account_form.addWidget(self._account_discovery_mode_combo, 9, 1)
        account_form.addWidget(QLabel("Auto Queue Limit"), 10, 0)
        account_form.addWidget(self._account_auto_queue_limit_input, 10, 1)
        account_form.addWidget(QLabel("Min Views"), 11, 0)
        account_form.addWidget(self._account_min_view_count_input, 11, 1)
        account_form.addWidget(QLabel("Min Likes"), 12, 0)
        account_form.addWidget(self._account_min_like_count_input, 12, 1)
        account_form.addWidget(QLabel("Weight: Views"), 13, 0)
        account_form.addWidget(self._account_weight_views_input, 13, 1)
        account_form.addWidget(QLabel("Weight: Likes"), 14, 0)
        account_form.addWidget(self._account_weight_likes_input, 14, 1)
        account_form.addWidget(QLabel("Weight: Recency"), 15, 0)
        account_form.addWidget(self._account_weight_recency_input, 15, 1)
        account_form.addWidget(QLabel("Weight: Keyword Match"), 16, 0)
        account_form.addWidget(self._account_weight_keyword_input, 16, 1)
        account_form.addWidget(QLabel("Writing Tone"), 17, 0)
        account_form.addWidget(self._account_writing_tone_input, 17, 1)
        account_form.addWidget(QLabel("Target Audience"), 18, 0)
        account_form.addWidget(self._account_target_audience_input, 18, 1)
        account_form.addWidget(QLabel("Hook Style"), 19, 0)
        account_form.addWidget(self._account_hook_style_input, 19, 1)
        account_form.addWidget(QLabel("Banned Phrases"), 20, 0)
        account_form.addWidget(self._account_banned_phrases_input, 20, 1)
        account_form.addWidget(QLabel("Title Style Notes"), 21, 0)
        account_form.addWidget(self._account_title_style_notes_input, 21, 1)
        account_form.addWidget(QLabel("Caption Style Notes"), 22, 0)
        account_form.addWidget(self._account_caption_style_notes_input, 22, 1)

        self._account_form_panel = QWidget()
        account_form_panel_layout = QVBoxLayout()
        account_form_panel_layout.setContentsMargins(0, 0, 0, 0)
        account_form_panel_layout.setSpacing(10)
        account_form_panel_layout.addLayout(account_form)
        self._account_form_panel.setLayout(account_form_panel_layout)

        self._account_save_button = QPushButton("Create Account")
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

        self._account_delete_picker = QComboBox()
        self._account_delete_picker_label = QLabel("Choose account to delete")
        self._account_delete_picker_label.setObjectName("metaLabel")
        self._account_delete_button = QPushButton("Delete Selected Account")
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
        self._account_panel.setMinimumWidth(320)
        self._account_panel.setMaximumWidth(360)
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
        account_layout.addWidget(self._account_form_panel)
        account_layout.addWidget(self._account_form_actions)
        account_layout.addWidget(self._account_delete_panel)
        account_layout.addStretch(1)
        self._account_panel.setLayout(account_layout)

        self._sidebar_brand = QLabel("NicheFlow")
        self._sidebar_brand.setObjectName("sidebarBrand")
        self._sidebar_brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._sidebar_brand.setMinimumHeight(18)

        sidebar_modules = [
            ("scraping", "Scrape", "refresh"),
            ("downloads", "Download", "play"),
            ("processing", "Preprocess", "refresh"),
            ("uploads", "Schedule", "check"),
        ]
        self._sidebar_nav = QWidget()
        sidebar_nav_layout = QVBoxLayout()
        sidebar_nav_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_nav_layout.setSpacing(10)
        for page_name, tooltip, icon_name in sidebar_modules:
            button = QPushButton()
            button.setObjectName("sidebarToggle")
            button.setText(tooltip)
            button.setToolTip(tooltip)
            button.setCheckable(True)
            button.setIcon(self._icon(icon_name))
            button.setIconSize(QSize(16, 16))
            button.setFixedHeight(44)
            button.setProperty("selected", False)
            button.clicked.connect(
                lambda checked=False, target=page_name: self._set_current_page(target)
            )
            self._module_buttons[page_name] = button
            sidebar_nav_layout.addWidget(button)
        self._sidebar_nav.setLayout(sidebar_nav_layout)
        self._sidebar_nav.setFixedHeight(206)

        self._sidebar_panel = QFrame()
        self._sidebar_panel.setObjectName("sidebar")
        self._sidebar_panel.setFixedWidth(188)
        sidebar_layout = QVBoxLayout()
        sidebar_layout.setContentsMargins(12, 12, 12, 14)
        sidebar_layout.setSpacing(12)
        sidebar_layout.addWidget(self._sidebar_brand)
        sidebar_layout.addWidget(self._sidebar_nav)
        sidebar_layout.addStretch(1)
        sidebar_layout.addWidget(self._sidebar_account_label)
        sidebar_layout.addWidget(self._sidebar_account_combo)
        sidebar_layout.addWidget(
            self._sidebar_toggle_button,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        self._sidebar_panel.setLayout(sidebar_layout)

        history_title = QLabel(self._ui.history_title)
        history_title.setObjectName("sectionTitle")

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search title or source URL...")
        self._search_input.textChanged.connect(self._on_search_changed)

        self._status_filter = QComboBox()
        self._status_filter.addItems(
            ["All statuses", "queued", "downloading", "downloaded", "failed"]
        )
        self._status_filter.currentIndexChanged.connect(self._on_status_filter_changed)

        self._review_filter = QComboBox()
        self._review_filter.addItem("All review states", "all")
        for label, state in REVIEW_STATE_OPTIONS:
            self._review_filter.addItem(label, state)
        self._review_filter.currentIndexChanged.connect(self._on_status_filter_changed)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        filter_row.addWidget(self._search_input, stretch=1)
        filter_row.addWidget(self._status_filter)
        self._review_filter.setVisible(False)

        self._table = TableFocusScrollWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(
            ["Status", "Review", "Account", "Title", "Source URL", "Output"]
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
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.verticalScrollBar().setSingleStep(18)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)

        self._batch_keep_button = QPushButton("Keep Selected")
        self._batch_keep_button.clicked.connect(
            lambda: self._set_review_state_for_selection("kept")
        )
        self._batch_ignore_button = QPushButton("Ignore Selected")
        self._batch_ignore_button.setObjectName("ghostButton")
        self._batch_ignore_button.clicked.connect(
            lambda: self._set_review_state_for_selection("rejected")
        )
        self._batch_return_button = QPushButton("Return Selected To Review")
        self._batch_return_button.setObjectName("ghostButton")
        self._batch_return_button.clicked.connect(
            lambda: self._set_review_state_for_selection("new")
        )

        batch_row = QHBoxLayout()
        batch_row.setSpacing(10)
        batch_row.addWidget(self._batch_keep_button)
        batch_row.addWidget(self._batch_ignore_button)
        batch_row.addWidget(self._batch_return_button)
        batch_row.addStretch(1)

        history_panel = QFrame()
        history_panel.setObjectName("panel")
        history_layout = QVBoxLayout()
        history_layout.setContentsMargins(18, 18, 18, 18)
        history_layout.setSpacing(12)
        history_layout.addWidget(history_title)
        history_layout.addLayout(filter_row)
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
        history_panel.setLayout(history_layout)

        intake_title = QLabel("Source Intake")
        intake_title.setObjectName("sectionTitle")
        intake_hint = QLabel(
            "Fetch ranked candidates from this account's YouTube sources and automatic keyword discovery settings."
        )
        intake_hint.setObjectName("subtleLabel")
        intake_hint.setWordWrap(True)
        self._scrape_summary_label = QLabel("Select an account to configure source intake.")
        self._scrape_summary_label.setObjectName("subtleLabel")
        self._scrape_summary_label.setWordWrap(True)
        self._scrape_progress_label = QLabel("")
        self._scrape_progress_label.setObjectName("subtleLabel")
        self._scrape_progress_label.setWordWrap(True)
        self._scrape_progress_bar = QProgressBar()
        self._scrape_progress_bar.setTextVisible(True)
        self._scrape_progress_bar.setVisible(False)
        self._scrape_progress_bar.setMinimum(0)
        self._scrape_progress_bar.setMaximum(1)
        self._scrape_progress_bar.setValue(0)
        self._source_summary_label = QLabel("No source selected.")
        self._source_summary_label.setObjectName("subtleLabel")
        self._source_summary_label.setWordWrap(True)
        self._scrape_source_input = QLineEdit()
        self._scrape_source_input.setPlaceholderText("Add YouTube channel/profile URL...")
        self._scrape_add_source_button = QPushButton("Add Source")
        self._scrape_add_source_button.clicked.connect(self._on_add_scrape_source_clicked)
        self._source_filter = QComboBox()
        self._source_filter.addItem("All sources", "all")
        self._source_filter.addItem("Enabled only", "enabled")
        self._source_filter.addItem("Disabled only", "disabled")
        self._source_filter.currentIndexChanged.connect(self._on_source_filter_changed)
        self._source_sort = QComboBox()
        self._source_sort.addItem("Sort: Priority", "priority")
        self._source_sort.addItem("Sort: Status", "status")
        self._source_sort.addItem("Sort: Last scraped", "last_scraped")
        self._source_sort.addItem("Sort: Label", "label")
        self._source_sort.currentIndexChanged.connect(self._on_source_filter_changed)
        self._source_remove_button = QPushButton("Remove Source")
        self._source_remove_button.setObjectName("ghostButton")
        self._source_remove_button.clicked.connect(self._on_remove_source_clicked)
        self._source_toggle_button = QPushButton("Disable Source")
        self._source_toggle_button.setObjectName("ghostButton")
        self._source_toggle_button.clicked.connect(self._on_toggle_source_clicked)
        self._scrape_selected_button = QPushButton("Scrape Selected")
        self._scrape_selected_button.clicked.connect(self._on_scrape_selected_clicked)
        self._scrape_button = QPushButton("Scrape All Enabled")
        self._scrape_button.clicked.connect(self._on_scrape_clicked)

        intake_source_row = QHBoxLayout()
        intake_source_row.setSpacing(10)
        intake_source_row.addWidget(self._scrape_source_input, stretch=1)
        intake_source_row.addWidget(self._scrape_add_source_button)

        source_filter_row = QHBoxLayout()
        source_filter_row.setSpacing(10)
        source_filter_row.addWidget(self._source_filter)
        source_filter_row.addWidget(self._source_sort)
        source_filter_row.addStretch(1)

        self._source_table = TableFocusScrollWidget()
        self._source_table.setColumnCount(6)
        self._source_table.setHorizontalHeaderLabels(
            ["Enabled", "Label", "Type", "Source URL", "Last Scraped", "Last Status"]
        )
        self._source_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._source_table.horizontalHeader().setStretchLastSection(True)
        self._source_table.verticalHeader().setVisible(False)
        self._source_table.setAlternatingRowColors(True)
        self._source_table.setShowGrid(True)
        self._source_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._source_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._source_table.setMinimumHeight(200)
        self._source_table.itemSelectionChanged.connect(self._on_source_selection_changed)

        source_actions = QHBoxLayout()
        source_actions.setSpacing(10)
        source_actions.addWidget(self._source_remove_button)
        source_actions.addWidget(self._source_toggle_button)
        source_actions.addWidget(self._scrape_selected_button)
        source_actions.addWidget(self._scrape_button)

        self._candidate_table = TableFocusScrollWidget()
        self._candidate_table.setColumnCount(8)
        self._candidate_table.setHorizontalHeaderLabels(
            ["State", "Score", "Views", "Likes", "Published", "Channel", "Title", "Match"]
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
        self._candidate_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._candidate_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._candidate_table.setWordWrap(False)
        self._candidate_table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._candidate_table.verticalScrollBar().setSingleStep(18)
        self._candidate_table.setMinimumHeight(340)
        self._candidate_table.itemSelectionChanged.connect(self._on_candidate_selection_changed)

        self._candidate_queue_button = QPushButton("Queue Selected Candidate")
        self._candidate_queue_button.clicked.connect(self._on_candidate_queue_clicked)
        self._candidate_queue_button.setIcon(self._icon("play"))
        self._candidate_queue_button.setIconSize(QSize(16, 16))
        self._candidate_ignore_button = QPushButton("Ignore For Now")
        self._candidate_ignore_button.setObjectName("ghostButton")
        self._candidate_ignore_button.clicked.connect(self._on_candidate_ignore_clicked)
        self._candidate_ignore_button.setIcon(self._icon("trash"))
        self._candidate_ignore_button.setIconSize(QSize(16, 16))
        self._candidate_restore_button = QPushButton("Return To Review")
        self._candidate_restore_button.setObjectName("ghostButton")
        self._candidate_restore_button.clicked.connect(self._on_candidate_restore_clicked)
        self._candidate_restore_button.setIcon(self._icon("refresh"))
        self._candidate_restore_button.setIconSize(QSize(16, 16))
        self._candidate_action_hint = QLabel("Select a candidate to review it.")
        self._candidate_action_hint.setObjectName("subtleLabel")
        self._candidate_action_hint.setWordWrap(True)
        self._candidate_state_filter = QComboBox()
        self._candidate_state_filter.addItem("All candidates", "all")
        self._candidate_state_filter.addItem("Ready to review", "candidate")
        self._candidate_state_filter.addItem("Queued for download", "queued")
        self._candidate_state_filter.addItem("Already downloaded", "downloaded")
        self._candidate_state_filter.addItem("Ignored for now", "ignored")
        self._candidate_state_filter.currentIndexChanged.connect(self._on_candidate_filter_changed)

        intake_actions = QHBoxLayout()
        intake_actions.setSpacing(10)
        intake_actions.addWidget(self._candidate_state_filter)
        intake_actions.addStretch(1)
        intake_actions.addWidget(self._candidate_queue_button)
        intake_actions.addWidget(self._candidate_ignore_button)
        intake_actions.addWidget(self._candidate_restore_button)

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
        self._run_table.setMinimumHeight(320)

        self._scrape_tabs = QTabWidget()
        self._scrape_tabs.setObjectName("panel")

        source_tab = QWidget()
        source_tab_layout = QVBoxLayout()
        source_tab_layout.setContentsMargins(0, 0, 0, 0)
        source_tab_layout.setSpacing(12)
        source_tab_layout.addLayout(intake_source_row)
        source_tab_layout.addLayout(source_filter_row)
        source_tab_layout.addWidget(self._source_summary_label)
        source_tab_layout.addWidget(self._source_table, stretch=1)
        source_tab_layout.addLayout(source_actions)
        source_tab.setLayout(source_tab_layout)

        candidate_tab = QWidget()
        candidate_tab_layout = QVBoxLayout()
        candidate_tab_layout.setContentsMargins(0, 0, 0, 0)
        candidate_tab_layout.setSpacing(12)
        candidate_tab_layout.addWidget(self._candidate_action_hint)
        candidate_tab_layout.addLayout(intake_actions)
        candidate_tab_layout.addWidget(self._candidate_table, stretch=1)
        candidate_tab.setLayout(candidate_tab_layout)

        run_tab = QWidget()
        run_tab_layout = QVBoxLayout()
        run_tab_layout.setContentsMargins(0, 0, 0, 0)
        run_tab_layout.setSpacing(12)
        run_tab_layout.addWidget(self._run_table, stretch=1)
        run_tab.setLayout(run_tab_layout)

        self._scrape_tabs.addTab(source_tab, "Sources")
        self._scrape_tabs.addTab(candidate_tab, "Candidates")
        self._scrape_tabs.addTab(run_tab, "Activity")

        intake_panel = QFrame()
        intake_panel.setObjectName("panel")
        intake_layout = QVBoxLayout()
        intake_layout.setContentsMargins(18, 18, 18, 18)
        intake_layout.setSpacing(12)
        intake_layout.addWidget(intake_title)
        intake_layout.addWidget(intake_hint)
        intake_layout.addWidget(self._scrape_summary_label)
        intake_layout.addWidget(self._scrape_progress_label)
        intake_layout.addWidget(self._scrape_progress_bar)
        intake_layout.addWidget(self._scrape_tabs, stretch=1)
        intake_panel.setLayout(intake_layout)

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
            ("Status", "status"),
            ("Review", "review"),
            ("Account", "account"),
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
        self._detail_account_combo = QComboBox()
        self._detail_account_combo.setMinimumWidth(220)
        self._detail_assign_button = QPushButton("Save Account Assignment")
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

        library_row = QHBoxLayout()
        library_row.setSpacing(16)
        library_row.addWidget(history_panel, stretch=2)
        library_row.addWidget(self._detail_panel, stretch=3)

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
        self._workspace_stack = QStackedWidget()
        self._workspace_stack.addWidget(self._scraping_page)
        self._workspace_stack.addWidget(self._downloads_page)
        self._workspace_stack.addWidget(self._processing_page)
        self._workspace_stack.addWidget(self._uploads_page)
        workspace_content_layout.addWidget(self._workspace_stack, stretch=1)
        self._workspace_content.setLayout(workspace_content_layout)

        workspace_column = QVBoxLayout()
        workspace_column.setContentsMargins(0, 0, 0, 0)
        workspace_column.setSpacing(16)
        workspace_column.addWidget(self._library_gate_panel)
        workspace_column.addWidget(self._workspace_content, stretch=1)

        workspace_panel = QWidget()
        workspace_panel.setLayout(workspace_column)

        body_row = QHBoxLayout()
        body_row.setSpacing(16)
        body_row.addWidget(self._sidebar_panel, stretch=0)
        body_row.addWidget(self._account_panel, stretch=0)
        body_row.addWidget(workspace_panel, stretch=1)

        content = QWidget()
        root = QVBoxLayout()
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)
        root.addLayout(body_row, stretch=1)
        content.setLayout(root)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setWidget(content)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll_area)

        self.setLayout(outer)
        self.setMinimumSize(self._minimum_window_width, self._minimum_window_height)
        self.resize(self._default_window_width, self._default_window_height)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(8000)
        self._refresh_timer.timeout.connect(self._request_refresh)
        self._refresh_timer.start()

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
    def _review_state_label(review_state: str) -> str:
        labels = {
            "new": "ready",
            "kept": "kept",
            "rejected": "ignored",
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
            return "Ready to review. Keep it for this account or ignore it from the library."
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

    def _make_processing_page(self) -> QWidget:
        title_label = QLabel("Preprocess")
        title_label.setObjectName("sectionTitle")
        message_label = QLabel(
            "Generate editable context, title, and caption drafts for one downloaded video, "
            "then export the prepared clip into the local processed folder."
        )
        message_label.setObjectName("metaValue")
        message_label.setWordWrap(True)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout()
        panel_layout.setContentsMargins(24, 24, 24, 24)
        panel_layout.setSpacing(14)
        panel_layout.addWidget(title_label)
        panel_layout.addWidget(message_label)

        selector_row = QHBoxLayout()
        selector_row.setSpacing(10)
        selector_label = QLabel("Downloaded video")
        selector_label.setObjectName("metaLabel")
        self._processing_item_combo = QComboBox()
        self._processing_item_combo.currentIndexChanged.connect(self._on_processing_item_changed)
        selector_row.addWidget(selector_label)
        selector_row.addWidget(self._processing_item_combo, stretch=1)
        panel_layout.addLayout(selector_row)

        self._processing_summary_label = QLabel("Select an account workspace to prepare videos.")
        self._processing_summary_label.setObjectName("subtleLabel")
        self._processing_summary_label.setWordWrap(True)
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
        self._processing_preview_mode_combo = QComboBox()
        self._processing_preview_mode_combo.addItem("Original Video", "source")
        self._processing_preview_mode_combo.addItem("Processed Output", "output")
        self._processing_preview_mode_combo.currentIndexChanged.connect(
            self._on_processing_preview_mode_changed
        )
        preview_header.addWidget(preview_title)
        preview_header.addStretch(1)
        preview_header.addWidget(self._processing_preview_mode_combo)
        preview_layout.addLayout(preview_header)

        self._processing_video_widget = QLabel("Preview unavailable")
        self._processing_video_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._processing_video_widget.setFixedHeight(360)
        self._processing_video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._processing_video_widget.setObjectName("metaValue")
        preview_layout.addWidget(self._processing_video_widget)

        preview_action_row = QHBoxLayout()
        preview_action_row.setSpacing(10)
        self._processing_preview_back_button = QPushButton("-5s")
        self._processing_preview_back_button.setObjectName("ghostButton")
        self._processing_preview_back_button.clicked.connect(
            lambda: self._shift_processing_preview(-5000)
        )
        preview_action_row.addWidget(self._processing_preview_back_button)
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
        self._processing_preview_forward_button = QPushButton("+5s")
        self._processing_preview_forward_button.setObjectName("ghostButton")
        self._processing_preview_forward_button.clicked.connect(
            lambda: self._shift_processing_preview(5000)
        )
        preview_action_row.addWidget(self._processing_preview_forward_button)
        preview_action_row.addStretch(1)
        preview_layout.addLayout(preview_action_row)

        self._processing_preview_meta_label = QLabel(
            "Select a downloaded video to preview it here."
        )
        self._processing_preview_meta_label.setObjectName("subtleLabel")
        self._processing_preview_meta_label.setWordWrap(True)
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
        text_title = QLabel("LLM Drafts")
        text_title.setObjectName("metaLabel")
        self._processing_loading_badge = QLabel("")
        self._processing_loading_badge.setObjectName("statusLabel")
        self._processing_loading_badge.setVisible(False)
        self._processing_generate_drafts_button = QPushButton("Generate Drafts")
        self._processing_generate_drafts_button.setObjectName("ghostButton")
        self._processing_generate_drafts_button.clicked.connect(
            self._on_generate_text_drafts_clicked
        )
        self._processing_save_drafts_button = QPushButton("Save Text Drafts")
        self._processing_save_drafts_button.clicked.connect(self._on_save_text_drafts_clicked)
        text_header.addWidget(text_title)
        text_header.addStretch(1)
        text_header.addWidget(self._processing_loading_badge)
        text_header.addWidget(self._processing_generate_drafts_button)
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
        text_layout.addWidget(context_label)
        text_layout.addWidget(self._processing_transcript_input)

        title_draft_label = QLabel("Applied Title")
        title_draft_label.setObjectName("metaLabel")
        self._processing_title_draft_input = QLineEdit()
        self._processing_title_draft_input.setPlaceholderText("Generated or edited title...")
        text_layout.addWidget(title_draft_label)
        text_layout.addWidget(self._processing_title_draft_input)

        caption_draft_label = QLabel("Applied Caption")
        caption_draft_label.setObjectName("metaLabel")
        self._processing_caption_draft_input = QTextEdit()
        self._processing_caption_draft_input.setPlaceholderText("Generated or edited caption...")
        self._processing_caption_draft_input.setMinimumHeight(120)
        text_layout.addWidget(caption_draft_label)
        text_layout.addWidget(self._processing_caption_draft_input)

        self._processing_draft_status_label = QLabel(
            "Generate video-aware drafts from transcript, metadata, and sampled frames when available."
        )
        self._processing_draft_status_label.setObjectName("subtleLabel")
        self._processing_draft_status_label.setWordWrap(True)
        text_layout.addWidget(self._processing_draft_status_label)

        self._processing_smart_summary_label = QLabel(
            "Smart draft summary will appear here after Groq generation."
        )
        self._processing_smart_summary_label.setObjectName("metaValue")
        self._processing_smart_summary_label.setWordWrap(True)
        text_layout.addWidget(self._processing_smart_summary_label)

        self._processing_eval_provider_label = QLabel(
            "Provider metadata will appear here after smart generation."
        )
        self._processing_eval_provider_label.setObjectName("subtleLabel")
        self._processing_eval_provider_label.setWordWrap(True)
        text_layout.addWidget(self._processing_eval_provider_label)

        self._processing_usage_label = QLabel(
            "Usage budget will appear here after smart generation is configured."
        )
        self._processing_usage_label.setObjectName("subtleLabel")
        self._processing_usage_label.setWordWrap(True)
        text_layout.addWidget(self._processing_usage_label)

        self._processing_debug_toggle = QPushButton("Show Generation Details")
        self._processing_debug_toggle.setObjectName("ghostButton")
        self._processing_debug_toggle.setCheckable(True)
        self._processing_debug_toggle.toggled.connect(self._on_processing_debug_toggled)
        text_layout.addWidget(self._processing_debug_toggle)

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
        text_layout.addWidget(self._processing_smart_cards_status_label)

        self._processing_smart_option_buttons: list[QPushButton] = []
        self._processing_smart_option_title_inputs: list[QLineEdit] = []
        self._processing_smart_option_caption_inputs: list[QTextEdit] = []
        self._processing_smart_option_pairs: list[tuple[str | None, str | None]] = []
        smart_cards_layout = QGridLayout()
        smart_cards_layout.setHorizontalSpacing(10)
        smart_cards_layout.setVerticalSpacing(10)
        for index in range(SMART_DRAFT_OPTION_COUNT):
            card_panel = QFrame()
            card_panel.setObjectName("panel")
            card_layout = QVBoxLayout()
            card_layout.setContentsMargins(14, 14, 14, 14)
            card_layout.setSpacing(8)
            card_label = QLabel(f"Option {index + 1}")
            card_label.setObjectName("metaLabel")
            card_layout.addWidget(card_label)

            title_input = QLineEdit()
            title_input.setPlaceholderText("Generated title option...")
            self._processing_smart_option_title_inputs.append(title_input)
            card_layout.addWidget(title_input)

            caption_input = QTextEdit()
            caption_input.setObjectName("smartOptionEdit")
            caption_input.setPlaceholderText("Generated caption option...")
            caption_input.setMinimumHeight(88)
            self._processing_smart_option_caption_inputs.append(caption_input)
            card_layout.addWidget(caption_input)

            apply_button = QPushButton("Apply This Option")
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

        style_panel = QFrame()
        style_panel.setObjectName("panel")
        style_layout = QGridLayout()
        style_layout.setContentsMargins(16, 16, 16, 16)
        style_layout.setHorizontalSpacing(12)
        style_layout.setVerticalSpacing(10)

        title_style_label = QLabel("Title Style")
        title_style_label.setObjectName("metaLabel")
        self._processing_title_style_combo = QComboBox()
        for key, config in TITLE_STYLE_PRESETS.items():
            self._processing_title_style_combo.addItem(str(config["label"]), key)
        self._processing_title_style_combo.currentIndexChanged.connect(
            self._on_title_style_preset_changed
        )
        style_layout.addWidget(title_style_label, 0, 0)
        style_layout.addWidget(self._processing_title_style_combo, 0, 1)

        title_size_label = QLabel("Title Size")
        title_size_label.setObjectName("metaLabel")
        self._processing_title_font_size = QSpinBox()
        self._processing_title_font_size.setRange(18, 144)
        style_layout.addWidget(title_size_label, 0, 2)
        style_layout.addWidget(self._processing_title_font_size, 0, 3)

        title_font_label = QLabel("Title Font")
        title_font_label.setObjectName("metaLabel")
        self._processing_title_font_combo = QComboBox()
        for label, key in TITLE_FONT_CHOICES:
            self._processing_title_font_combo.addItem(label, key)
        style_layout.addWidget(title_font_label, 1, 0)
        style_layout.addWidget(self._processing_title_font_combo, 1, 1)

        title_color_label = QLabel("Title Color")
        title_color_label.setObjectName("metaLabel")
        self._processing_title_color_input = QLineEdit()
        self._processing_title_color_input.setPlaceholderText("#FFFFFF")
        style_layout.addWidget(title_color_label, 1, 2)
        style_layout.addWidget(self._processing_title_color_input, 1, 3)

        title_background_label = QLabel("Title Background")
        title_background_label.setObjectName("metaLabel")
        self._processing_title_background_combo = QComboBox()
        self._processing_title_background_combo.addItem("None", "none")
        self._processing_title_background_combo.addItem("Dark Box", "dark")
        self._processing_title_background_combo.addItem("Light Box", "light")
        style_layout.addWidget(title_background_label, 2, 0)
        style_layout.addWidget(self._processing_title_background_combo, 2, 1)

        self._processing_style_status_label = QLabel(
            "These controls only affect the rendered title on the processed video. Captions stay as editable metadata for later upload."
        )
        self._processing_style_status_label.setObjectName("subtleLabel")
        self._processing_style_status_label.setWordWrap(True)
        style_layout.addWidget(self._processing_style_status_label, 3, 0, 1, 4)
        style_panel.setLayout(style_layout)
        panel_layout.addWidget(style_panel)
        text_panel.setLayout(text_layout)
        panel_layout.addWidget(text_panel)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self._processing_export_button = QPushButton("Export Processed Video")
        self._processing_export_button.clicked.connect(self._on_process_video_clicked)
        self._processing_open_processed_button = QPushButton("Open Processed Folder")
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
        self._processing_open_latest_output_button = QPushButton("Open Latest Output")
        self._processing_open_latest_output_button.setObjectName("ghostButton")
        self._processing_open_latest_output_button.clicked.connect(
            self._on_open_latest_processed_output_clicked
        )
        latest_output_row.addWidget(latest_output_label)
        latest_output_row.addWidget(self._processing_latest_output_label, stretch=1)
        latest_output_row.addWidget(self._processing_open_latest_output_button)
        panel_layout.addLayout(latest_output_row)

        self._processing_suggestion_label = QLabel(
            "Processing now auto-detects the crop and renders the applied title onto the video. "
            "You no longer need to tune crop margins manually."
        )
        self._processing_suggestion_label.setObjectName("subtleLabel")
        self._processing_suggestion_label.setWordWrap(True)
        panel_layout.addWidget(self._processing_suggestion_label)

        panel_layout.addStretch(1)
        panel.setLayout(panel_layout)

        self._processing_preview_timer = QTimer(self)
        self._processing_preview_timer.setInterval(33)
        self._processing_preview_timer.timeout.connect(self._advance_processing_preview)

        page = QWidget()
        page_layout = QVBoxLayout()
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(panel)
        page.setLayout(page_layout)
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
        title_label = QLabel("Schedule")
        title_label.setObjectName("sectionTitle")
        message_label = QLabel(
            "Review processed drafts that are ready for a future uploader. This queue does not publish yet."
        )
        message_label.setObjectName("metaValue")
        message_label.setWordWrap(True)

        self._schedule_summary_label = QLabel(
            "Select an account workspace to review schedule drafts."
        )
        self._schedule_summary_label.setObjectName("subtleLabel")
        self._schedule_summary_label.setWordWrap(True)

        self._schedule_table = TableFocusScrollWidget()
        self._schedule_table.setColumnCount(5)
        self._schedule_table.setHorizontalHeaderLabels(
            ["Account", "Video", "Title", "Caption", "Status"]
        )
        self._schedule_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._schedule_table.horizontalHeader().setStretchLastSection(True)
        self._schedule_table.verticalHeader().setVisible(False)
        self._schedule_table.setAlternatingRowColors(True)
        self._schedule_table.setShowGrid(True)
        self._schedule_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._schedule_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._schedule_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._schedule_table.setWordWrap(False)
        self._schedule_table.setTextElideMode(Qt.TextElideMode.ElideRight)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout()
        panel_layout.setContentsMargins(24, 24, 24, 24)
        panel_layout.setSpacing(12)
        panel_layout.addWidget(title_label)
        panel_layout.addWidget(message_label)
        panel_layout.addWidget(self._schedule_summary_label)
        panel_layout.addWidget(self._schedule_table, stretch=1)
        panel.setLayout(panel_layout)

        page = QWidget()
        page_layout = QVBoxLayout()
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(panel)
        page.setLayout(page_layout)
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
            self._set_processing_placeholder_state(
                "No downloaded files are ready for processing in this account yet."
            )
            return

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

    def _set_processing_placeholder_state(self, message: str) -> None:
        self._processing_summary_label.setText(message)
        self._processing_preview_path = None
        self._processing_preview_mode = "source"
        self._stop_processing_preview()
        self._processing_toggle_preview_button.setText("Play Full Video")
        self._processing_preview_position_slider.setRange(0, 0)
        self._processing_preview_position_slider.setValue(0)
        self._processing_preview_time_label.setText("00:00 / 00:00")
        self._processing_preview_meta_label.setText("Select a downloaded video to preview it here.")
        self._processing_video_widget.setPixmap(QPixmap())
        self._processing_video_widget.setText("Preview unavailable")
        self._processing_preview_mode_combo.blockSignals(True)
        self._processing_preview_mode_combo.setCurrentIndex(0)
        self._processing_preview_mode_combo.blockSignals(False)
        self._processing_title_draft_input.setText("")
        self._processing_caption_draft_input.setPlainText("")
        self._processing_transcript_input.setPlainText("")
        self._processing_smart_summary_label.setText(
            "Smart draft summary will appear here after Groq generation."
        )
        self._set_processing_eval_state()
        self._refresh_processing_usage_label()
        self._set_processing_smart_options([], [])
        self._apply_title_style_preset("clean_hook")
        self._processing_style_status_label.setText(
            "These controls only affect the rendered title on the processed video. Captions stay as editable metadata for later upload."
        )
        self._processing_draft_status_label.setText(
            "Generate drafts from the selected downloaded video. Transcript context will be used automatically when available."
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
        self._processing_export_button.setEnabled(combo_enabled)
        self._processing_open_processed_button.setEnabled(True)
        self._processing_preview_back_button.setEnabled(
            combo_enabled and self._processing_preview_path is not None
        )
        self._processing_preview_forward_button.setEnabled(
            combo_enabled and self._processing_preview_path is not None
        )
        self._processing_generate_drafts_button.setEnabled(combo_enabled)
        self._processing_save_drafts_button.setEnabled(combo_enabled)
        self._processing_title_draft_input.setEnabled(combo_enabled)
        self._processing_caption_draft_input.setEnabled(combo_enabled)
        self._processing_transcript_input.setEnabled(True)
        for button in self._processing_smart_option_buttons:
            button.setEnabled(combo_enabled and button.isVisible())
        for title_input in self._processing_smart_option_title_inputs:
            title_input.setEnabled(combo_enabled)
        for caption_input in self._processing_smart_option_caption_inputs:
            caption_input.setEnabled(combo_enabled)
        self._processing_title_style_combo.setEnabled(combo_enabled)
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
        self._processing_generate_drafts_button.setText("Generate Drafts")

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

    def _refresh_processing_selection(self) -> None:
        item = self._processing_selected_item()
        if item is None or not self._item_exists(item):
            self._processing_probe = None
            self._processing_probe_item_id = None
            self._processing_auto_crop = CropSettings()
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
        self._processing_raw_transcript_text = item.transcript_text or ""
        self._processing_title_draft_input.setText(item.title_draft or "")
        self._processing_caption_draft_input.setPlainText(item.caption_draft or "")
        self._processing_transcript_input.setPlainText(
            self._processing_context_text(item, self._processing_raw_transcript_text)
        )
        self._load_processing_smart_drafts(item)
        self._load_processing_style_state(item)
        if item.transcript_text:
            self._processing_draft_status_label.setText(
                "Saved text drafts are loaded for this video."
            )
        else:
            self._processing_draft_status_label.setText(
                "Generate drafts from the selected downloaded video. Transcript context will be used automatically when available."
            )

        if self._processing_probe_item_id != item.id:
            try:
                self._processing_probe = probe_video(path)
                self._processing_probe_item_id = item.id
                self._processing_auto_crop = CropSettings()
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
                f"Processed output: {processed_output_path(path, processed_dir()).name}"
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

    def _load_processing_style_state(self, item: DownloadItem) -> None:
        title_preset = item.title_style_preset or "clean_hook"
        self._apply_title_style_preset(title_preset)

        if item.title_style_config:
            try:
                config = json.loads(item.title_style_config)
            except json.JSONDecodeError:
                config = {}
            self._processing_title_font_size.setValue(
                int(config.get("font_size", self._processing_title_font_size.value()))
            )
            font_index = self._processing_title_font_combo.findData(
                str(config.get("font_name", self._processing_title_font_combo.currentData()))
            )
            self._processing_title_font_combo.setCurrentIndex(font_index if font_index >= 0 else 0)
            self._processing_title_color_input.setText(
                str(config.get("text_color", self._processing_title_color_input.text()))
            )
            background_index = self._processing_title_background_combo.findData(
                str(config.get("background", self._processing_title_background_combo.currentData()))
            )
            self._processing_title_background_combo.setCurrentIndex(
                background_index if background_index >= 0 else 0
            )

    def _load_processing_smart_drafts(self, item: DownloadItem) -> None:
        self._processing_smart_summary_label.setText(
            item.smart_summary or "Smart draft summary will appear here after Groq generation."
        )
        title_options = self._parse_saved_options(item.smart_title_options)
        caption_options = self._parse_saved_options(item.smart_caption_options)
        self._set_processing_smart_options(title_options, caption_options)
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

    def _set_processing_smart_options(
        self, title_options: list[str], caption_options: list[str]
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

        for index, button in enumerate(self._processing_smart_option_buttons):
            title_input = self._processing_smart_option_title_inputs[index]
            caption_input = self._processing_smart_option_caption_inputs[index]
            button.blockSignals(True)
            button.setChecked(False)
            title_option, caption_option = self._processing_smart_option_pairs[index]
            has_content = bool(title_option or caption_option)
            if has_content:
                title_input.setText(title_option or "")
                caption_input.setPlainText(caption_option or "")
                button.setEnabled(not self._processing_in_progress)
                button.setText(f"Apply Option {index + 1}")
            else:
                title_input.setText("")
                caption_input.setPlainText("")
                button.setEnabled(False)
                button.setText(f"Option {index + 1} unavailable")
            button.blockSignals(False)

        if any(title or caption for title, caption in self._processing_smart_option_pairs):
            self._processing_smart_cards_status_label.setText(
                "Each card includes its own editable title and caption. Apply one option when it looks right."
            )
        else:
            self._processing_smart_cards_status_label.setText("")

    def _persist_processing_draft_state(self, item_id: int) -> None:
        with get_session() as session:
            item_row = session.get(DownloadItem, item_id)
            if item_row is None:
                return
            item_row.title_draft = self._processing_title_draft_input.text().strip() or None
            item_row.caption_draft = (
                self._processing_caption_draft_input.toPlainText().strip() or None
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
                    self._processing_smart_option_caption_inputs[index].toPlainText().strip()
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
            "font_size": self._processing_title_font_size.value(),
            "font_name": str(self._processing_title_font_combo.currentData() or "segoe_ui"),
            "text_color": self._processing_title_color_input.text().strip() or "#FFFFFF",
            "background": self._processing_title_background_combo.currentData(),
        }
        return json.dumps(payload, sort_keys=True)

    def _processing_crop_settings(self) -> CropSettings:
        return self._processing_auto_crop

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

        output_path = processed_output_path(Path(item.file_path), processed_dir())
        self._refresh_processing_latest_output_state(
            output_path if output_path.exists() else self._processing_last_output_path
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
            self._processing_latest_output_label.setText("No processed output yet in this session.")
            self._processing_open_latest_output_button.setEnabled(False)
            output_index = self._processing_preview_mode_combo.findData("output")
            if output_index >= 0:
                self._processing_preview_mode_combo.model().item(output_index).setEnabled(False)  # type: ignore[attr-defined]
                if self._processing_preview_mode == "output":
                    self._processing_preview_mode = "source"
                    self._processing_preview_mode_combo.blockSignals(True)
                    self._processing_preview_mode_combo.setCurrentIndex(0)
                    self._processing_preview_mode_combo.blockSignals(False)
            return

        self._processing_latest_output_label.setText(path.name)
        self._processing_open_latest_output_button.setEnabled(True)
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
        self._refresh_processing_selection()

    def _on_title_style_preset_changed(self) -> None:
        preset_key = self._processing_title_style_combo.currentData()
        if isinstance(preset_key, str):
            self._apply_title_style_preset(preset_key)
            self._processing_style_status_label.setText(
                "Applied the title style preset. You can still edit the fields below it."
            )

            self._processing_style_status_label.setText(
                "Applied the caption style preset. You can still edit the fields below it."
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
        if job.transcript_available:
            status_text = (
                "Using transcript context, metadata, and sampled video frames when supported "
                "to generate smarter title and caption options..."
            )
        else:
            status_text = (
                "No transcript was available. Generating smart drafts from metadata and sampled video frames when supported, "
                "but results can still be weaker than true speech-based context."
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
        self._stop_processing_loading_state()
        self._refresh_processing_output_preview()

    def _on_suggest_crop_completed(self, payload: dict) -> None:
        crop: CropSettings = payload["crop"]
        self._processing_auto_crop = crop

        self._finish_processing_job()
        reason_text = "; ".join(payload.get("reasons") or ["automatic crop updated"])
        if not payload.get("used_ocr", False):
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
                title_color=pending_job.title_color,
                title_background=pending_job.title_background,
            )
            self._start_processing_job(auto_job)
            return
        self._notify("Applied automatic crop suggestion.", Tone.SUCCESS)

    def _on_suggest_crop_failed(self, message: str) -> None:
        self._finish_processing_job()
        self._processing_pending_job = None
        self._processing_progress_label.setText("Automatic crop suggestion failed.")
        self._notify(f"Automatic crop suggestion failed: {message}", Tone.ERROR)

    def _on_generate_text_drafts_clicked(self) -> None:
        item = self._processing_selected_item()
        if item is None or not item.file_path:
            self._notify("Select a downloaded video first.", Tone.WARNING)
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
        self._start_smart_draft_job(
            SmartDraftJobConfig(
                transcript_text=transcript_text,
                source_title=item.title,
                niche_label=account.niche_label if account is not None else None,
                input_path=Path(item.file_path) if item.file_path else None,
                transcript_available=bool(transcript_text),
                account_voice=self._account_voice_config(account),
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
        self._set_processing_smart_options(title_options, caption_options)

        if title_options:
            self._processing_title_draft_input.setText(title_options[0])
        if caption_options:
            self._processing_caption_draft_input.setPlainText(caption_options[0])
        if self._processing_smart_option_buttons and (title_options or caption_options):
            self._processing_smart_option_buttons[0].setChecked(True)

        if used_fallback:
            self._processing_draft_status_label.setText(
                f"Generated drafts with {provider_label}. The result may be less grounded than the primary provider path."
            )
        else:
            self._processing_draft_status_label.setText(
                f"Generated smart draft options with {provider_label} and applied the first title/caption."
            )
        self._processing_progress_label.setText("Smart drafts generated.")
        item = self._processing_selected_item()
        if item is not None:
            self._persist_processing_draft_state(item.id)
        self._notify(
            f"Generated smart title and caption options with {provider_label}.", Tone.SUCCESS
        )

    def _on_smart_draft_failed(self, message: str) -> None:
        self._finish_processing_job()
        self._processing_draft_status_label.setText("Could not generate smart drafts.")
        self._processing_progress_label.setText("Smart draft generation failed.")
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
                "Generate Drafts: no speech found. Falling back to metadata-based generation..."
            )
            self._processing_draft_status_label.setText(
                f"{reason_text} Using source metadata to generate smart title and caption options instead. "
                "If the available context is weak, generation may still need manual editing to match the exact moment."
            )

        self._start_smart_draft_job(
            SmartDraftJobConfig(
                transcript_text=transcript_text.strip(),
                source_title=item.title,
                niche_label=account.niche_label if account is not None else None,
                input_path=Path(item.file_path) if item.file_path else None,
                transcript_available=transcript_available,
                account_voice=self._account_voice_config(account),
            )
        )
        return True

    def _on_processing_smart_option_clicked(self, option_index: int) -> None:
        if option_index >= len(self._processing_smart_option_pairs):
            return
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

        with get_session() as session:
            item_row = session.get(DownloadItem, item.id)
            if item_row is None:
                self._notify("Could not find the selected video.", Tone.ERROR)
                return
            item_row.title_draft = self._processing_title_draft_input.text().strip() or None
            item_row.caption_draft = (
                self._processing_caption_draft_input.toPlainText().strip() or None
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
            output_path = processed_output_path(Path(item.file_path), processed_dir())
            pending_job = ProcessJobConfig(
                input_path=Path(item.file_path),
                output_path=output_path,
                crop=CropSettings(),
                title_text=self._processing_title_draft_input.text().strip() or item.title or None,
                title_font_size=self._processing_title_font_size.value(),
                title_font_name=str(self._processing_title_font_combo.currentData() or "segoe_ui"),
                title_color=self._processing_title_color_input.text().strip() or "#FFFFFF",
                title_background=str(
                    self._processing_title_background_combo.currentData() or "none"
                ),
            )
            probe_video(Path(item.file_path))
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Could not start processing: {exc}", Tone.ERROR)
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
        self._processing_preview_timer.start()
        self._processing_toggle_preview_button.setText("Pause Video")

    def _on_processing_preview_seek(self, position: int) -> None:
        self._seek_processing_preview(position)

    def _shift_processing_preview(self, delta_ms: int) -> None:
        duration = self._processing_effective_duration_ms()
        if duration <= 0:
            return
        next_position = max(0, min(self._processing_preview_position_ms + delta_ms, duration))
        self._seek_processing_preview(next_position)

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
        self._processing_preview_timer.stop()
        if stream.time_base is not None:
            target_pts = int((position_ms / 1000) / float(stream.time_base))
            try:
                container.seek(max(target_pts, 0), stream=stream, any_frame=False, backward=True)
            except Exception:  # noqa: BLE001
                pass
        self._processing_preview_frame_iter = container.decode(video=0)
        self._processing_preview_position_ms = max(position_ms, 0)
        self._render_processing_frame_at_or_after(position_ms / 1000)
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
        self._processing_preview_position_ms = int(frame_time * 1000)
        self._processing_preview_position_slider.setValue(self._processing_preview_position_ms)
        self._processing_preview_time_label.setText(
            f"{self._format_media_time(self._processing_preview_position_ms)} / {self._format_media_time(self._processing_effective_duration_ms())}"
        )

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
        self._refresh_processing_output_preview()
        self._processing_progress_label.setText("Processing complete.")
        self._notify(f"Processed video saved to {output_path.name}.", Tone.SUCCESS)

    def _on_processing_failed(self, message: str) -> None:
        self._finish_processing_job()
        self._processing_progress_label.setText("Processing failed.")
        self._notify(f"Processing failed: {message}", Tone.ERROR)

    def _on_open_processed_folder_clicked(self) -> None:
        path = processed_dir()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))
            self._notify("Opened the processed videos folder.", Tone.INFO)
        except OSError as exc:
            self._notify(f"Could not open the processed folder: {exc}", Tone.ERROR)

    def _on_open_latest_processed_output_clicked(self) -> None:
        path = self._processing_last_output_path
        if path is None or not path.exists():
            self._notify("No processed output is available to open yet.", Tone.WARNING)
            return
        try:
            os.startfile(str(path))
            self._notify("Opening the latest processed output.", Tone.INFO)
        except OSError as exc:
            self._notify(f"Could not open the processed output: {exc}", Tone.ERROR)

    def _set_current_page(self, page_name: str) -> None:
        if page_name not in MODULE_PAGES:
            return
        self._current_page = page_name
        page_index = MODULE_PAGES.index(page_name)
        self._workspace_stack.setCurrentIndex(page_index)
        self._refresh_runtime_fields()
        self._sync_sidebar_selection()
        self._sync_account_panel_visibility()
        self._apply_refresh(force=True, preserve_status=True)

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
        if self._selected_source_id is None:
            return None
        return next(
            (source for source in self._displayed_sources if source.id == self._selected_source_id),
            None,
        )

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
                        source_type=infer_youtube_source_type(source_url),
                        label=source_url.rstrip("/").rsplit("/", 1)[-1] or source_url,
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
        scrape_controls_enabled = workspace_enabled and not self._scrape_in_progress
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
            self._source_toggle_button.setText("Disable Source")
        else:
            self._source_toggle_button.setText(
                "Disable Source" if selected_source.enabled else "Enable Source"
            )
        self._candidate_table.setEnabled(workspace_enabled)
        self._candidate_state_filter.setEnabled(workspace_enabled)
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
        if item.account_id not in {self._current_account_id, None}:
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

    def _schedule_status_text(self, item: DownloadItem) -> str:
        if not self._item_exists(item):
            return "Missing file"
        if item.title_draft and item.caption_draft:
            return "Ready for scheduler"
        if item.title_draft or item.caption_draft or item.smart_summary:
            return "Needs final edit"
        return "Needs preprocessing"

    def _refresh_schedule_page(self) -> None:
        if not hasattr(self, "_schedule_table"):
            return

        workspace_enabled = self._current_account_id is not None
        self._schedule_table.setEnabled(workspace_enabled)
        self._schedule_table.blockSignals(True)
        self._schedule_table.setRowCount(0)

        if not workspace_enabled:
            self._schedule_summary_label.setText(
                "Select an account workspace to review schedule drafts."
            )
            self._schedule_table.blockSignals(False)
            return

        schedule_items = [
            item
            for item in self._displayed_items
            if item.status == "downloaded" and item.review_state != "rejected"
        ]
        for item in schedule_items:
            row = self._schedule_table.rowCount()
            self._schedule_table.insertRow(row)
            values = [
                item.account.name if item.account else "Unassigned",
                item.title or "(untitled)",
                item.title_draft or "(not drafted)",
                item.caption_draft or "(not drafted)",
                self._schedule_status_text(item),
            ]
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                table_item.setData(Qt.ItemDataRole.UserRole, item.id)
                self._schedule_table.setItem(row, column, table_item)

        self._schedule_table.resizeRowsToContents()
        self._schedule_table.blockSignals(False)
        if schedule_items:
            ready_count = sum(
                1
                for item in schedule_items
                if self._schedule_status_text(item) == "Ready for scheduler"
            )
            self._schedule_summary_label.setText(
                f"{len(schedule_items)} draft videos for this account. {ready_count} are ready for scheduling."
            )
        else:
            self._schedule_summary_label.setText(
                "No downloaded drafts are ready for scheduling yet. Finish Preprocess first."
            )

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
            "Create Account" if creating_new else "Save Account Changes"
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
        self._account_form_panel.setVisible(show_form)
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
            hint="Create a fresh account record, then return to the main account tools.",
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
            title="Delete Account",
            hint="Choose one saved account to remove, then return to the main account tools.",
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

        self._detail_panel.setVisible(True)
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
        return "Queue Selected Candidate"

    def _matches_candidate_state_filter(self, candidate: ScrapeCandidate) -> bool:
        selected_state = self._candidate_state_filter.currentData()
        if selected_state in {None, "all"}:
            return True
        normalized_state = "candidate" if candidate.state == "new" else candidate.state
        return normalized_state == selected_state

    def _mark_user_interacting(self) -> None:
        if self._suppress_interaction_tracking:
            return
        self._interaction_idle_timer.start(900)

    def _on_interaction_idle(self) -> None:
        if self._pending_refresh:
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

    @staticmethod
    def _restore_combo_value(combo: QComboBox, value: int | None) -> None:
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
        self._current_account_combo.addItem("Choose current account...", None)
        self._sidebar_account_combo.clear()
        self._sidebar_account_combo.addItem("Choose account...", None)
        self._account_picker.clear()
        self._account_picker.addItem("Select account to edit...", None)
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
        self._sync_account_panel_visibility()
        self._clear_selection()
        self._clear_source_selection()
        self._clear_candidate_selection()
        self._apply_refresh(force=True)

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
            self._account_platform_combo.setCurrentText("youtube")
            self._account_niche_input.clear()
            self._account_login_input.clear()
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
            self._refresh_account_action_labels(None)
            return

        self._account_name_input.setText(account.name)
        self._account_platform_combo.setCurrentText(account.platform)
        self._account_niche_input.setText(account.niche_label or "")
        self._account_login_input.setText(account.login_identifier or "")
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
        try:
            scrape_max_items = (
                self._parse_optional_positive_int(
                    self._account_scrape_max_items_input.text(),
                    "Max intake items",
                )
                or 20
            )
            scrape_max_age_days = self._parse_optional_positive_int(
                self._account_scrape_max_age_days_input.text(),
                "Max age days",
            )
            auto_queue_limit = (
                self._parse_optional_positive_int(
                    self._account_auto_queue_limit_input.text(),
                    "Auto queue limit",
                )
                or 3
            )
            min_view_count = (
                self._parse_optional_nonnegative_int(
                    self._account_min_view_count_input.text(),
                    "Min views",
                )
                or 0
            )
            min_like_count = (
                self._parse_optional_nonnegative_int(
                    self._account_min_like_count_input.text(),
                    "Min likes",
                )
                or 0
            )
            ranking_weight_views = (
                self._parse_optional_positive_int(
                    self._account_weight_views_input.text(),
                    "Views weight",
                )
                or 35
            )
            ranking_weight_likes = (
                self._parse_optional_positive_int(
                    self._account_weight_likes_input.text(),
                    "Likes weight",
                )
                or 20
            )
            ranking_weight_recency = (
                self._parse_optional_positive_int(
                    self._account_weight_recency_input.text(),
                    "Recency weight",
                )
                or 25
            )
            ranking_weight_keyword_match = (
                self._parse_optional_positive_int(
                    self._account_weight_keyword_input.text(),
                    "Keyword match weight",
                )
                or 20
            )
        except ValueError as exc:
            self._notify(str(exc), Tone.WARNING)
            return

        raw_scrape_source_urls = self._parse_source_urls(self._account_scrape_sources_input.text())
        scrape_source_urls: list[str] = []
        normalized_source_count = 0
        for source in raw_scrape_source_urls:
            normalized_source, validation_error = normalize_youtube_source_url(source)
            if validation_error is not None or normalized_source is None:
                self._notify(
                    "Use only YouTube channel or profile URLs for source intake.", Tone.WARNING
                )
                return
            if normalized_source != source.strip():
                normalized_source_count += 1
            scrape_source_urls.append(normalized_source)
        discovery_keywords = self._parse_keyword_phrases(
            self._account_discovery_keywords_input.text()
        )

        selected = self._current_account() if self._account_mode == "edit" else None
        with get_session() as session:
            if selected is None:
                account = Account(name=name, platform="youtube")
                session.add(account)
            else:
                account = session.get(Account, selected.id)
                assert account is not None
                account.name = name

            account.platform = "youtube"
            account.niche_label = self._account_niche_input.text().strip() or None
            account.login_identifier = self._account_login_input.text().strip() or None
            account.credential_blob = self._account_credential_input.text().strip() or None
            account.scrape_source_urls = "\n".join(scrape_source_urls) or None
            account.scrape_max_items = scrape_max_items
            account.scrape_max_age_days = scrape_max_age_days
            account.discovery_keywords = "\n".join(discovery_keywords) or None
            account.discovery_mode = self._account_discovery_mode_combo.currentData()
            account.auto_queue_limit = auto_queue_limit
            account.min_view_count = min_view_count
            account.min_like_count = min_like_count
            account.ranking_weight_views = ranking_weight_views
            account.ranking_weight_likes = ranking_weight_likes
            account.ranking_weight_recency = ranking_weight_recency
            account.ranking_weight_keyword_match = ranking_weight_keyword_match
            account.writing_tone = self._account_writing_tone_input.text().strip() or None
            account.target_audience = self._account_target_audience_input.text().strip() or None
            account.hook_style = self._account_hook_style_input.text().strip() or None
            account.banned_phrases = self._account_banned_phrases_input.text().strip() or None
            account.title_style_notes = self._account_title_style_notes_input.text().strip() or None
            account.caption_style_notes = (
                self._account_caption_style_notes_input.text().strip() or None
            )
            session.commit()
            saved_account_id = account.id

        self._ensure_source_rows(
            account_id=saved_account_id,
            platform="youtube",
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
                "Saved account target and normalized source URLs to channel/profile roots.",
                Tone.SUCCESS,
            )
        else:
            self._notify_and_refresh("Saved account target.", Tone.SUCCESS)
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
        self._notify_and_refresh("Deleted account target.", Tone.SUCCESS)
        self._show_account_main()

    def _on_scroll_changed(self) -> None:
        self._mark_user_interacting()

    def _request_refresh(self) -> None:
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
            latest_failed = next(
                (item for item in filtered_items if item.status == "failed" and item.error_message),
                None,
            )
            active_account = self._active_account()
            if active_account is None:
                self._set_status(
                    "Create and select an account target to use the library.", Tone.WARNING
                )
            elif latest_failed is not None:
                self._set_status(f"Last failure: {latest_failed.error_message}", Tone.ERROR)
            elif filtered_items:
                self._set_status(
                    f"Showing {len(filtered_items)} items for {active_account.name}.",
                    Tone.INFO,
                )
            else:
                self._set_status(f"No clips yet for {active_account.name}.", Tone.INFO)

        workspace_enabled = self._current_account_id is not None
        self._url_input.setEnabled(workspace_enabled)
        self._download_button.setEnabled(workspace_enabled)
        self._search_input.setEnabled(workspace_enabled)
        self._status_filter.setEnabled(workspace_enabled)
        self._review_filter.setEnabled(workspace_enabled)
        self._table.setEnabled(workspace_enabled)
        self._table.setColumnHidden(2, workspace_enabled)
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
            review_background, review_foreground = self._review_colors(item.review_state)
            review_item.setBackground(review_background)
            review_item.setForeground(review_foreground)

            account_item = QTableWidgetItem(item.account.name if item.account else "Unassigned")
            account_item.setData(Qt.ItemDataRole.UserRole, item.id)
            title_item = QTableWidgetItem(item.title or "(untitled)")
            title_item.setData(Qt.ItemDataRole.UserRole, item.id)
            source_item = QTableWidgetItem(item.source_url)
            source_item.setData(Qt.ItemDataRole.UserRole, item.id)
            file_item = QTableWidgetItem(self._output_text(item))
            file_item.setData(Qt.ItemDataRole.UserRole, item.id)

            self._table.setItem(row, 0, status_item)
            self._table.setItem(row, 1, review_item)
            self._table.setItem(row, 2, account_item)
            self._table.setItem(row, 3, title_item)
            self._table.setItem(row, 4, source_item)
            self._table.setItem(row, 5, file_item)

            if self._selected_item_id == item.id:
                self._table.selectRow(row)

        self._table.resizeRowsToContents()
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
                    f"Last status: {status_text}."
                )
            )
            return

        enabled_count = sum(1 for source in sources if source.enabled)
        disabled_count = len(sources) - enabled_count
        self._source_summary_label.setText(
            (
                f"Showing {len(sources)} source(s): "
                f"{enabled_count} enabled, {disabled_count} disabled. "
                f"Select a source to inspect or scrape it directly."
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

            enabled_combo = QComboBox()
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
        self._refresh_source_summary()
        self._refresh_candidate_action_state()

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
    def _published_text(candidate: ScrapeCandidate) -> str:
        if candidate.published_at is None:
            return "(unknown)"
        published_at = candidate.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=dt.timezone.utc)
        return published_at.astimezone().strftime("%Y-%m-%d")

    @staticmethod
    def _candidate_number_text(value: int | None) -> str:
        if value is None:
            return "-"
        return f"{value:,}"

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

        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == self._current_account_id)
                .all()
            )

        self._sync_candidate_download_states(candidates)
        with get_session() as session:
            candidates = (
                session.query(ScrapeCandidate)
                .filter(ScrapeCandidate.account_id == self._current_account_id)
                .all()
            )

        filtered_candidates = [
            candidate for candidate in candidates if self._matches_candidate_state_filter(candidate)
        ]

        return sorted(
            filtered_candidates,
            key=lambda item: (
                item.ranking_score or 0,
                item.view_count or 0,
                item.like_count or 0,
                item.published_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            ),
            reverse=True,
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
            views_item = QTableWidgetItem(self._candidate_number_text(candidate.view_count))
            views_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            likes_item = QTableWidgetItem(self._candidate_number_text(candidate.like_count))
            likes_item.setData(Qt.ItemDataRole.UserRole, candidate.id)
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
            self._candidate_table.setItem(row, 2, views_item)
            self._candidate_table.setItem(row, 3, likes_item)
            self._candidate_table.setItem(row, 4, published_item)
            self._candidate_table.setItem(row, 5, channel_item)
            self._candidate_table.setItem(row, 6, title_item)
            self._candidate_table.setItem(row, 7, match_item)

            if self._selected_candidate_id == candidate.id:
                self._candidate_table.selectRow(row)

        self._candidate_table.resizeRowsToContents()
        self._candidate_table.blockSignals(False)
        self._refresh_candidate_action_state()

    def _on_candidate_selection_changed(self) -> None:
        selected_items = self._candidate_table.selectedItems()
        if not selected_items:
            self._refresh_candidate_action_state()
            return

        self._selected_candidate_id = selected_items[0].data(Qt.ItemDataRole.UserRole)
        self._refresh_candidate_action_state()

    def _on_source_selection_changed(self) -> None:
        selected_items = self._source_table.selectedItems()
        if not selected_items:
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
            self._notify("Create and select an account target first.", Tone.WARNING)
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

    def _start_scrape_job(self, job: ScrapeJobConfig) -> None:
        if self._scrape_in_progress:
            self._notify("A scrape is already running.", Tone.WARNING)
            return

        self._scrape_in_progress = True
        self._prepare_scrape_progress(total_sources=len(job.source_ids))
        self._scrape_progress_label.setText("Preparing scrape job...")
        self._scrape_progress_bar.setVisible(True)
        self._scrape_progress_bar.setFormat("Preparing scrape job...")
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
        self._scrape_progress_bar.setVisible(True)
        self._scrape_progress_bar.setMinimum(0)
        self._scrape_progress_bar.setMaximum(max(total_sources, 1))
        self._scrape_progress_bar.setValue(0)

    def _on_scrape_progress(self, payload: dict) -> None:
        self._scrape_progress_label.setText(
            f"Scraping {payload['current']}/{payload['total']}: {payload['source_label']}"
        )
        self._scrape_progress_bar.setMaximum(max(payload["total"], 1))
        self._scrape_progress_bar.setValue(max(payload["current"] - 1, 0))
        self._scrape_progress_bar.setFormat(
            f"{max(payload['current'] - 1, 0)}/{payload['total']} sources complete"
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
        completed_sources = min(
            self._scrape_progress_bar.value() + 1,
            self._scrape_progress_bar.maximum(),
        )
        self._scrape_progress_bar.setValue(completed_sources)
        self._scrape_progress_bar.setFormat(
            f"{completed_sources}/{self._scrape_progress_bar.maximum()} sources complete"
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
    ) -> tuple[int, int, int, int]:
        with get_session() as session:
            run = ScrapeRun(account_id=account_id, source_id=source.id, status="running")
            session.add(run)
            session.commit()
            run_id = run.id

        try:
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
                    source_row.last_scraped_at = dt.datetime.now(dt.timezone.utc)
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
            self._notify("Create and select an account target first.", Tone.WARNING)
            return

        source_url = self._scrape_source_input.text().strip()
        if not source_url:
            self._notify("Paste a YouTube channel or profile URL first.", Tone.WARNING)
            return

        normalized_source_url, validation_error = normalize_youtube_source_url(source_url)
        if validation_error is not None:
            self._notify(validation_error, Tone.WARNING)
            return
        assert normalized_source_url is not None

        existing_urls = {source.source_url for source in self._load_sources_for_account(account.id)}
        if normalized_source_url in existing_urls:
            self._notify("This source is already configured for the current account.", Tone.WARNING)
            return

        with get_session() as session:
            session.add(
                Source(
                    account_id=account.id,
                    platform=account.platform,
                    source_type=infer_youtube_source_type(normalized_source_url),
                    label=normalized_source_url.rstrip("/").rsplit("/", 1)[-1]
                    or normalized_source_url,
                    source_url=normalized_source_url,
                    enabled=1,
                    priority=100,
                )
            )
            session.commit()

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
                (candidate.video_id or candidate.source_url): candidate
                for candidate in all_candidates
                if candidate.account_id == account_id
            }
            all_downloads = session.query(DownloadItem).all()
            download_keys_same_account = {
                self._youtube_video_key(item.source_url) or item.source_url
                for item in all_downloads
                if item.account_id == account_id
            }

            for candidate in candidates:
                candidate_key = (
                    f"youtube:{candidate.video_id}" if candidate.video_id else candidate.source_url
                )
                existing_candidate = candidate_by_key.get(
                    candidate.video_id or candidate.source_url
                )
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
                candidate_by_key[candidate.video_id or candidate.source_url] = candidate_row
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
            item_id = QueueManager.enqueue_download(url=candidate.source_url, account_id=account_id)
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

    def _find_duplicate_for_account(self, url: str, account_id: int) -> DownloadItem | None:
        requested_key = self._youtube_video_key(url)
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
            (item for item in items if self._youtube_video_key(item.source_url) == requested_key),
            None,
        )

    def _on_download_clicked(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            self._notify("Paste a URL first.", Tone.WARNING)
            return
        if self._current_account_id is None:
            self._notify("Create and select an account target first.", Tone.WARNING)
            return

        validation_error = self._validate_youtube_url(url)
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
                message = "This video already failed for this account. Use Retry from history."
            else:
                message = "This video already exists in this account library."
            self._notify_and_refresh(message, Tone.WARNING)
            return

        self._download_button.setEnabled(False)
        try:
            item_id = QueueManager.enqueue_download(url=url, account_id=self._current_account_id)
            self._selected_item_id = item_id
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
