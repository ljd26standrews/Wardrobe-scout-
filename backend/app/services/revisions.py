"""Data revisions: a counter per kind of data, bumped by database triggers on every insert, update and delete.

Caches of derived results (feature space, quality assessments) are keyed by the revisions they depend on, so they
are invalidated by the data itself: a decision, an Undo, an evidence edit, a match decision, an import, a product
update or a settings change, made by this process or another (a script, a restore), cannot leave a stale result
behind, and no write path has to remember to clear a cache.

Products count only content changes: marking a piece as seen (``is_new``) or a scan noting when it last saw a
listing does not change what any derived result depends on.

Every key also carries the database's instance number (random, set when the counters are created and renewed by a
restore), so two databases, or a database and an older copy of it, never share cached results.
"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

# The product columns derived results depend on (features, quality, eligibility, cards' substance).
PRODUCT_CONTENT = ("seller_id", "title_original", "title_en", "description_en", "brand", "category", "colours",
                   "materials", "material_text", "gsm", "sizes", "seasons", "fit_tags", "logo_level", "price",
                   "currency", "status", "data_origin", "completeness", "analysis_state", "group_id",
                   "image_embedding", "text_embedding", "embedding_backend")  # fmt: skip

# revision name -> (table, columns whose update counts; None: any)
TRACKED: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "products": ("products", PRODUCT_CONTENT),
    "photos": ("products", ("image_embedding", "embedding_backend")),  # photo vectors written (re-analysis)
    "addresses": ("products", ("source_url",)),  # a listing's address (identity across re-imports)
    "evidence": ("evidence", None),
    "relations": ("product_relations", None),
    "feedback": ("feedback", None),
    "wardrobe": ("wardrobe_items", None),
    "inspiration": ("inspiration_images", None),
    "sellers": ("sellers", None),
    "settings": ("app_settings", None),
}


def ddl(dialect: str) -> list[str]:
    """Statements creating the counters and their triggers (idempotent)."""
    out = ["CREATE TABLE IF NOT EXISTS data_revisions (name VARCHAR(40) PRIMARY KEY, n BIGINT NOT NULL)"]
    if dialect == "postgresql":
        out += [f"INSERT INTO data_revisions (name, n) VALUES ('{name}', 0) ON CONFLICT DO NOTHING" for name in TRACKED]
        out.append("INSERT INTO data_revisions (name, n) VALUES ('instance', floor(random() * 9e15)::bigint) "
                   "ON CONFLICT DO NOTHING")  # fmt: skip
        out.append(
            "CREATE OR REPLACE FUNCTION ws_bump_revision() RETURNS trigger AS $$ BEGIN "
            "UPDATE data_revisions SET n = n + 1 WHERE name = TG_ARGV[0]; RETURN NULL; END $$ LANGUAGE plpgsql"
        )
        for name, (table, cols) in TRACKED.items():
            update = f"UPDATE OF {', '.join(cols)}" if cols else "UPDATE"
            out.append(f"DROP TRIGGER IF EXISTS ws_rev_{name} ON {table}")
            out.append(f"CREATE TRIGGER ws_rev_{name} AFTER INSERT OR DELETE OR {update} ON {table} "
                       f"FOR EACH STATEMENT EXECUTE FUNCTION ws_bump_revision('{name}')")  # fmt: skip
        return out
    out += [f"INSERT OR IGNORE INTO data_revisions (name, n) VALUES ('{name}', 0)" for name in TRACKED]
    out.append("INSERT OR IGNORE INTO data_revisions (name, n) VALUES ('instance', abs(random()))")
    for name, (table, cols) in TRACKED.items():
        bump = f"BEGIN UPDATE data_revisions SET n = n + 1 WHERE name = '{name}'; END"
        out.append(f"CREATE TRIGGER IF NOT EXISTS ws_rev_{name}_ins AFTER INSERT ON {table} {bump}")
        out.append(f"CREATE TRIGGER IF NOT EXISTS ws_rev_{name}_del AFTER DELETE ON {table} {bump}")
        of = f" OF {', '.join(cols)}" if cols else ""
        out.append(f"CREATE TRIGGER IF NOT EXISTS ws_rev_{name}_upd AFTER UPDATE{of} ON {table} {bump}")
    return out


def drop_ddl(dialect: str) -> list[str]:
    if dialect == "postgresql":
        out = [f"DROP TRIGGER IF EXISTS ws_rev_{name} ON {table}" for name, (table, _) in TRACKED.items()]
        return [*out, "DROP FUNCTION IF EXISTS ws_bump_revision()", "DROP TABLE IF EXISTS data_revisions"]
    out = [f"DROP TRIGGER IF EXISTS ws_rev_{name}_{op}" for name in TRACKED for op in ("ins", "del", "upd")]
    return [*out, "DROP TABLE IF EXISTS data_revisions"]


def install(bind: Engine | Connection) -> None:
    """Create the counters and triggers if missing. Safe to repeat: run after every migration, because a table
    rebuilt by a migration loses its triggers."""
    statements = ddl(bind.dialect.name)
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            for s in statements:
                conn.execute(text(s))
    else:
        for s in statements:
            bind.execute(text(s))


def renew_instance(bind: Engine | Connection) -> None:
    """Give the database a new instance number (after replacing it with a copy, e.g. a restore)."""
    stmt = text("UPDATE data_revisions SET n = :n WHERE name = 'instance'")
    n = int.from_bytes(os.urandom(7), "big")
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            conn.execute(stmt, {"n": n})
    else:
        bind.execute(stmt, {"n": n})


def current(db: Session, *names: str) -> tuple[int, ...]:
    """A cache key part: the database's instance number, then the revisions of ``names`` (in that order)."""
    rows = dict(db.execute(text("SELECT name, n FROM data_revisions")).all())
    return (int(rows.get("instance", -1)), *(int(rows.get(n, -1)) for n in names))
