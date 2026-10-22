"""Evaluate on a copy: the evaluators never open your database for writing."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

_previous: list[tuple] = []  # the databases the process used before each open_copy, restored by close_copy


def open_copy(src: Path, tmp: Path):
    """Copy ``src`` with SQLite's online backup (source opened read-only), migrate the copy, return a session on it.
    The process uses the copy until ``close_copy``."""
    from app import db as appdb
    from app.config import get_settings

    _previous.append((appdb._engine, appdb._SessionLocal, os.environ.get("WS_DATABASE_URL")))
    copy = tmp / "eval.db"
    with closing(sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)) as a, closing(sqlite3.connect(copy)) as b:
        a.backup(b)  # closed, not just committed: the source must not stay open (Windows locks open files)
    os.environ["WS_DATABASE_URL"] = f"sqlite:///{copy}"
    get_settings.cache_clear()
    from app.db import SessionLocal, init_engine, migrate

    init_engine(f"sqlite:///{copy}")
    migrate()  # the copy only
    return SessionLocal()


def close_copy(db) -> None:
    """Close a session from ``open_copy``, release the copy and go back to the database used before (so an evaluator
    called from another program leaves it as it was)."""
    from app import db as appdb
    from app.config import get_settings

    db.close()
    if appdb._engine is not None:
        appdb._engine.dispose()
    if _previous:
        engine, maker, url = _previous.pop()
        appdb._engine, appdb._SessionLocal = engine, maker
        if url is None:
            os.environ.pop("WS_DATABASE_URL", None)
        else:
            os.environ["WS_DATABASE_URL"] = url
        get_settings.cache_clear()


def source_database(db_arg: str | None) -> Path:
    from app.config import get_settings

    return Path(db_arg) if db_arg else Path(get_settings().database_url.removeprefix("sqlite:///"))
