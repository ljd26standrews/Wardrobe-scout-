import pytest

from app.ingestion.generic import parse_generic_product, parse_weidian_html, shopify_product_to_listing
from app.ingestion.http import BlockedError
from app.ingestion.yupoo import YupooAdapter, parse_album_page, parse_listing_page

from .conftest import fixture_text, make_image

BASE = "https://testseller.x.yupoo.com"


def test_listing_page_parse():
    page = parse_listing_page(fixture_text("yupoo_albums_p1.html"), f"{BASE}/albums?page=1")
    assert [a.external_id for a in page.albums] == ["1001", "1002"]
    assert page.albums[0].title.startswith("【8009】")
    assert page.albums[0].cover_url == "https://photo.yupoo.com/testseller/aaa1/big.jpg"
    assert page.total_pages == 2
    assert page.next_url == f"{BASE}/albums?page=2"
    assert ("卫衣 Hoodies", f"{BASE}/categories/501") in page.categories


def test_last_page_has_no_next():
    page = parse_listing_page(fixture_text("yupoo_albums_p2.html"), f"{BASE}/albums?page=2")
    assert page.next_url is None and len(page.albums) == 1


def test_album_page_parse():
    raw = parse_album_page(fixture_text("yupoo_album_1001.html"), f"{BASE}/albums/1001?uid=1")
    assert raw.external_id == "1001"
    assert raw.price == 919
    assert raw.image_urls == [
        "https://photo.yupoo.com/testseller/aaa1/big.jpg",
        "https://photo.yupoo.com/testseller/aaa2/big.jpg",
    ]
    assert raw.links["weidian"] == "https://weidian.com/item.html?itemID=7707075268"
    assert "70%羊毛" in raw.description
    assert raw.image_referer.endswith("/albums/1001?uid=1")


def _serve_catalogue(web):
    web.add(f"{BASE}/robots.txt", "User-agent: *\nDisallow: /private\n", ctype="text/plain")
    web.add(f"{BASE}/albums?page=1", fixture_text("yupoo_albums_p1.html"))
    web.add(f"{BASE}/albums?page=2", fixture_text("yupoo_albums_p2.html"))
    for i in ("1001", "1002", "1003"):
        web.add(f"{BASE}/albums/{i}?uid=1", fixture_text(f"yupoo_album_{i}.html"))
    shapes = {
        "aaa1": ("hoodie", (25, 25, 25)),
        "aaa2": ("hoodie", (60, 60, 60)),
        "bbb1": ("trousers", (70, 70, 72)),
        "ccc1": ("tee", (250, 250, 248)),
    }
    for key, (shape, col) in shapes.items():
        web.add(f"https://photo.yupoo.com/testseller/{key}/big.jpg", make_image(col, shape), ctype="image/jpeg")


def test_adapter_traverses_pagination(web):
    _serve_catalogue(web)
    with web.client() as client:
        refs = list(YupooAdapter(client).iter_album_refs(BASE, max_pages=10))
    assert [r.external_id for r in refs] == ["1001", "1002", "1003"]


def test_robots_disallow_blocks(web):
    web.add(f"{BASE}/robots.txt", "User-agent: *\nDisallow: /\n", ctype="text/plain")
    web.add(f"{BASE}/albums?page=1", fixture_text("yupoo_albums_p1.html"))
    with web.client() as client, pytest.raises(BlockedError):
        list(YupooAdapter(client).iter_album_refs(BASE, max_pages=2))


def test_http_403_and_captcha_are_blocks_not_retries(web):
    web.add(f"{BASE}/robots.txt", "", status=404)
    web.add(f"{BASE}/albums?page=1", "denied", status=403)
    with web.client() as client, pytest.raises(BlockedError):
        list(YupooAdapter(client).iter_album_refs(BASE, max_pages=2))
    web.add(f"{BASE}/albums?page=1", fixture_text("captcha.html"))
    with web.client() as client, pytest.raises(BlockedError):
        list(YupooAdapter(client).iter_album_refs(BASE, max_pages=2))
    assert sum(1 for r in web.requests if r.url.path == "/albums") == 2  # no retry storms


