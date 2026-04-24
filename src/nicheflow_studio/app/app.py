import sys

from PyQt6.QtWidgets import QApplication

from nicheflow_studio.core.env import load_dotenv
from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.core.logging import configure_logging
from nicheflow_studio.core.paths import ensure_data_dirs
from nicheflow_studio.db.session import init_db


def run_app() -> None:
    load_dotenv()
    ensure_data_dirs()
    configure_logging()
    init_db()

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())
