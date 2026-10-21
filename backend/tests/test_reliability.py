"""Regressions for the reliability follow-up (audit of f1e9ecb): desired behaviour, not defect probes."""

from __future__ import annotations

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
