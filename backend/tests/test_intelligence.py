"""Recommender, quality scoring and deduplication."""

from app.ingestion.base import RawListing
from app.ingestion.pipeline import process_images, upsert_listing
from app.models import Evidence, Feedback, Product, Seller
from app.services import dedup
from app.services.quality import assess, score_dimension
from app.services.recommender import (
    build_feed,
    build_profile,
    rank_products,
    route_feedback,
    snapshot,
)

from .conftest import make_image


def _seller(db, slug="s1"):
    s = Seller(name=slug.upper(), slug=slug, platform="manual")
    db.add(s)
    db.commit()
    return s


def _product(
    db,
    seller,
    ext,
    title,
    *,
    colour=(30, 30, 30),
    shape="hoodie",
    price=500.0,
    desc=None,
    img_seed=0,
    same_image_as: bytes | None = None,
    web=None,
):
    raw = RawListing(
        external_id=ext,
        source_url=f"https://example.com/{seller.slug}/{ext}",
        title=title,
        description=desc,
        price=price,
        image_urls=[f"https://img.example.com/{seller.slug}/{ext}.jpg"],
    )
    p = upsert_listing(db, seller, raw).product
    data = same_image_as or make_image(colour, shape, seed=img_seed)
    if web is not None:
        web.add(raw.image_urls[0], data, ctype="image/jpeg")
        with web.client() as c:
            process_images(db, p, c)
    db.commit()
    return p, data


def _fb(db, p, action, reasons=()):
    db.add(Feedback(product_id=p.id, action=action, reasons=list(reasons), snapshot=snapshot(p)))
    db.commit()


# ------------------------------------------------------------- reason routing
def test_value_rejection_does_not_teach_taste_dislike():
    r = route_feedback("dislike", ["too_expensive"])
    assert r.attrs == {} and r.visual == 0 and r.text == 0 and r.price_ceiling


def test_colour_rejection_only_touches_colour():
    r = route_feedback("dislike", ["colour"])
    assert set(r.attrs) == {"colour"} and r.visual == 0


def test_skip_teaches_nothing():
    r = route_feedback("skip", [])
    assert not r.attrs and not r.visual and not r.suppress


def test_profile_white_tee_value_rejection(db):
    s = _seller(db)
    tee, _ = _product(db, s, "t1", "Brunello Cucinelli 纯棉T恤 白色", shape="tee", price=510)
    _fb(db, tee, "dislike", ["too_expensive"])
    prof = build_profile(db)
    assert prof.affinity("colour:white") == 0  # no negative preference for white
    assert prof.affinity("category:t-shirt") == 0
    assert prof.price_ceiling["t-shirt"] == 510
    assert prof.n_informative == 0  # not counted as taste learning


def test_colour_reason_lowers_only_colour(db):
    s = _seller(db)
    p, _ = _product(db, s, "h1", "Acne 羊毛连帽卫衣 红色")
    _fb(db, p, "dislike", ["colour"])
    prof = build_profile(db)
    assert prof.affinity("colour:red") < 0
    assert prof.affinity("category:hoodie") == 0


def test_cold_start_then_learning(db, web):
    s = _seller(db)
    loved, _ = _product(db, s, "a", "羊绒连帽卫衣 黑色", colour=(20, 20, 20), web=web)
    similar, _ = _product(db, s, "b", "羊绒连帽卫衣 炭灰", colour=(40, 40, 42), web=web, img_seed=1)
    different, _ = _product(db, s, "c", "印花T恤 黄色 大logo", colour=(230, 200, 20), shape="tee", web=web, img_seed=2)
    prof0 = build_profile(db)
    assert prof0.strength == 0
    ranked0 = rank_products(db, [similar, different], prof0)
    assert "Cold start" in " ".join(ranked0[0].reasons)
    _fb(db, loved, "love")
    prof = build_profile(db)
    assert 0 < prof.strength < 0.2  # one decision is weak evidence, and we say so
    ranked = rank_products(db, [similar, different], prof)
    assert ranked[0].product.id == similar.id
    assert ranked[0].components["visual"] > ranked[1].components["visual"]
    assert any("Visually close" in r for r in ranked[0].reasons)


def test_near_duplicate_of_rejected_item_is_suppressed(db, web):
    s1, s2 = _seller(db, "s1"), _seller(db, "s2")
    p1, img = _product(db, s1, "x", "卫衣", web=web)
    p2, _ = _product(db, s2, "y", "卫衣", same_image_as=img, web=web)
    _fb(db, p1, "dislike", ["specific_item"])
    ranked = rank_products(db, [p2], build_profile(db))
    assert ranked[0].suppressed
    assert build_feed(ranked, build_profile(db), 10, 0.18) == []


def test_exploration_share(db, web):
    s = _seller(db)
    prods = []
    for i in range(30):
        cat = ["卫衣", "休闲裤", "T恤", "开衫", "大衣"][i % 5]
        p, _ = _product(db, s, f"p{i}", f"{cat} {i}", colour=(10 * i % 255, 40, 60), img_seed=i, web=web)
        prods.append(p)
    _fb(db, prods[0], "love")
    prof = build_profile(db)
    ranked = rank_products(db, prods[1:], prof)
    feed = build_feed(ranked, prof, 20, 0.18, seed=1)
    explore = [r for r in feed if r.explore]
    assert len(feed) == 20 and 3 <= len(explore) <= 4
    assert all(r.reasons[0].startswith("Exploration") for r in explore)


