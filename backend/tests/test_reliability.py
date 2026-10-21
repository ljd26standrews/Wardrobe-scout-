"""Regressions for the reliability follow-up (audit of f1e9ecb): desired behaviour, not defect probes."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import make_image


def _seller(db, slug="s1", platform="manual"):
    from app.models import Seller

    s = Seller(name=slug, slug=slug, platform=platform)
    db.add(s)
    db.commit()
    return s


def _product(db, seller, ext, **kw):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing

    raw = RawListing(ext, kw.pop("url", f"https://shop.example/{ext}"), kw.pop("title", f"wool coat {ext} ¥500"), **kw)
    p = upsert_listing(db, seller, raw).product
    db.commit()
    return p


# ------------------------------------------------------------------ 1a: one relation per pair
def test_purchase_link_and_shared_photos_make_one_relation_with_both_signals(db, web):
    from app.ingestion.pipeline import process_images
    from app.models import ProductRelation
    from app.services import dedup

    s1, s2 = _seller(db, "x1"), _seller(db, "x2")
    link = {"weidian": "https://weidian.com/item.html?itemID=123456789"}
    imgs = [make_image((40, 40, 90), "hoodie", seed=1), make_image((90, 40, 40), "tee", seed=2)]
    prods = []
    for s, ext in ((s1, "p"), (s2, "q")):
        urls = [f"https://img.example/{ext}{i}.jpg" for i in range(2)]
        for u, d in zip(urls, imgs, strict=True):
            web.add(u, d, ctype="image/jpeg")
        prods.append(_product(db, s, ext, links=link, image_urls=urls))
    with web.client() as c:
        for p in prods:
            process_images(db, p, c)
    db.commit()
    for _ in range(2):  # repeated processing must be stable
        for p in prods:
            dedup.match_product(db, p)
        db.commit()
    rels = db.query(ProductRelation).all()
    assert len(rels) == 1
    r = rels[0]
    assert r.relation == "same_purchase_link"
    assert r.signals["purchase_ids"] == ["weidian:123456789"] and r.signals["matching_photos"] == 2


# ------------------------------------------------------------------ 1c: settings validation
def test_invalid_settings_are_refused(env, db):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import AUTH

    with TestClient(create_app(), headers=AUTH) as c:
        rec = c.get("/api/settings").json()["recommender"]
        for bad in ({"cold_start_k": -1}, {"cold_start_k": 0}, {"cold_start_k": True}, {"engine": "magic"},
                    {"exploration_rate": 0.9}, {"near_duplicate_threshold": "high"}):  # fmt: skip
            r = c.put("/api/settings/recommender", json={**rec, **bad})
            assert r.status_code == 422, (bad, r.status_code)
        r = c.put("/api/settings/landed_cost", json={**c.get("/api/settings").json()["landed_cost"], "vat_pct": 250})
        assert r.status_code == 422 and "vat_pct" in r.json()["detail"]
        assert c.put("/api/settings/recommender", json={**rec, "cold_start_k": 30}).status_code == 200
        assert c.get("/api/settings").json()["recommender"]["cold_start_k"] == 30


def test_bad_values_already_stored_cannot_break_the_feed(env, db):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import AppSetting
    from app.services.settings_store import DEFAULTS, get_setting
    from tests.conftest import AUTH

    s = _seller(db)
    ps = [_product(db, s, f"k{i}") for i in range(4)]
    with TestClient(create_app(), headers=AUTH, raise_server_exceptions=False) as c:
        c.post("/api/feedback", json={"product_id": ps[0].id, "action": "love", "reasons": []})
        for engine in ("bayes", "legacy"):
            for k in range(-12, 1):
                row = db.get(AppSetting, "recommender") or AppSetting(key="recommender")
                row.value = {**DEFAULTS["recommender"], "cold_start_k": k, "engine": engine}
                db.merge(row)
                db.commit()
                assert c.get("/api/feed?limit=5").status_code == 200, (engine, k)
    db.expire_all()
    assert get_setting(db, "recommender")["cold_start_k"] == 1


# ------------------------------------------------------------------ 1d: backup / restore
def _rich_history(db):
    """A database with every kind of personal state the backup must preserve."""
    from app.models import Evidence, Feedback, InspirationImage, ProductImage, SavedItem, WardrobeItem
    from app.services import dedup
    from app.services.images import store_image_bytes
    from app.services.recommender import snapshot
    from app.services.settings_store import get_setting, set_setting

    s = _seller(db, "shopa", platform="yupoo")
    p1, p2, p3, p4, p5 = (_product(db, s, f"h{i}") for i in range(1, 6))
    manual = _product(db, None, "manual1", url="manual:1")
    manual.data_origin = "manual"  # entered by you, referenced by nothing
    rel, _ = store_image_bytes(make_image((10, 10, 120), "tee", seed=9), "uploads")
    manual.images = [ProductImage(url=f"upload:{rel}", local_path=rel, fetch_status="uploaded", position=0)]
    db.add(Feedback(product_id=p1.id, action="love", reasons=[], snapshot=snapshot(p1)))
    db.add(Feedback(product_id=p2.id, action="dislike", reasons=["colour"], snapshot=snapshot(p2)))
    db.add(SavedItem(product_id=p1.id, list_name="loved", priority=1, note="for winter", target_size="M"))
    db.add(Evidence(product_id=p3.id, kind="buyer_report", aspect="construction", claim="clean seams in QC",
                    polarity=1, strength=0.8, source_url="https://r.example/qc", created_by="user"))  # fmt: skip
    db.add(Evidence(seller_id=s.id, kind="buyer_report", aspect="service", claim="shipped fast", polarity=1,
                    strength=0.6, created_by="user"))  # fmt: skip
    w_rel, _ = store_image_bytes(make_image((120, 100, 80), "hoodie", seed=4), "uploads")
    db.add(WardrobeItem(name="Camel overcoat", category="overcoat", rating=5, image_path=w_rel, product_id=p1.id))
    i_rel, _ = store_image_bytes(make_image((30, 30, 30), "trousers", seed=5), "uploads")
    db.add(InspirationImage(path=i_rel, polarity=1, note="this drape"))
    db.commit()
    dedup.set_user_decision(db, p4.id, p5.id, False)
    taste = get_setting(db, "taste_profile")
    taste["notes"] = "my own words"
    set_setting(db, "taste_profile", taste)
    return s


def _counts(db) -> dict:
    from app.models import (Evidence, Feedback, InspirationImage, Product, ProductRelation, SavedItem,
                            WardrobeItem)  # fmt: skip

    return {
        "products": db.query(Product).count(),
        "feedback": db.query(Feedback).count(),
        "saved": db.query(SavedItem).count(),
        "evidence": db.query(Evidence).filter_by(created_by="user").count(),
        "wardrobe": db.query(WardrobeItem).count(),
        "inspiration": db.query(InspirationImage).count(),
        "decisions": db.query(ProductRelation).filter(ProductRelation.user_confirmed.isnot(None)).count(),
    }


def _fresh_install(tmp_path, monkeypatch, name):
    from app.config import get_settings
    from app.db import SessionLocal, create_all, init_engine

    d = tmp_path / name
    monkeypatch.setenv("WS_DATA_DIR", str(d))
    monkeypatch.setenv("WS_DATABASE_URL", f"sqlite:///{d}/ws.db")
    get_settings.cache_clear()
    get_settings()  # creates the folders
    init_engine(f"sqlite:///{d}/ws.db")
    create_all()
    return SessionLocal(), d


def test_backup_restores_everything_into_a_fresh_install_and_reimport_changes_nothing(env, db, tmp_path, monkeypatch):
    from app.models import Evidence, InspirationImage, Product, ProductRelation, SavedItem, WardrobeItem
    from app.services.backup import export_all, import_all
    from app.services.images import resolve_path
    from app.services.settings_store import get_setting

    _rich_history(db)
    data = export_all(db)
    db.close()

    fresh, d = _fresh_install(tmp_path, monkeypatch, "restored")
    import_all(fresh, data)
    c1 = _counts(fresh)
    assert c1 == {"products": 6, "feedback": 2, "saved": 1, "evidence": 2, "wardrobe": 1, "inspiration": 1,
                  "decisions": 1}, c1  # fmt: skip
    by_ext = {p.external_id: p for p in fresh.query(Product)}
    ev = fresh.query(Evidence).filter(Evidence.product_id.isnot(None)).one()
    assert ev.product_id == by_ext["h3"].id and ev.source_url == "https://r.example/qc"  # undecided product kept
    saved = fresh.query(SavedItem).one()
    assert saved.product_id == by_ext["h1"].id and saved.note == "for winter" and saved.target_size == "M"
    rel = fresh.query(ProductRelation).one()
    assert {rel.a_id, rel.b_id} == {by_ext["h4"].id, by_ext["h5"].id} and rel.user_confirmed is False
    w = fresh.query(WardrobeItem).one()
    assert w.product_id == by_ext["h1"].id and w.embedding is not None and resolve_path(w.image_path).exists()
    insp = fresh.query(InspirationImage).one()
    assert insp.embedding is not None and str(resolve_path(insp.path)).startswith(str(d))
    up = by_ext["manual1"].images[0]
    assert up.fetch_status == "uploaded" and resolve_path(up.local_path).exists()
    assert get_setting(fresh, "taste_profile")["notes"] == "my own words"

    # Importing again, and into a database that has other data too, changes nothing.
    fresh.add(WardrobeItem(name="Something else", category="t-shirt"))
    fresh.commit()
    before = _counts(fresh)
    second = import_all(fresh, data)
    assert _counts(fresh) == before
    assert sum(second.values()) == 0, second


def test_version_1_backups_still_import_without_duplicating(env, db):
    from app.services.backup import import_all

    v1 = {
        "format": "wardrobe-scout-backup", "version": 1, "settings": {}, "sellers": [], "watched_urls": [],
        "products": [], "feedback": [], "saved": [],
        "evidence": [{"product": None, "seller": None, "kind": "user_note", "aspect": "fit", "claim": "runs small",
                      "polarity": -1, "strength": 0.7, "source_url": None, "captured_at": "2026-10-01T10:00:00"}],
        "wardrobe": [{"name": "Navy coat", "category": "overcoat", "colours": [], "materials": [], "seasons": []}],
        "inspiration": [],
    }  # fmt: skip
    import_all(db, v1)
    import_all(db, v1)
    assert _counts(db)["wardrobe"] == 1 and _counts(db)["evidence"] == 1


def test_full_snapshot_restores_database_and_photos_into_a_new_folder(env, db, tmp_path):
    import sqlite3

    import pytest

    from app.config import get_settings
    from app.services.backup import restore_full_snapshot, write_full_snapshot

    _rich_history(db)
    s = get_settings()
    snap = tmp_path / "snap.zip"
    res = write_full_snapshot(Path(s.database_url.removeprefix("sqlite:///")), s.data_dir, snap)
    assert res["files"] >= 3
    target = tmp_path / "moved"
    out = restore_full_snapshot(snap, target)
    with sqlite3.connect(out["database"]) as c:
        assert c.execute("select count(*) from feedback").fetchone()[0] == 2
        paths = [r[0] for r in c.execute("select image_path from wardrobe_items")]
    assert all((target / p).exists() for p in paths)
    with pytest.raises(ValueError):
        restore_full_snapshot(snap, target)  # never overwrites an existing installation


def test_snapshot_restore_rejects_paths_outside_the_folder(tmp_path):
    import json
    import zipfile

    import pytest

    from app.services.backup import restore_full_snapshot

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "wardrobe-scout-snapshot", "version": 1}))
        z.writestr("../escape.txt", "x")
    with pytest.raises(ValueError):
        restore_full_snapshot(bad, tmp_path / "t")
    assert not (tmp_path / "escape.txt").exists()
