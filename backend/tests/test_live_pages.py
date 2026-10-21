"""Parsers against real seller pages (trimmed captures; see fixtures/live/README.md)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ingestion.generic import parse_weidian_html
from app.ingestion.http import BlockedError, FetchError
from app.ingestion.parsing import parse_price
from app.ingestion.yupoo import YupooAdapter, parse_album_page, parse_listing_page

LIVE = Path(__file__).parent / "fixtures" / "live"


def page(name: str) -> str:
    return (LIVE / name).read_text("utf-8")


def photo_key(url: str) -> tuple[str, str]:
    parts = url.split("photo.yupoo.com/", 1)[1].split("/")
    return parts[0], parts[1]


ALBUMS = {
    "madebykungfu": ("yupoo_madebykungfu_album_259067099.html", "https://madebykungfu.x.yupoo.com/albums/259067099?uid=1",
                     50, 429.0, "7875016861"),
    "aristide": ("yupoo_aristide_album_224673345.html", "https://aristide.x.yupoo.com/albums/224673345?uid=1",
                 7, 480.0, "7697357516"),
    "stitches": ("yupoo_stitches_album_250109320.html", "https://stitches-reps-2.x.yupoo.com/albums/250109320?uid=1",
                 20, 485.0, "7830134555"),
}  # fmt: skip


@pytest.mark.parametrize("seller", sorted(ALBUMS))
def test_album_lists_each_photo_once(seller):
    fname, url, n_photos, price, item = ALBUMS[seller]
    raw = parse_album_page(page(fname), url)
    keys = [photo_key(u) for u in raw.image_urls]
    assert len(keys) == len(set(keys)) == n_photos  # one entry per photo, not one per size variant
    assert len(raw.image_thumbs) == len(raw.image_urls)
    assert raw.price == price
    assert raw.links["weidian"].endswith(f"itemID={item}") or f"itemID={item}&" in raw.links["weidian"]


def test_html_entities_do_not_leak_into_links():
    fname, url, *_ = ALBUMS["aristide"]
    raw = parse_album_page(page(fname), url)
    assert "&amp;" not in raw.links["weidian"]
    assert raw.links["weidian"] == "https://weidian.com/item.html?itemID=7697357516&spider_token=2564"


def test_weidian_item_read_from_embedded_data():
    raw = parse_weidian_html(page("weidian_item_7707075268.html"), "https://weidian.com/item.html?itemID=7707075268")
    assert raw.external_id == "7707075268"
    assert raw.title == "Item ID 8009"  # not the page's generic <title>
    assert raw.price == 919.0
    assert raw.sizes == ["44", "46", "48"]
    assert raw.available is True
    assert raw.image_urls and all("geilicdn.com" in u for u in raw.image_urls)


def test_weidian_shell_page_is_refused_not_stored():
    html = "<html><head><title>商品详情</title></head><body><div id='app'></div></body></html>"
    with pytest.raises(FetchError):
        parse_weidian_html(html, "https://weidian.com/item.html?itemID=1")


NOTICES = {
    "madebykungfu": {"New whatsapp", "Old one got deleted, Join the new server", "Click to follow: madebykungfu.kf",
                     "Direct order guide", "Important (read before order)!"},
    "aristide": {"About Aristide", "Contact Us", "Discord", "Our Manufacturers", "How to Care for Cashmere and Wool",
                 "Purchase Notice", "Women Catalogue"},
    "stitches_reps_2": set(),  # "Mr. : P1~P2", "P4"... are ambiguous price-tier albums: left as products
}  # fmt: skip


@pytest.mark.parametrize("seller", sorted(NOTICES))
def test_shop_notice_albums_are_flagged_conservatively(seller):
    host = seller.replace("_", "-")
    lp = parse_listing_page(page(f"yupoo_{seller}_listing.html"), f"https://{host}.x.yupoo.com/albums?page=1")
    flagged = {a.title for a in lp.albums if a.notice}
    assert flagged == NOTICES[seller]
    priced = [a for a in lp.albums if parse_price(a.title)]
    assert priced and not any(a.notice for a in priced)


def test_listing_never_uses_inline_placeholder_as_photo():
    lp = parse_listing_page(page("yupoo_aristide_listing.html"), "https://aristide.x.yupoo.com/albums?page=1")
    assert len(lp.albums) == 68
    for a in lp.albums:
        for u in (a.cover_url, a.cover_thumb):
            assert u is None or u.startswith("https://")


class _OnePage:
    def __init__(self, html: str, url: str):
        self.r = SimpleNamespace(text=html, url=url, status_code=200, headers={})

    def get(self, url, **kw):
        return self.r


def test_password_protected_catalogue_is_blocked_not_empty():
    url = "https://ethanhomme.x.yupoo.com/albums?page=1"
    ad = YupooAdapter(_OnePage(page("yupoo_ethanhomme_listing.html"), url))
    with pytest.raises(BlockedError, match="password"):
        list(ad.iter_album_refs(url, max_pages=5))
    assert ad.exhausted is False


def test_empty_first_page_is_not_a_complete_catalogue():
    url = "https://someone.x.yupoo.com/albums?page=1"
    html = "<html><body><div class='album__list'>No albums yet</div></body></html>"
    ad = YupooAdapter(_OnePage(html, url))
    assert list(ad.iter_album_refs(url, max_pages=5)) == []
    assert ad.exhausted is False  # so no product is ever retired on the strength of an empty page


# --------------------------------------------------------------- whole scans
def _scanned_catalogue(db, web, n=4):
    from app.ingestion.scan import scan_seller
    from tests.test_scanner import _catalogue, _seller, serve

    albums, images = _catalogue(n)
    serve(web, albums, images)
    seller = _seller(db)
    with web.client() as c:
        scan_seller(db, seller, client=c)
    return seller, albums, images


@pytest.mark.parametrize(
    "listing, status",
    [(page("yupoo_ethanhomme_listing.html"), "blocked"),
     ("<html><body><div class='album__list'>No albums yet</div></body></html>", "empty")],
)  # fmt: skip
def test_unreadable_catalogue_retires_nothing(db, web, listing, status):
    from app.ingestion.scan import scan_seller
    from app.models import Product
    from tests.test_scanner import BASE

    seller, *_ = _scanned_catalogue(db, web)
    web.add(f"{BASE}/albums?page=1", listing)
    before = len(web.requests)
    with web.client() as c:
        run = scan_seller(db, seller, mode="full", client=c)
    assert run.status == status and not run.discovery_complete
    assert {p.status for p in db.query(Product).filter_by(seller_id=seller.id)} == {"active"}
    assert not [r for r in web.requests[before:] if "/albums/" in str(r.url)]  # no removal checks either


def test_notice_albums_do_not_become_products(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Product
    from tests.test_scanner import _album, serve

    seller, albums, images = _scanned_catalogue(db, web)
    albums[0]["title"] = "Contact Us"  # an album already stored as a product turns out to be a notice
    albums.insert(0, _album(9001, "New whatsapp", [albums[1]["images"][0]["key"]]))
    serve(web, albums, images)
    with web.client() as c:
        run = scan_seller(db, seller, client=c)
    prods = {p.external_id: p for p in db.query(Product).filter_by(seller_id=seller.id)}
    assert "9001" not in prods
    assert prods[albums[1]["id"]].status == "notice"
    assert sum(p.status == "active" for p in prods.values()) == len(albums) - 2
    assert run.stats["notices"] == 2
