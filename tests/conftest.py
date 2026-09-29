from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Stand-in footage for tests that never decode the file. Padded past
# media_library.MIN_PLAUSIBLE_VIDEO_BYTES because the download chokepoint now
# refuses anything small enough to be Instagram's black placeholder, and a
# 5-byte fixture is indistinguishable from one.
FAKE_VIDEO_BYTES = b"video" * 12_000


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NICHEFLOW_DATA_DIR", raising=False)

    from nicheflow_studio.db.session import reset_db_state

    reset_db_state()
    # Each test gets a fresh DB, so item ids repeat across tests; a batch-prompt
    # "#N" snapshot left by an earlier test would route a later test's import.
    draft_handoff = sys.modules.get("nicheflow_studio.services.draft_handoff")
    if draft_handoff is not None:
        monkeypatch.setattr(draft_handoff, "_PROMPT_SEQ_TO_ITEM", {})
    yield
    reset_db_state()


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app
