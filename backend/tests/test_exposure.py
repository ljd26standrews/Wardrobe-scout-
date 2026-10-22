"""Exposure records (services/exposure.py, POST /api/exposures): versioned, recorded once per event however often
it is delivered, only for pieces the named feed served; earlier "rendered" records survive the upgrade as legacy and
are never a denominator. Disposable database."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


@pytest.fixture()
def client(env, monkeypatch):
    from app.main import create_app
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: 0)
    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def _feed(c, db) -> dict:
    from .test_calibration_lifecycle import catalogue

    catalogue(db)  # undecided listings with photos
    r = c.get("/api/feed", params={"limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) >= 4, "the catalogue serves a feed"
    return body


def _event(feed: dict, i: int, event_id: str, surface="grid", dwell=1200) -> dict:
    return {"event_id": event_id, "feed_id": feed["feed_id"], "product_id": feed["items"][i]["id"], "surface": surface,
            "dwell_ms": dwell}  # fmt: skip


def test_a_retried_delivery_is_recorded_once(client, db):
    from app.services import exposure

    feed = _feed(client, db)
    batch = {"definition": exposure.DEFINITION, "events": [_event(feed, 0, "evt-aaaa-0001"), _event(feed, 1, "evt-aaaa-0002")]}
    first = client.post("/api/exposures", json=batch).json()
    assert first["recorded"] == 2 and first["duplicates"] == 0
    again = client.post("/api/exposures", json=batch).json()  # the response was lost: the browser sends it again
    assert again["recorded"] == 0 and again["duplicates"] == 2
    more = {"definition": exposure.DEFINITION, "events": [_event(feed, 1, "evt-aaaa-0002"), _event(feed, 2, "evt-aaaa-0003")]}
    assert client.post("/api/exposures", json=more).json() == {"recorded": 1, "duplicates": 1, "rejected": 0,
                                                                 "definition": exposure.DEFINITION}  # fmt: skip
    summary = client.get("/api/exposures/summary").json()
    assert summary["events"] == 3 and sorted(summary["by_product"].values()) == [1, 1, 1]


def test_only_pieces_the_feed_served_and_the_current_definition_are_accepted(client, db):
    from app.services import exposure

    feed = _feed(client, db)
    served = {i["id"] for i in feed["items"]}
    stranger = next(p["id"] for p in client.get("/api/products", params={"page_size": 200}).json()["items"]
                    if p["id"] not in served)  # fmt: skip
    bad = {**_event(feed, 0, "evt-bbbb-0001"), "product_id": stranger}
    unknown_feed = {**_event(feed, 0, "evt-bbbb-0002"), "feed_id": "nosuchfeed"}
    r = client.post("/api/exposures", json={"definition": exposure.DEFINITION, "events": [bad, unknown_feed]})
    assert r.json()["rejected"] == 2 and r.json()["recorded"] == 0
    old = client.post("/api/exposures", json={"definition": "rendered-v1", "events": [_event(feed, 0, "evt-bbbb-0003")]})
    assert old.status_code == 422
    pos = client.post("/api/exposures", json={"definition": exposure.DEFINITION,
                                              "events": [{**_event(feed, 3, "evt-bbbb-0004"), "position": 0}]})  # fmt: skip
    assert pos.json()["recorded"] == 1
    from app.db import SessionLocal
    from app.models import FeedRequest, Impression

    with SessionLocal() as s:  # the position is the one the feed served it at, whatever the browser says
        served_at = {it["product_id"]: it["position"] for it in s.get(FeedRequest, feed["feed_id"]).items}
        assert s.query(Impression).filter_by(event_id="evt-bbbb-0004").one().position == served_at[feed["items"][3]["id"]]


def test_upgrading_keeps_earlier_records_as_legacy(tmp_path, monkeypatch):
    """A database at the previous revision, with impressions recorded the old way, is migrated in place: the rows
    survive with no definition, and the exposure rates call them legacy instead of using them."""
    import sqlalchemy as sa
    from alembic.config import Config

    from alembic import command

    db_url = f"sqlite:///{tmp_path / 'old.db'}"
    monkeypatch.setenv("WS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WS_DATABASE_URL", db_url)
    from app.config import get_settings

    get_settings.cache_clear()
    from app import db as dbmod

    dbmod.init_engine(db_url)
    ini = Path(dbmod.__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    command.upgrade(cfg, "a4c7e2f9b1d3")
    eng = dbmod.get_engine()
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO feed_requests (id, created_at, engine, engine_version, decisions, candidates, "
                             "exploration_rate, filters, items, policy) VALUES ('f1', '2026-10-01', 'bayes', 'x', 0, 0, "
                             "0, '{}', '[]', 'ranked')"))  # fmt: skip
        for i in range(3):
            conn.execute(sa.text("INSERT INTO impressions (feed_id, product_id, position, surface, explore, shown_at) "
                                 f"VALUES ('f1', NULL, {i}, 'grid', 0, '2026-10-01')"))  # fmt: skip
    dbmod.migrate()
    with eng.begin() as conn:
        rows = conn.execute(sa.text("SELECT definition, event_id FROM impressions")).all()
        indexes = {r[1]: r[2] for r in conn.execute(sa.text("PRAGMA index_list(impressions)"))}
    assert rows == [(None, None)] * 3
    assert indexes.get("ix_impressions_event_id") == 1  # unique
    from eval.selective import served_feeds

    with dbmod.SessionLocal() as s:
        out = served_feeds(s)
    assert out["legacy_records"] == 3 and "unavailable" in out["repetition"]
    command.downgrade(cfg, "a4c7e2f9b1d3")  # and back
    with eng.begin() as conn:
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(impressions)"))}
        assert conn.execute(sa.text("SELECT count(*) FROM impressions")).scalar() == 3
    assert "definition" not in cols
    get_settings.cache_clear()
