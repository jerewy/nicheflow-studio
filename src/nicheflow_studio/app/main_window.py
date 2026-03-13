from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from nicheflow_studio.core.paths import downloads_dir
from nicheflow_studio.db.models import DownloadItem
from nicheflow_studio.db.session import get_session
from nicheflow_studio.downloader.youtube import download_youtube_url


@dataclass(frozen=True)
class UiStrings:
    title: str = "NicheFlow Studio (MVP)"
    url_placeholder: str = "Paste a YouTube / Shorts URL…"
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

        self._list = QListWidget()

        root = QVBoxLayout()
        root.addLayout(top_row)
        root.addWidget(self._status_label)
        root.addWidget(self._list, stretch=1)

        self.setLayout(root)
        self.resize(900, 520)
        self._refresh_list()

    def _refresh_list(self) -> None:
        self._list.clear()
        with get_session() as session:
            items = session.query(DownloadItem).order_by(DownloadItem.created_at.desc()).all()
        for item in items:
            label = f"[{item.status}] {item.source_url} → {item.file_path or '(pending)'}"
            list_item = QListWidgetItem(label)
            list_item.setData(Qt.ItemDataRole.UserRole, item.id)
            self._list.addItem(list_item)

    def _on_download_clicked(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            self._status_label.setText("Paste a URL first.")
            return

        self._download_button.setEnabled(False)
        try:
            result = download_youtube_url(url=url, output_dir=downloads_dir())
            with get_session() as session:
                session.add(
                    DownloadItem(
                        source_url=url,
                        extractor=result.extractor,
                        video_id=result.video_id,
                        title=result.title,
                        file_path=str(result.file_path),
                        status="downloaded",
                    )
                )
                session.commit()

            self._status_label.setText(f"Downloaded: {result.title or result.video_id or 'OK'}")
            self._url_input.clear()
            self._refresh_list()
        except Exception as exc:  # noqa: BLE001
            self._status_label.setText(f"Download failed: {exc}")
        finally:
            self._download_button.setEnabled(True)

