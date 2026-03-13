from __future__ import annotations

from dataclasses import dataclass

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
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["Status", "Title", "Source URL", "Output"])
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
