"""pywebview launcher for the migrated React Processing screen.

Run alongside the existing PyQt app during the migration:

    # against the Vite dev server (hot reload)
    $env:NICHEFLOW_WEBVIEW_URL = "http://localhost:5173"
    .venv\\Scripts\\python.exe -m nicheflow_studio.app.webview_app

    # against the production build (frontend/dist)
    cd frontend; npm run build; cd ..
    .venv\\Scripts\\python.exe -m nicheflow_studio.app.webview_app

The window hosts the React UI and exposes :class:`ProcessingBridge` as
``window.pywebview.api``. This module is intentionally thin and imports
``webview`` lazily so importing the package (e.g. in tests) never requires a GUI
runtime.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from nicheflow_studio.app.processing_bridge import ProcessingBridge
from nicheflow_studio.core.env import load_dotenv
from nicheflow_studio.core.logging import configure_logging
from nicheflow_studio.core.paths import ensure_data_dirs
from nicheflow_studio.db.session import init_db

logger = logging.getLogger(__name__)

# Vite dev server default. Set NICHEFLOW_WEBVIEW_URL to override (or to point at a
# different port); when unset we load the static production build instead.
_DEFAULT_DEV_URL = "http://localhost:5173"


def _frontend_dist_index() -> Path:
    # src/nicheflow_studio/app/webview_app.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3] / "frontend" / "dist" / "index.html"


def resolve_entry() -> str:
    """Return the URL or file path the webview window should load.

    Order: an explicit ``NICHEFLOW_WEBVIEW_URL`` (dev server), else the built
    ``frontend/dist/index.html``. Raises ``FileNotFoundError`` when neither is
    available so the failure is obvious instead of a blank window.
    """
    url = os.environ.get("NICHEFLOW_WEBVIEW_URL")
    if url:
        return url
    index = _frontend_dist_index()
    if not index.exists():
        raise FileNotFoundError(
            f"No built frontend at {index}. Run `npm run build` in frontend/, "
            f"or set NICHEFLOW_WEBVIEW_URL to the Vite dev server."
        )
    return str(index)


def run_webview() -> None:
    load_dotenv()
    ensure_data_dirs()
    configure_logging()
    init_db()

    import webview  # lazy: keep GUI runtime out of plain imports/tests

    entry = resolve_entry()
    bridge = ProcessingBridge()
    logger.info("Opening Processing webview at %s", entry)
    webview.create_window(
        "NicheFlow Studio — Processing",
        url=entry,
        js_api=bridge,
        width=1280,
        height=860,
        min_size=(960, 640),
    )
    webview.start()


if __name__ == "__main__":
    run_webview()