def test_scan_seller_end_to_end(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Product, Seller

    _serve_catalogue(web)
    seller = Seller(name="Test", slug="testseller", platform="yupoo", base_url=BASE)
    db.add(seller)
    db.commit()
    with web.client() as client:
        run = scan_seller(db, seller, mode="full", client=client)
    assert run.status == "succeeded", run.message
    assert run.products_new == 3
    products = {p.external_id: p for p in db.query(Product)}
    hoodie = products["1001"]
    assert hoodie.category == "hoodie" and hoodie.price == 919 and hoodie.seller_item_code == "8009"
    assert hoodie.materials[0] == {"fibre": "wool", "pct": 70.0}
    assert hoodie.data_origin == "scraped" and hoodie.completeness == "complete"
    assert all(i.local_path for i in hoodie.images) and hoodie.image_embedding is not None
    # every extracted claim has provenance
    claims = [e for e in hoodie.evidence if e.kind == "seller_claim"]
    assert claims and all(e.source_url == hoodie.source_url and e.captured_at for e in claims)
    # Image requests carried the album page as Referer (hotlink protection)
    img_req = [r for r in web.requests if r.url.host == "photo.yupoo.com"][0]
    assert img_req.headers["referer"].startswith(f"{BASE}/albums/")
    # Second incremental scan: nothing new, no product pages re-fetched
    before = len(web.requests)
    with web.client() as client:
        run2 = scan_seller(db, seller, mode="incremental", client=client)
    assert run2.products_new == 0
    assert not any("/albums/" in r.url.path and r.url.path != "/albums" for r in web.requests[before:])


def test_price_change_and_removal_tracking(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Product, Seller

    _serve_catalogue(web)
    seller = Seller(name="Test", slug="testseller", platform="yupoo", base_url=BASE)
    db.add(seller)
    db.commit()
    with web.client() as client:
        scan_seller(db, seller, mode="full", client=client)
    # Price changes on the album page; album 1003 disappears from the catalogue
    web.add(f"{BASE}/albums/1001?uid=1", fixture_text("yupoo_album_1001.html").replace("¥919", "¥899"))
    web.add(
        f"{BASE}/albums?page=1",
        fixture_text("yupoo_albums_p1.html")
        .replace("¥919", "¥899")
        .replace('<a href="/albums?page=2">2</a>', "")
        .replace('max="2"', 'max="1"')
        .replace("/albums?page=2", "/x"),
    )
    with web.client() as client:
        run = scan_seller(db, seller, mode="full", client=client)
    hoodie = db.query(Product).filter_by(external_id="1001").one()
    assert hoodie.price == 899
    assert any(o.change and "919 → 899" in o.change for o in hoodie.observations)
    assert run.products_updated >= 1
    assert db.query(Product).filter_by(external_id="1003").one().status == "unavailable"


def test_jsonld_generic_product():
    raw = parse_generic_product(fixture_text("jsonld_product.html"), "https://shop.example.com/p/cc-01")
    assert raw.title.startswith("Cashmere crewneck")
    assert raw.price == 890 and raw.currency == "CNY" and raw.available is True
    assert raw.external_id == "CC-01" and len(raw.image_urls) == 2


def test_weidian_embedded_json():
    raw = parse_weidian_html(fixture_text("weidian_item.html"), "https://weidian.com/item.html?itemID=7872986192")
    assert raw.external_id == "7872986192"
    assert raw.title == "MM 羊毛羊绒双头拉链夹克 黑色"
    assert raw.price == 528
    assert raw.image_urls[0] == "https://si.geilicdn.com/test-abc_800_800.jpg"


def test_weidian_without_data_raises_helpful_error():
    from app.ingestion.http import FetchError

    with pytest.raises(FetchError, match="bookmarklet"):
        parse_weidian_html("<html><title>微店</title></html>", "https://weidian.com/item.html?itemID=1")


def test_shopify_feed_mapping():
    p = {
        "id": 9,
        "handle": "wool-trouser",
        "title": "Wool trouser",
        "body_html": "<p>100% virgin wool</p>",
        "product_type": "Trousers",
        "vendor": "House",
        "options": [{"name": "Size", "values": ["46", "48"]}],
        "variants": [{"price": "250.00", "available": True}],
        "images": [{"src": "https://s.example.com/1.jpg"}],
    }
    raw = shopify_product_to_listing(p, "https://s.example.com")
    assert raw.source_url == "https://s.example.com/products/wool-trouser"
    assert raw.sizes == ["46", "48"] and raw.price == 250 and raw.available is True