# -------------------------------------------------------------------- quality
def _ev(kind, aspect, polarity=0, strength=0.8, affiliate=False):
    return Evidence(kind=kind, aspect=aspect, claim="x", polarity=polarity, strength=strength, is_affiliate=affiliate)


def test_no_evidence_is_unverified(db):
    s = _seller(db)
    p, _ = _product(db, s, "q", "Plain tee ¥900", price=900)
    p.evidence = []
    q = assess(db, p)
    assert q["status"] == "Unverified" and q["quality_score"] is None
    assert q["dimensions"]["construction"]["label"] == "Unverified"


def test_seller_claims_alone_stay_unverified():
    d = score_dimension([_ev("seller_claim", "material", 1, 1.0)] * 5)
    assert d["score"] is None and d["label"] == "Seller claim only"


def test_price_never_raises_quality(db):
    s = _seller(db)
    cheap, _ = _product(db, s, "c1", "tee", price=100)
    pricey, _ = _product(db, s, "c2", "tee", price=2000)
    assert assess(db, cheap)["quality_score"] == assess(db, pricey)["quality_score"] is None


def test_buyer_reports_score_and_affiliate_discount():
    good = score_dimension(
        [_ev("buyer_report", "construction", 1), _ev("buyer_report", "stitching", 1), _ev("verified", "hardware", 1)]
    )
    assert good["score"] >= 75 and good["label"] == "Strong"
    affiliate = score_dimension([_ev("buyer_report", "construction", 1, affiliate=True)] * 2)
    assert affiliate["confidence"] < good["confidence"]
    bad = score_dimension(
        [_ev("buyer_report", "defect", -1), _ev("buyer_report", "stitching", -1), _ev("user_note", "construction", -1)]
    )
    assert bad["score"] < 40


# ---------------------------------------------------------------------- dedup
def test_shared_photos_across_sellers_grouped_but_not_confirmed(db, web):
    s1, s2, s3 = _seller(db, "s1"), _seller(db, "s2"), _seller(db, "s3")
    imgs = [make_image((30, 30, 30), "hoodie", seed=1), make_image((30, 30, 30), "tee", seed=2)]
    raw_a = RawListing(
        "a",
        "https://example.com/s1/a",
        "Margiela 双头拉链夹克",
        price=528,
        image_urls=["https://img.example.com/s1/a1.jpg", "https://img.example.com/s1/a2.jpg"],
    )
    raw_b = RawListing(
        "b",
        "https://example.com/s2/b",
        "MM 夹克",
        price=420,
        image_urls=["https://img.example.com/s2/b1.jpg", "https://img.example.com/s2/b2.jpg"],
    )
    a, b = upsert_listing(db, s1, raw_a).product, upsert_listing(db, s2, raw_b).product
    for raw in (raw_a, raw_b):
        for url, data in zip(raw.image_urls, imgs, strict=True):
            web.add(url, data, ctype="image/jpeg")
    with web.client() as c:
        process_images(db, a, c)
        process_images(db, b, c)
    c3, _ = _product(db, s3, "c", "trousers", shape="trousers", colour=(200, 190, 170), web=web, img_seed=7)
    dedup.match_product(db, b)
    dedup.recompute_groups(db)
    db.commit()
    rels = {other: r.relation for r, other in dedup.relations_for(db, b.id)}
    assert rels.get(a.id) == "shared_photos"
    assert c3.id not in rels
    db.refresh(a)
    db.refresh(b)
    assert a.group_id == b.group_id is not None
    rel = dedup.relations_for(db, b.id)[0][0]
    assert rel.user_confirmed is None  # suggestion, not a confirmed identity
    # User rejects the match -> never re-suggested
    dedup.set_user_decision(db, a.id, b.id, same=False)
    dedup.match_product(db, b)
    assert dedup.relations_for(db, b.id) == []


def test_one_photo_reused_twice_counts_once(db, web):
    s1, s2 = _seller(db, "s1"), _seller(db, "s2")
    a, img = _product(db, s1, "a", "jacket", web=web)
    raw_b = RawListing(
        "b",
        "https://example.com/s2/b",
        "jacket",
        price=420,
        image_urls=["https://img.example.com/s2/b1.jpg", "https://img.example.com/s2/b2.jpg"],
    )
    b = upsert_listing(db, s2, raw_b).product
    for u in raw_b.image_urls:
        web.add(u, img, ctype="image/jpeg")
    with web.client() as c:
        process_images(db, b, c)
    dedup.match_product(db, b)
    assert {r.relation for r, _ in dedup.relations_for(db, b.id)} == {"similar_photo"}


def test_same_purchase_link_detected(db):
    s1, s2 = _seller(db, "s1"), _seller(db, "s2")
    u = "https://weidian.com/item.html?itemID=123"
    upsert_listing(db, s1, RawListing("a", "https://a.example/a", "tee", links={"weidian": u}))
    b = upsert_listing(db, s2, RawListing("b", "https://b.example/b", "tee", links={"weidian": u + "&x=1"})).product
    dedup.match_product(db, b)
    assert dedup.relations_for(db, b.id)[0][0].relation == "same_purchase_link"
    assert db.query(Product).count() == 2


def test_same_silhouette_different_colour_is_not_shared_photos(db, web):
    s1, s2 = _seller(db, "s1"), _seller(db, "s2")
    black, _ = _product(db, s1, "a", "hoodie black", colour=(20, 20, 20), web=web)
    red, _ = _product(db, s2, "b", "hoodie red", colour=(180, 30, 30), web=web)
    dedup.match_product(db, red)
    assert all(r.relation != "shared_photos" for r, _ in dedup.relations_for(db, red.id))
