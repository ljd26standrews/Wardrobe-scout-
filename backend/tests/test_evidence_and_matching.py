"""Quality evidence independence, comparison grouping and price verdicts, and model cache/history fixes."""

from __future__ import annotations

from app.models import Evidence
from app.services.quality import score_dimension


def _ev(**kw):
    return Evidence(**{"aspect": "construction", "claim": "x", "strength": 1.0, **kw})


def test_a_reposted_review_counts_once():
    one = score_dimension([_ev(kind="buyer_report", polarity=1, source_url="https://r.example/1")])
    five = score_dimension(
        [_ev(kind="buyer_report", polarity=1, source_url="https://www.R.example/1/") for _ in range(5)]
    )
    assert five["confidence"] == one["confidence"] and five["label"] == one["label"]
    two = score_dimension([_ev(kind="buyer_report", polarity=1, source_url=f"https://r.example/{i}") for i in (1, 2)])
    assert two["confidence"] > one["confidence"]  # genuinely separate reports still add up


def test_marketing_cannot_be_unlocked_by_a_token_independent_report():
    claims = [_ev(kind="seller_claim", polarity=1, source_url=f"https://seller/{i}") for i in range(20)]
    r = score_dimension([_ev(kind="buyer_report", polarity=-1, strength=0.01), *claims])
    assert r["score"] is None and r["label"] == "Unverified"
    # Claims never move a score: not against a real negative report, nor from Good to Strong.
    for polarity in (-1, 1):
        report = _ev(kind="buyer_report", polarity=polarity)
        assert score_dimension([report, *claims])["score"] == score_dimension([report])["score"]


# ------------------------------------------------------------------ comparisons
def _seller(db, slug="s1"):
    from app.models import Seller

    s = Seller(name=slug, slug=slug, platform="manual")
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


def _relate(db, x, y, relation="shared_photos", score=0.9):
    from app.models import ProductRelation

    db.add(ProductRelation(a_id=min(x.id, y.id), b_id=max(x.id, y.id), relation=relation, score=score))


def test_listings_you_separated_never_share_a_group(env, db):
    from app.services import dedup

    s = _seller(db)
    a, b, c, d = (_product(db, s, x) for x in ("ga", "gb", "gc", "gd"))
    _relate(db, a, b, score=0.95)
    _relate(db, b, c, score=0.7)  # the weaker link of the chain a - b - c
    _relate(db, c, d, score=0.9)
    db.commit()
    dedup.set_user_decision(db, a.id, c.id, False)
    for p in (a, b, c, d):
        db.refresh(p)
    assert a.group_id == b.group_id and c.group_id == d.group_id
    assert a.group_id != c.group_id
    dedup.set_user_decision(db, a.id, c.id, True)  # changing your mind joins them again
    for p in (a, c):
        db.refresh(p)
    assert a.group_id == c.group_id


def _index_photos(db, web, prods_urls):
    from app.ingestion.pipeline import process_images

    with web.client() as c:
        for p in prods_urls:
            process_images(db, p, c)
    db.commit()


def test_one_photo_listed_at_two_sizes_is_one_photo(env, db, web):
    from app.services import dedup
    from tests.conftest import make_image

    data = make_image((10, 80, 40), "tee", seed=3)
    prods = []
    for slug, ext in (("m1", "e"), ("m2", "f")):
        urls = [f"https://img.example/{ext}/big.jpg", f"https://img.example/{ext}/medium.jpg"]
        for u in urls:
            web.add(u, data, ctype="image/jpeg")
        prods.append(_product(db, _seller(db, slug), ext, image_urls=urls))
    _index_photos(db, web, prods)
    dedup.match_product(db, prods[0])
    db.commit()
    rels = {r.relation for r, _ in dedup.relations_for(db, prods[0].id)}
    assert rels == {"similar_photo"}  # still a hint, but not "shared photos"


def test_photo_later_recognised_as_boilerplate_stops_matching(env, db, web):
    from app.services import dedup
    from tests.conftest import make_image

    data = [make_image((90, 20, 20), "tee", seed=7), make_image((20, 90, 20), "hoodie", seed=8)]
    prods = []
    for slug, ext in (("n1", "g"), ("n2", "h")):
        urls = [f"https://img.example/{ext}{i}.jpg" for i in range(2)]
        for u, d in zip(urls, data, strict=True):
            web.add(u, d, ctype="image/jpeg")
        prods.append(_product(db, _seller(db, slug), ext, image_urls=urls))
    _index_photos(db, web, prods)
    ctx = dedup.MatchContext(db)  # built while the photos were still eligible
    for img in prods[0].images:
        img.role = "repeated"
    db.commit()
    ctx.match(prods[1])
    db.commit()
    rels = {r.relation for r, _ in dedup.relations_for(db, prods[1].id)}
    assert not rels & {"shared_photos", "similar_photo"}, rels


def _row(price, cur, status="active", relation="same_purchase_link"):
    return {"relation": relation, "user_confirmed": None,
            "product": {"price": price, "currency": cur, "status": status, "seller": {"name": "B"},
                        "quality": {"quality_score": None}}}  # fmt: skip


def test_price_verdict_compares_like_for_like_only():
    from app.services.presenter import price_verdict

    base = {"price": 500, "currency": "CNY", "quality": {"quality_score": None}}
    for rows in ([_row(100, "GBP")], [_row(100, "")], [_row(100, "CNY", status="unavailable")]):
        v = price_verdict(base, rows)
        assert v["recommendation"] == "insufficient_data", (rows, v)
        assert "not compared" in v["notes"][0]
    assert price_verdict(base, [_row(100, "RMB")])["recommendation"] == "cheaper_equivalent_available"
    unknown = {**base, "currency": ""}
    assert price_verdict(unknown, [_row(100, "")])["recommendation"] == "insufficient_data"
