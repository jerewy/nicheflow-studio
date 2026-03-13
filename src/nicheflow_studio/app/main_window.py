from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from nicheflow_studio.db.models import DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.queue import QueueManager


@dataclass(frozen=True)
class UiStrings:
    title: str = "NicheFlow Studio (MVP)"
    url_placeholder: str = "Paste a YouTube / Shorts URL..."
    add_button: str = "Download"


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._ui = UiStrings()
        self.setWindowTitle(self._ui.title)

        self._status_label = QLabel("Ready.")
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText(self._ui.url_placeholder)

        self._download_button = QPushButton(self._ui.add_button)
        self._download_button.clicked.connect(self._on_download_clicked)

        top_row = QHBoxLayout()
        top_row.addWidget(self._url_input, stretch=1)
        top_row.addWidget(self._download_button)

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(
            ["Status", "Title", "Source URL", "Output", "Actions"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        root = QVBoxLayout()
        root.addLayout(top_row)
        root.addWidget(self._status_label)
        root.addWidget(self._table, stretch=1)

        self.setLayout(root)
        self.resize(900, 520)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(3000)
        self._refresh_timer.timeout.connect(self._refresh_list)
        self._refresh_timer.start()

        self._refresh_list()

    def _action_widget(self, item: DownloadItem) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        retry_button = QPushButton("Retry")
        retry_button.setEnabled(item.status in {"failed", "downloaded"})
        retry_button.clicked.connect(lambda: self._on_retry_clicked(item.id))

        open_button = QPushButton("Open")
        open_button.setEnabled(bool(item.file_path))
        open_button.clicked.connect(lambda: self._on_open_clicked(item.file_path))

        layout.addWidget(retry_button)
        layout.addWidget(open_button)
        widget.setLayout(layout)
        return widget

    def _refresh_list(self) -> None:
        self._table.setRowCount(0)
        with get_session() as session:
            items = (
                session.query(DownloadItem)
                .order_by(DownloadItem.created_at.desc())
                .limit(50)
                .all()
            )
        for item in items:
            row = self._table.rowCount()
            self._table.insertRow(row)

            status_item = QTableWidgetItem(item.status)
            status_item.setFlags(Qt.ItemFlag.ItemIsSelectable)
            title_item = QTableWidgetItem(item.title or "(untitled)")
            source_item = QTableWidgetItem(item.source_url)
            file_item = QTableWidgetItem(item.file_path or "(pending)")

            self._table.setItem(row, 0, status_item)
            self._table.setItem(row, 1, title_item)
            self._table.setItem(row, 2, source_item)
            self._table.setItem(row, 3, file_item)
            self._table.setCellWidget(row, 4, self._action_widget(item))

    def _on_download_clicked(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            self._status_label.setText("Paste a URL first.")
            return

        self._download_button.setEnabled(False)
        try:
            QueueManager.enqueue_download(url=url)
            self._status_label.setText("Queued download...")
            self._url_input.clear()
            self._refresh_list()
        except Exception as exc:  # noqa: BLE001
            self._status_label.setText(f"Queue failed: {exc}")
        finally:
            self._download_button.setEnabled(True)

    def _on_retry_clicked(self, item_id: int) -> None:
        if QueueManager.retry_item(item_id):
            self._status_label.setText("Retrying download…")
            self._refresh_list()
        else:
            self._status_label.setText("Could not retry download.")

    def _on_open_clicked(self, target: str | None) -> None:
        if not target:
            self._status_label.setText("No file to open yet.")
            return
        path = Path(target)
        if not path.exists():
            self._status_label.setText("File missing.")
            return
        os.startfile(str(path))
