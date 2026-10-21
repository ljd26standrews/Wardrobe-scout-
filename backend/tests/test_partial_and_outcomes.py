"""Pages that arrive cut short, and telling blocked / invalid / failed / empty outcomes apart."""

from __future__ import annotations

from pathlib import Path

from app.ingestion.yupoo import parse_album_page

LIVE = Path(__file__).parent / "fixtures" / "live"
MBKF = (LIVE / "yupoo_madebykungfu_album_259067099.html").read_text("utf-8")
MBKF_URL = "https://madebykungfu.x.yupoo.com/albums/259067099?uid=1"


def _seller(db, slug="kf"):
    from app.models import Seller

    s = Seller(name=slug, slug=slug, platform="yupoo")
    db.add(s)
    db.commit()
    return s


def test_cut_short_page_keeps_what_it_did_not_reach(db):
    from app.ingestion.pipeline import upsert_listing

    seller = _seller(db)
    p = upsert_listing(db, seller, parse_album_page(MBKF, MBKF_URL)).product
    db.commit()
    before = {
        "price": p.price, "links": dict(p.links), "description": p.description_original, "hash": p.content_hash,
        "images": [i.url for i in sorted(p.images, key=lambda i: i.position)], "read": p.detail_fetched_at,
    }  # fmt: skip
    assert before["price"] == 429 and len(before["images"]) == 50 and "weidian" in before["links"]

    third = MBKF[: len(MBKF) // 3]  # how the benchmark's fault injection truncates a page
    after_title = MBKF[: MBKF.index("showalbumheader__gallerysubtitle")]  # cut before the description
    for cut in (third, after_title):
        raw = parse_album_page(cut, MBKF_URL)
        assert raw.partial and len(raw.image_urls) < 50
        res = upsert_listing(db, seller, raw)
        db.commit()
        assert res.changes == [] and not res.created  # nothing recorded as removed
        p = res.product
        assert p.price == before["price"] and p.links == before["links"]
        assert p.description_original == before["description"]
        assert [i.url for i in sorted(p.images, key=lambda i: i.position)] == before["images"]
        assert p.content_hash == before["hash"] and p.detail_fetched_at == before["read"]

    full = upsert_listing(db, seller, parse_album_page(MBKF, MBKF_URL))  # a complete read changes nothing
    assert full.changes == [] and full.product.content_hash == before["hash"]


def test_a_price_genuinely_removed_from_a_complete_page_is_recorded(db):
    from app.ingestion.pipeline import upsert_listing

    seller = _seller(db)
    upsert_listing(db, seller, parse_album_page(MBKF, MBKF_URL))
    db.commit()
    res = upsert_listing(db, seller, parse_album_page(MBKF.replace("(429￥)", ""), MBKF_URL))
    assert res.product.price is None and "price no longer stated" in res.changes


def test_product_first_read_from_a_cut_page_is_read_again_next_scan(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Product
    from tests.test_scanner import BASE, _catalogue, _seller, serve

    albums, images = _catalogue(3)
    serve(web, albums, images)
    cut_url = f"{BASE}/albums/{albums[1]['id']}?uid=1"
    full_html = web.routes[cut_url][1]
    web.add(cut_url, full_html[: len(full_html) // 3])  # this album's page arrives cut short
    seller = _seller(db)
    with web.client() as c:
        scan_seller(db, seller, client=c)
    p = db.query(Product).filter_by(external_id=albums[1]["id"]).one()
    assert p.detail_fetched_at is None and p.completeness == "partial"

    web.add(cut_url, full_html)
    before = len(web.requests)
    with web.client() as c:
        scan_seller(db, seller, mode="incremental", client=c)
    refetched = [str(r.url) for r in web.requests[before:] if "/albums/" in str(r.url)]
    assert refetched == [cut_url]  # only the partly read album is read again
    db.refresh(p)
    assert p.detail_fetched_at is not None and len(p.images) == 3


def test_import_outcomes_are_told_apart(db, web):
    from app.ingestion.scan import import_url

    web.add("https://weidian.com/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    shell = "<html><head><title>商品详情</title></head><body><div id='app'></div></body></html>"
    web.add("https://weidian.com/item.html?itemID=1", shell)
    web.add("https://weidian.com/item.html?itemID=2", "gone", status=404)
    web.add("https://weidian.com/item.html?itemID=3", "denied", status=403)
    with web.client() as c:
        out = {i: import_url(db, f"https://weidian.com/item.html?itemID={i}", client=c) for i in (1, 2, 3)}
    assert {i: r["outcome"] for i, r in out.items()} == {1: "invalid", 2: "failed", 3: "blocked"}
    assert not any(r["ok"] for r in out.values())


def test_unrecognised_catalogue_layout_is_reported_as_invalid(db, web):
    from app.ingestion.scan import scan_seller
    from tests.test_scanner import BASE, _seller

    web.add(f"{BASE}/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    web.add(f"{BASE}/albums?page=1", "<html><body><p>Welcome</p></body></html>")
    seller = _seller(db)
    with web.client() as c:
        run = scan_seller(db, seller, client=c)
    assert run.status == "invalid" and "not recognised" in run.message
    assert seller.last_scan_status == "invalid"
