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


# ------------------------------------------------------------------ taste model
def _space(db):
    from app.services import embeddings as emb
    from app.services.preference_model import build_feature_space
    from app.services.settings_store import get_setting

    return build_feature_space(db, get_setting(db, "taste_profile"), emb.get_image_backend().name)


def test_wardrobe_change_refreshes_the_model_vocabulary(env, db):
    from app.models import WardrobeItem

    _product(db, _seller(db), "w1", title="cotton tee ¥100")
    before = _space(db)
    db.add(WardrobeItem(name="overcoat", category="overcoat", rating=5))
    db.commit()
    after = _space(db)
    assert ("category", "overcoat") in after.vocab and after is not before


def test_decisions_about_deleted_products_still_teach(env, db):
    from app.models import Feedback
    from app.services import preference_model as pm
    from app.services.recommender import snapshot

    s = _seller(db)
    p = _product(db, s, "d1", title="olive wool overcoat ¥500")
    for i in range(5):
        _product(db, s, f"o{i}", title=f"navy cotton trousers {i} ¥300")
    db.add(Feedback(product_id=p.id, action="love", reasons=[], snapshot=snapshot(p)))
    db.commit()
    before = pm.fit(db, _space(db))
    db.query(Feedback).update({Feedback.product_id: None})  # what deleting the product does (SET NULL)
    db.delete(p)
    db.commit()
    space = _space(db)
    after = pm.fit(db, space)
    assert after.n_obs == before.n_obs == 1
    assert after.mean[1 + space.vocab[("category", "overcoat")]] > 0.05  # the overcoat preference survives


# ------------------------------------------------------------------ jobs
def test_jobs_that_return_an_error_are_not_reported_as_succeeded(env):
    from app.db import SessionLocal, create_all
    from app.models import Job
    from app.services import jobs

    create_all()
    with SessionLocal() as s:
        job = Job(kind="ai_assess", params={"product_id": 999999}, status="queued")
        s.add(job)
        s.commit()
        job_id = job.id
    jobs._run(job_id)  # the assessment returns {"error": "product not found"}
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        assert job.status == "failed" and job.message == "product not found"

    assert jobs.outcome("succeeded", {"found": 3, "new": 1, "errors": ["r/x: HTTP 429"]}) == "partial"
    assert jobs.outcome("succeeded", {"found": 0, "new": 0, "errors": ["HTTP 403"]}) == "failed"
    assert jobs.outcome("succeeded", {"checked": 4, "ok": 3, "failed": 1}) == "partial"
    assert jobs.outcome("succeeded", {"checked": 2, "ok": 0, "failed": 2}) == "failed"
    assert jobs.outcome("succeeded", {"ok": False, "blocked": True, "error": "blocked"}) == "failed"
    assert jobs.outcome("succeeded", {"checked": 0, "ok": 0, "failed": 0}) == "succeeded"
    assert jobs.outcome("cancelled", {"error": "x"}) == "cancelled"


def test_seller_service_never_makes_a_garment_look_verified(env, db):
    from app.services.quality import assess

    s = _seller(db, "svc")
    p = _product(db, s, "sv1")
    for i in range(3):  # strong, independent praise for the seller's service only
        db.add(Evidence(seller_id=s.id, kind="buyer_report", aspect="service", claim=f"fast shipping {i}",
                        polarity=1, strength=1.0, source_url=f"https://r.example/svc{i}"))  # fmt: skip
    db.commit()
    q = assess(db, p)
    assert q["dimensions"]["seller_reliability"]["score"] is not None
    assert q["quality_score"] is None and q["status"] == "Unverified"

    db.add(Evidence(product_id=p.id, kind="buyer_report", aspect="stitching", claim="loose seams", polarity=-1,
                    strength=1.0, source_url="https://r.example/qc"))  # fmt: skip
    db.commit()
    db.refresh(p)
    q = assess(db, p)
    assert q["quality_score"] == q["dimensions"]["construction"]["score"] < 50  # service praise doesn't dilute it
