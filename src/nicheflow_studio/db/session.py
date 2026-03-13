from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nicheflow_studio.core.paths import data_dir
from nicheflow_studio.db.models import Base


def _db_path() -> Path:
    return data_dir() / "nicheflow.db"


_ENGINE = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def init_db() -> None:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        return

    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _ENGINE = create_engine(f"sqlite:///{db_path}", future=True)
    _SESSION_FACTORY = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(_ENGINE)


@contextmanager
def get_session() -> Session:
    if _SESSION_FACTORY is None:
        init_db()

    assert _SESSION_FACTORY is not None
    session = _SESSION_FACTORY()
    try:
        yield session
    finally:
        session.close()

