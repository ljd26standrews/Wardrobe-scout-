"""Database engine and session management.

SQLite is used by default; set `WS_DATABASE_URL=postgresql+psycopg://...` to
move to PostgreSQL (all models use portable SQLAlchemy types).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; much cheaper commits
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    _engine = _make_engine(url or get_settings().database_url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def SessionLocal() -> Session:  # noqa: N802 - mirrors sessionmaker naming
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_all() -> None:
    from . import models  # noqa: F401 - register models

    Base.metadata.create_all(get_engine())


INITIAL_REVISION = "53601a417a9c"


def migrate() -> None:
    """Bring the schema to the latest Alembic revision.

    A database created earlier with `create_all` (no alembic_version table) has the initial schema: it is
    stamped at the initial revision and then upgraded, so later migrations still add their columns.
    """
    from pathlib import Path

    from alembic.config import Config
    from sqlalchemy import inspect

    from alembic import command

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    insp = inspect(get_engine())
    tables = set(insp.get_table_names())
    if "products" in tables and "alembic_version" not in tables:
        # Created by create_all: at head if it already has every current column, else at the initial schema.
        from . import models  # noqa: F401 - register models

        missing = [
            (t.name, c.name)
            for t in Base.metadata.sorted_tables
            if t.name in tables
            for c in t.columns
            if c.name not in {col["name"] for col in insp.get_columns(t.name)}
        ]
        create_all()  # add any missing tables
        command.stamp(cfg, INITIAL_REVISION if missing else "head")
    command.upgrade(cfg, "head")
