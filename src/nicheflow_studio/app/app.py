import logging
import sys

from PyQt6.QtWidgets import QApplication

from nicheflow_studio.core.env import load_dotenv
from nicheflow_studio.app.main_window import MainWindow
from nicheflow_studio.core.logging import configure_logging
from nicheflow_studio.core.paths import ensure_data_dirs
from nicheflow_studio.db.session import init_db
from nicheflow_studio.processing.thumbnail import face_detection_available


def run_app() -> None:
    load_dotenv()
    ensure_data_dirs()
    configure_logging()
    init_db()

    # Surface whether face-aware cover picking is active. If this logs
    # "unavailable" in a packaged build, the OpenCV cascade wasn't bundled
    # (see NicheFlowStudio.spec) and covers fall back to sharpness/exposure.
    available = face_detection_available()
    logging.getLogger(__name__).info(
        "Reel cover face detection: %s", "available" if available else "unavailable"
    )

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())
