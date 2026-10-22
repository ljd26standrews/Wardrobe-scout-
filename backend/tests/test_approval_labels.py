"""What counts as an approval label, and what a rejection reason may teach (selective recommendation, M1)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np


def _seller(db, slug="al"):
    from app.models import Seller

    s = Seller(name=slug, slug=slug, platform="manual")
    db.add(s)
    db.commit()
    return s


def _product(db, seller, ext, title="navy wool coat"):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing

    p = upsert_listing(db, seller, RawListing(ext, f"https://shop.example/{ext}", title)).product
    db.commit()
    return p


def _fb(db, p, action, reasons=(), minutes=0, **context):
    from app.models import Feedback, utcnow
    from app.services.recommender import snapshot

    fb = Feedback(product_id=p.id, action=action, reasons=list(reasons), context=context, snapshot=snapshot(p),
                  created_at=utcnow() + timedelta(minutes=minutes))  # fmt: skip
    db.add(fb)
    db.commit()
    return fb


def test_only_explicit_decisions_become_labels(env, db):
    from app.models import FeedRequest, Impression
    from app.services.approval_data import rated_outcomes

    s = _seller(db)
    ps = {k: _product(db, s, k) for k in ("love", "like", "bought", "dislike", "maybe", "skip", "unsaved",
                                          "undone", "shown", "never", "seed")}  # fmt: skip
    _fb(db, ps["love"], "love")
    _fb(db, ps["like"], "like", feed_id="f1", position=3)
    _fb(db, ps["bought"], "purchased")
    _fb(db, ps["dislike"], "dislike", ["too_expensive"])  # any rejection is "not approved"
    _fb(db, ps["maybe"], "maybe")
    _fb(db, ps["skip"], "skip")  # displayed, not rated: no label
    _fb(db, ps["unsaved"], "like")
    _fb(db, ps["unsaved"], "unsave", minutes=1)  # withdrawn: no label
    db.delete(_fb(db, ps["undone"], "love"))  # undone: the row is gone
    db.add(FeedRequest(id="f1", engine="bayes", engine_version="x", items=[]))
    db.add(Impression(feed_id="f1", product_id=ps["shown"].id, position=0, surface="card"))  # shown, unrated
    _fb(db, ps["seed"], "love", seed=True)  # seed examples are notes, not rated recommendations
    db.commit()

    # The earlier reading ("approval-v1"), kept unchanged for comparison; tests/test_taste_contract.py has the
    # current one, in which a purchase, a maybe and a price-only rejection say nothing about the look.
    labels = {o.product_id: o.y for o in rated_outcomes(db, version="approval-v1")}
    assert labels == {ps["love"].id: 1, ps["like"].id: 1, ps["bought"].id: 1, ps["dislike"].id: 0, ps["maybe"].id: 0}
    assert {o.product_id: o.y for o in rated_outcomes(db)} == {ps["love"].id: 1, ps["like"].id: 1}
    like = next(o for o in rated_outcomes(db) if o.product_id == ps["like"].id)
    assert (like.feed_id, like.position) == ("f1", 3)


def test_one_label_per_garment_family_from_its_latest_decision(env, db):
    from app.services.approval_data import rated_outcomes

    s1, s2 = _seller(db, "x1"), _seller(db, "x2")
    a, b = _product(db, s1, "a"), _product(db, s2, "b")
    a.group_id = b.group_id = a.id
    db.commit()
    _fb(db, a, "love")
    _fb(db, b, "dislike", ["colour"], minutes=5)
    outcomes = rated_outcomes(db)
    assert len(outcomes) == 1 and outcomes[0].y == 0 and outcomes[0].product_id == b.id


def test_a_deleted_product_keeps_its_label_from_the_snapshot(env, db):
    from app.models import Feedback
    from app.services.approval_data import rated_outcomes

    p = _product(db, _seller(db), "gone")
    _fb(db, p, "like")
    db.query(Feedback).update({Feedback.product_id: None})
    db.delete(p)
    db.commit()
    (o,) = rated_outcomes(db)
    assert o.product_id is None and o.y == 1 and o.family.startswith("d")


# ----------------------------------------------------------------- what a reason may teach
def _space(db):
    from app.services import embeddings as emb
    from app.services.preference_model import build_feature_space
    from app.services.settings_store import get_setting

    return build_feature_space(db, get_setting(db, "taste_profile"), emb.get_image_backend().name)


def _weights(db):
    from app.services import preference_model as pm

    space = _space(db)
    post = pm.fit(db, space)
    return space, post


def test_a_logo_rejection_does_not_reject_the_brand(env, db):
    s = _seller(db)
    loud = _product(db, s, "loud", "Burberry large logo print hoodie black")
    for i in range(6):
        _product(db, s, f"q{i}", f"Burberry plain wool coat {i}")
    _fb(db, loud, "dislike", ["logo"])
    space, post = _weights(db)
    brand = [j for (g, _), j in space.vocab.items() if g == "brand"]
    logo = [j for (g, _), j in space.vocab.items() if g == "logo"]
    assert brand and logo
    assert max(abs(post.mean[1 + j]) for j in brand) < 0.01  # the brand is untouched
    assert min(post.mean[1 + j] for j in logo) < -0.05  # the visible logo is what is learned


def test_new_reasons_teach_only_what_they_say(env, db):
    s = _seller(db)
    ps = [_product(db, s, f"r{i}", f"grey cotton t-shirt {i}") for i in range(4)]
    _fb(db, ps[0], "dislike", ["wrong_category"])  # a data error, not taste
    _fb(db, ps[1], "dislike", ["too_repetitive"])  # a feed problem, not taste
    space, post = _weights(db)
    assert post.n_obs == 0 and np.allclose(post.mean[1 : space.img_slice.start], 0, atol=1e-6)
    _fb(db, ps[2], "dislike", ["not_my_style"])  # a general taste rejection, like a dislike with no reason
    _, post = _weights(db)
    assert post.n_obs == 1
