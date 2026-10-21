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


# ------------------------------------------------------------------ 1e: truthful ingestion
def test_sold_out_listing_seen_again_unchanged_stays_unavailable(db):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing

    s = _seller(db)
    for _ in range(2):
        p = upsert_listing(db, s, RawListing("u1", "https://shop.example/u1", "coat ¥500", available=False)).product
    assert p.status == "unavailable"
    # ...and back in stock is recorded as a change, even though nothing else on the page changed
    p = upsert_listing(db, s, RawListing("u1", "https://shop.example/u1", "coat ¥500", available=True)).product
    assert p.status == "active" and any("unavailable → active" in (o.change or "") for o in p.observations)


def test_currency_and_purchase_link_changes_are_applied_and_recorded(db):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing

    s = _seller(db)
    a = {"weidian": "https://weidian.com/item.html?itemID=1"}
    b = {"weidian": "https://weidian.com/item.html?itemID=2"}
    upsert_listing(db, s, RawListing("v1", "https://shop.example/v1", "coat", price=500, currency="CNY", links=a))
    res = upsert_listing(db, s, RawListing("v1", "https://shop.example/v1", "coat", price=500, currency="USD", links=b))
    p = res.product
    assert p.currency == "USD" and p.links["weidian"].endswith("itemID=2")
    assert any("currency CNY → USD" in c for c in res.changes) and any("links changed" in c for c in res.changes)


def test_unstated_currency_is_unknown_not_cny(db):
    from app.ingestion.generic import parse_generic_product, shopify_product_to_listing
    from app.ingestion.pipeline import upsert_listing
    from app.services.landed_cost import estimate_landed_cost

    s = _seller(db)
    raw = shopify_product_to_listing({"id": 1, "handle": "coat", "title": "Coat", "variants": [{"price": "120.00"}]},
                                     "https://shop.example")  # fmt: skip
    p = upsert_listing(db, s, raw).product
    assert p.currency == "" and p.price == 120
    html = '<script type="application/ld+json">{"@type":"Product","name":"Knit","offers":{"price":"90"}}</script>'
    assert parse_generic_product(html, "https://shop.example/p/knit").currency == ""
    assert estimate_landed_cost(120, "", "overcoat", 0.1, {}) is None


def test_suffix_yen_prices_without_mistaking_item_codes():
    from app.ingestion.parsing import parse_price

    assert parse_price("YSL patchwork jacket (429￥)") == 429
    assert parse_price("货号8009 ¥919") == 919
    assert parse_price("8009¥919") == 919
    assert parse_price("专柜价12800￥ 现价899￥") == 899


WRAPPED = "https://x.yupoo.com/external?url=https%3A%2F%2Fweidian.com%2Fitem.html%3FitemID%3D7707075268"


def test_wrapped_purchase_links_resolve_to_their_destination():
    from app.ingestion.parsing import canonical_url, extract_purchase_links, weidian_item_id
    from app.ingestion.yupoo import parse_album_page

    assert extract_purchase_links(f"购买链接 {WRAPPED}") == {"weidian": "https://weidian.com/item.html?itemID=7707075268"}
    assert weidian_item_id(WRAPPED) == "7707075268"  # links stored before this fix still match
    html = (f'<span class="showalbumheader__gallerytitle">coat ¥500</span><div class="showalbumheader__gallerysubtitle">'
            f'<a href="{WRAPPED}">微店</a></div>')  # fmt: skip
    raw = parse_album_page(html, "https://s.x.yupoo.com/albums/1?uid=1")
    assert raw.links["weidian"] == "https://weidian.com/item.html?itemID=7707075268"
    # Classified by destination host, not by substring; non-web targets are not followed.
    assert extract_purchase_links("https://evil.example/weidian.com/item.html?itemID=1") == {}
    assert canonical_url("https://x.example/r?u=javascript%3Aalert(1)") == "https://x.example/r?u=javascript%3Aalert(1)"


def test_wrapped_and_direct_links_to_the_same_item_match(db):
    from app.services import dedup

    s1, s2 = _seller(db, "w1"), _seller(db, "w2")
    a = _product(db, s1, "a", links={"weidian": WRAPPED})  # as stored by older versions
    b = _product(db, s2, "b", links={"weidian": "https://weidian.com/item.html?itemID=7707075268"})
    dedup.match_product(db, a)
    db.commit()
    assert {r.relation for r, other in dedup.relations_for(db, a.id) if other == b.id} == {"same_purchase_link"}


def test_blocked_thumbnail_is_not_retried_through_the_original(env, web):
    import pytest

    from app.ingestion.http import BlockedError
    from app.ingestion.pipeline import fetch_and_analyse

    web.add("https://photo.yupoo.com/s/a/medium.jpg", "forbidden", status=403)
    web.add("https://photo.yupoo.com/s/a/big.jpg", make_image(), ctype="image/jpeg")
    with web.client() as c, pytest.raises(BlockedError):
        fetch_and_analyse(c, "https://photo.yupoo.com/s/a/medium.jpg", None, fallback="https://photo.yupoo.com/s/a/big.jpg")
    assert not any(str(r.url).endswith("big.jpg") for r in web.requests)
    # A merely missing thumbnail still falls back to the original.
    web.add("https://photo.yupoo.com/s/b/big.jpg", make_image(), ctype="image/jpeg")
    with web.client() as c:
        fetch_and_analyse(c, "https://photo.yupoo.com/s/b/medium.jpg", None, fallback="https://photo.yupoo.com/s/b/big.jpg")


def test_import_stops_at_a_blocked_image_host(db, web):
    from app.ingestion.pipeline import process_images

    s = _seller(db)
    urls = [f"https://img.example/x{i}.jpg" for i in range(4)]
    for u in urls:
        web.add(u, "no", status=403)
    p = _product(db, s, "blk", image_urls=urls)
    with web.client() as c:
        process_images(db, p, c)
    assert len([r for r in web.requests if r.url.host == "img.example"]) == 1
    assert p.images[0].fetch_error.startswith("blocked")


def test_scanner_downloads_no_photos_when_downloads_are_off(db, web, monkeypatch):
    from app.config import get_settings
    from app.ingestion.scan import scan_seller
    from app.models import Product, Seller
    from tests.test_scanner import BASE, _catalogue, serve

    monkeypatch.setattr(get_settings(), "download_images", False)
    albums, images = _catalogue(2)
    serve(web, albums, images)
    s = Seller(name="Shop", slug="shop1", platform="yupoo", base_url=BASE)
    db.add(s)
    db.commit()
    with web.client() as c:
        run = scan_seller(db, s, client=c)
    assert run.status == "succeeded"
    assert not any(r.url.host == "photo.yupoo.com" for r in web.requests)
    assert db.query(Product).filter_by(seller_id=s.id).count() == 2
