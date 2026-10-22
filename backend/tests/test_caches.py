"""Caches of derived results are keyed by the data they depend on (services/revisions.py): reused while nothing
relevant changed, recomputed after every kind of change that matters.

SYNTHETIC listings and evidence, made up for these tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


@pytest.fixture()
def client(env, monkeypatch):
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: 0)
    monkeypatch.setenv("WS_DEEP_ON_FEEDBACK", "false")
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def _listings(db, n=6):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller

    s = Seller(name="cache", slug="cache", platform="manual")
    db.add(s)
    db.flush()
    ps = [upsert_listing(db, s, RawListing(f"c{i}", f"https://cache.example/{i}", f"navy wool overcoat {i}",
                                           price=500.0 + 100 * i, currency="CNY")).product for i in range(n)]  # fmt: skip
    db.commit()
    return ps


def _rev(db, name):
    from app.services import revisions

    return revisions.current(db, name)[1]


# ----------------------------------------------------------------- the counters
def test_revisions_count_content_changes_only(env, db):
    from app.models import AppSetting, Evidence, Feedback, ProductRelation

    ps = _listings(db)
    p = ps[0]
    before = _rev(db, "products")
    p.is_new = False  # seen: not a content change
    db.commit()
    assert _rev(db, "products") == before
    p.price = 999.0
    db.commit()
    assert _rev(db, "products") == before + 1
    for name, row in (("evidence", Evidence(product_id=p.id, kind="buyer_report", aspect="construction", claim="ok")),
                      ("relations", ProductRelation(a_id=ps[0].id, b_id=ps[1].id, relation="similar_photo")),
                      ("feedback", Feedback(product_id=p.id, action="like")),
                      ("settings", AppSetting(key="x_test", value={"a": 1}))):  # fmt: skip
        n = _rev(db, name)
        db.add(row)
        db.commit()
        assert _rev(db, name) == n + 1, name
        db.delete(row)
        db.commit()
        assert _rev(db, name) == n + 2, name


def test_each_database_has_its_own_instance_number(env, tmp_path):
    from sqlalchemy import text

    from app.db import create_all, get_engine, init_engine

    numbers = set()
    for i in range(2):
        init_engine(f"sqlite:///{tmp_path / f'other{i}.db'}")
        create_all()
        with get_engine().connect() as conn:
            numbers.add(conn.execute(text("SELECT n FROM data_revisions WHERE name = 'instance'")).scalar())
    assert len(numbers) == 2


# ----------------------------------------------------------------- the feature space
def test_the_feature_space_survives_decisions_and_views_but_not_content_or_profile_changes(client, db):
    from app.services import preference_model as pm
    from app.services.settings_store import get_setting

    ps = _listings(db)

    def space():
        db.expire_all()
        return pm.build_feature_space(db, get_setting(db, "taste_profile"), "lite-v1")

    first = space()
    client.post("/api/feedback", json={"product_id": ps[0].id, "action": "like"})
    client.get(f"/api/products/{ps[1].id}")  # marks it as seen
    assert space() is first
    fid = client.post("/api/feedback", json={"product_id": ps[2].id, "action": "dislike"}).json()["feedback_id"]
    client.delete(f"/api/feedback/{fid}")  # Undo
    assert space() is first
    client.patch(f"/api/products/{ps[3].id}", json={"category": "trousers"})  # product update
    second = space()
    assert second is not first and second.X[second.row[ps[3].id]].tolist() != first.X[first.row[ps[3].id]].tolist()
    taste = client.get("/api/settings").json()["taste_profile"]
    client.put("/api/settings/taste_profile", json={**taste, "preferred_colours": ["red"]})
    assert space() is not second


# ----------------------------------------------------------------- quality
def _quality(client, pid):
    return client.get(f"/api/products/{pid}").json()["quality"]


def test_quality_is_reused_across_decisions_and_recomputed_after_evidence_matching_rates_and_updates(client, db):
    from app.services.presenter import Context

    ps = _listings(db)
    p, other = ps[0], ps[1]
    q0 = Context(db).quality(p)
    client.post("/api/feedback", json={"product_id": ps[5].id, "action": "love"})
    db.expire_all()
    assert Context(db).quality(p) is q0  # a decision changes nothing quality depends on

    # Evidence added, then removed
    ev = {"kind": "buyer_report", "aspect": "construction", "claim": "Seams split after a week", "polarity": -1,
          "strength": 1.0, "source_url": "https://forum.example/t/1"}  # fmt: skip
    eid = client.post(f"/api/products/{p.id}/evidence", json=ev).json()["id"]
    assert _quality(client, p.id)["dimensions"]["construction"]["score"] is not None
    client.delete(f"/api/evidence/{eid}")
    assert _quality(client, p.id)["dimensions"]["construction"]["score"] is None

    # A match decision: the cheaper listing of the same garment now counts against this one's value
    pricey, cheap = ps[4], ps[0]
    assert not any("cheaper" in n for n in _quality(client, pricey.id)["dimensions"]["value"]["notes"])
    client.post(f"/api/products/{pricey.id}/match", json={"other_id": cheap.id, "same": True})
    assert any("cheaper" in n for n in _quality(client, pricey.id)["dimensions"]["value"]["notes"])

    # A product update (price) and a rate change
    before = _quality(client, other.id)["dimensions"]["value"]["price_position"]
    client.patch(f"/api/products/{other.id}", json={"price": 5000})
    assert _quality(client, other.id)["dimensions"]["value"]["price_position"] != before
    cur = client.get("/api/settings").json()["currency"]
    card = client.get(f"/api/products/{other.id}").json()
    client.put("/api/settings/currency", json={**cur, "cny_to_gbp": cur["cny_to_gbp"] * 2})
    assert client.get(f"/api/products/{other.id}").json()["price_gbp"] == pytest.approx(2 * card["price_gbp"])


def test_an_imported_backup_with_evidence_is_reflected_at_once(client, db):
    ps = _listings(db)
    p = ps[0]
    ev = {"kind": "verified", "aspect": "construction", "claim": "Inspected in hand: neat seams", "polarity": 1,
          "strength": 1.0, "source_url": "https://forum.example/t/9"}  # fmt: skip
    eid = client.post(f"/api/products/{p.id}/evidence", json=ev).json()["id"]
    backup = client.get("/api/backup/export", params={"include_images": False}).content
    client.delete(f"/api/evidence/{eid}")
    assert _quality(client, p.id)["dimensions"]["construction"]["score"] is None
    r = client.post("/api/backup/import", files={"file": ("b.json", backup, "application/json")})
    assert r.status_code == 200 and r.json()["evidence"] == 1, r.text
    assert _quality(client, p.id)["dimensions"]["construction"]["score"] is not None


def test_cached_results_never_cross_databases(env, tmp_path, monkeypatch):
    """Two databases whose counters happen to be equal still never share a cached assessment."""
    from app.db import SessionLocal, create_all, init_engine
    from app.services.presenter import Context

    titles = []
    for i, colour in enumerate(("navy", "red")):
        init_engine(f"sqlite:///{tmp_path / f'db{i}.db'}")
        create_all()
        db = SessionLocal()
        from app.models import Evidence

        p = _listings(db, n=1)[0]
        if colour == "red":
            db.add(Evidence(product_id=p.id, kind="verified", aspect="construction", claim="neat", polarity=1))
            db.commit()
            db.refresh(p)
        titles.append(Context(db).quality(p)["dimensions"]["construction"]["score"])
        db.close()
    assert titles[0] is None and titles[1] is not None


# ----------------------------------------------------------------- written-profile scores
def test_profile_scores_follow_profile_edits(client, db):
    ps = _listings(db)
    taste = client.get("/api/settings").json()["taste_profile"]

    def reasons():
        return " ".join(client.get(f"/api/products/{ps[0].id}").json()["recommendation"]["reasons"])

    client.put("/api/settings/taste_profile", json={**taste, "preferred_colours": ["navy"], "favourite_brands": []})
    with_navy = client.get(f"/api/products/{ps[0].id}").json()["recommendation"]["components"]
    client.put("/api/settings/taste_profile", json={**taste, "preferred_colours": ["red"], "favourite_brands": []})
    without = client.get(f"/api/products/{ps[0].id}").json()["recommendation"]["components"]
    assert with_navy != without
    assert isinstance(reasons(), str)


# ----------------------------------------------------------------- the photo backend in use
def test_status_says_which_photo_backend_is_in_use_and_what_needs_reanalysis(client, db, monkeypatch):
    import numpy as np

    from app.services import embeddings as emb

    ps = _listings(db, n=3)
    for p in ps[:2]:
        p.image_embedding = emb.to_bytes(emb.normalise(np.ones(192, dtype=np.float32)))
        p.embedding_backend = emb.get_image_backend().name
    db.commit()
    e = client.get("/api/status").json()["embedding"]
    assert e["is_clip"] is False and e["recompute_needed"] is False and e["analysed_current"] == 2
    monkeypatch.setattr(emb.LiteImageBackend, "name", "lite-test-v2")  # e.g. CLIP unavailable, or a new version
    emb.reset_backend()
    e = client.get("/api/status").json()["embedding"]
    assert e["image_backend"] == "lite-test-v2" and e["analysed_other"] == 2 and e["recompute_needed"] is True


def test_a_restored_snapshot_is_a_new_instance(env, db, tmp_path):
    """A restored copy keeps its data's revisions but gets its own instance number, so nothing cached for the
    original database is ever served for the copy."""
    import sqlite3
    from pathlib import Path

    from app.config import get_settings
    from app.services import revisions
    from app.services.backup import restore_full_snapshot, write_full_snapshot

    _listings(db, 2)
    instance, products = revisions.current(db, "products")
    s = get_settings()
    snap = tmp_path / "snap.zip"
    write_full_snapshot(Path(s.database_url.removeprefix("sqlite:///")), s.data_dir, snap)
    out = restore_full_snapshot(snap, tmp_path / "restored")
    with sqlite3.connect(out["database"]) as c:
        rows = dict(c.execute("SELECT name, n FROM data_revisions").fetchall())
    assert rows["products"] == products
    assert rows["instance"] != instance
