"""Evaluate on a copy: the evaluators never open your database for writing."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def open_copy(src: Path, tmp: Path):
    """Copy ``src`` with SQLite's online backup (source opened read-only), migrate the copy, return a session on it."""
    from app.config import get_settings

    copy = tmp / "eval.db"
    with sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True) as a, sqlite3.connect(copy) as b:
        a.backup(b)
    os.environ["WS_DATABASE_URL"] = f"sqlite:///{copy}"
    get_settings.cache_clear()
    from app.db import SessionLocal, init_engine, migrate

    init_engine(f"sqlite:///{copy}")
    migrate()  # the copy only
    return SessionLocal()


def source_database(db_arg: str | None) -> Path:
    from app.config import get_settings

    return Path(db_arg) if db_arg else Path(get_settings().database_url.removeprefix("sqlite:///"))
