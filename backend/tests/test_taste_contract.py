"""The feedback contract: what each decision says about your taste (the look), about buying (price, size, owning
something like it, timing) and about quality, kept apart in labels, the taste model, the gate and Saved.

The interpretation is versioned (approval_data.LABEL_VERSION) and the original events are never rewritten:
"approval-v1" stays available as the unchanged comparator.

SYNTHETIC listings and decisions, made up for these tests.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_approval_labels import _fb, _product, _seller


# ----------------------------------------------------------------- labels
def test_aesthetic_labels_follow_the_contract(env, db):
    from app.services.approval_data import rated_outcomes

    s = _seller(db)
    names = ("love", "like", "not_now", "plain_no", "colour", "pricey", "own", "size", "evidence", "construction",
             "miscat", "repetitive", "maybe", "bought", "skip", "like_then_pricey", "like_then_colour",
             "like_then_bought", "like_then_maybe", "like_then_unsave")  # fmt: skip
    ps = {k: _product(db, s, k) for k in names}
    _fb(db, ps["love"], "love")
    _fb(db, ps["like"], "like")
    _fb(db, ps["not_now"], "like", ["not_now"])  # "I like the look, but not now": a clear yes to the look
    _fb(db, ps["plain_no"], "dislike")
    _fb(db, ps["colour"], "dislike", ["colour"])
    for name, reason in (("pricey", "too_expensive"), ("own", "already_own"), ("size", "wrong_size"),
                         ("evidence", "quality_unclear"), ("construction", "construction"),
                         ("miscat", "wrong_category"), ("repetitive", "too_repetitive")):  # fmt: skip
        _fb(db, ps[name], "dislike", [reason])  # about buying, quality or the listing: not about the look
    _fb(db, ps["maybe"], "maybe")  # ambiguous
    _fb(db, ps["bought"], "purchased")  # a purchase alone is not proof of liking the look
    _fb(db, ps["skip"], "skip")
    for name, later, reasons in (("like_then_pricey", "dislike", ["too_expensive"]),
                                 ("like_then_colour", "dislike", ["colour"]),
                                 ("like_then_bought", "purchased", []), ("like_then_maybe", "maybe", []),
                                 ("like_then_unsave", "unsave", [])):  # fmt: skip
        _fb(db, ps[name], "like")
        _fb(db, ps[name], later, reasons, minutes=1)

    labels = {o.product_id: o.y for o in rated_outcomes(db)}
    expect = {"love": 1, "like": 1, "not_now": 1, "plain_no": 0, "colour": 0, "like_then_pricey": 1,
              "like_then_colour": 0, "like_then_bought": 1}  # fmt: skip
    assert labels == {ps[k].id: y for k, y in expect.items()}

    # The earlier reading, kept unchanged for comparison: every rejection and maybe is "not approved", a purchase is.
    v1 = {o.product_id: o.y for o in rated_outcomes(db, version="approval-v1")}
    assert v1[ps["pricey"].id] == 0 and v1[ps["maybe"].id] == 0 and v1[ps["bought"].id] == 1
    assert v1[ps["like_then_pricey"].id] == 0


def test_the_taste_model_reads_decisions_the_same_way(env, db):
    """A price objection after a like keeps the like as taste evidence; a purchase, a maybe or a non-aesthetic
    rejection on its own teaches the model nothing."""
    from app.services import preference_model as pm

    s = _seller(db)
    a, b, c, d = (_product(db, s, k, f"{t} wool overcoat") for k, t in
                  (("a", "navy"), ("b", "black"), ("c", "grey"), ("d", "cream")))  # fmt: skip
    _fb(db, a, "like")
    _fb(db, a, "dislike", ["too_expensive"], minutes=1)
    _fb(db, b, "purchased")
    _fb(db, c, "maybe")
    _fb(db, d, "dislike", ["wrong_size"])
    space = pm.build_feature_space(db, {}, "lite-v1")
    rows, ys, ws, n_real, fams = pm.observations_with_families(db, space)
    real = [(f, y, w) for f, y, w in zip(fams, ys, ws, strict=True) if f is not None]
    assert real == [(f"p{a.id}", 1, 1.0)]


def test_purchase_and_ownership_reasons_are_recorded_without_teaching_taste(env, db):
    from app.services.recommender import build_profile

    s = _seller(db)
    p = _product(db, s, "x", "navy wool overcoat")
    _fb(db, p, "dislike", ["wrong_size", "quality_unclear"])
    prof = build_profile(db)
    assert prof.n_informative == 0 and prof.reason_counts == {"wrong_size": 1, "quality_unclear": 1}


# ----------------------------------------------------------------- the API, Saved and Undo
@pytest.fixture()
def client(env, monkeypatch):
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: 0)
    monkeypatch.setenv("WS_DEEP_ON_FEEDBACK", "false")
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def test_like_not_now_is_one_tap_saved_for_later_and_undoable(client, db):
    s = _seller(db)
    p = _product(db, s, "later", "navy wool overcoat")
    r = client.post("/api/feedback", json={"product_id": p.id, "action": "like", "reasons": ["not_now"]})
    assert r.status_code == 200 and r.json()["saved"] == "later"
    saved = client.get("/api/saved").json()
    assert [i["saved"]["list"] for i in saved["items"] if i["id"] == p.id] == ["later"]
    assert client.delete(f"/api/feedback/{r.json()['feedback_id']}").status_code == 200
    assert all(i["id"] != p.id for i in client.get("/api/saved").json()["items"])


def test_reasons_must_fit_the_decision(client, db):
    s = _seller(db)
    p = _product(db, s, "fit", "navy wool overcoat")
    bad = [("like", ["colour"]), ("dislike", ["not_now"]), ("love", ["too_expensive"])]
    for action, reasons in bad:
        r = client.post("/api/feedback", json={"product_id": p.id, "action": action, "reasons": reasons})
        assert r.status_code == 422, (action, reasons)


def test_saving_always_means_you_like_the_look(client, db):
    """Adding a piece to a list records a like; moving it between lists or adding notes records nothing more.
    A saved entry from an earlier version without any decision is left as a list entry, not turned into a label."""
    from app.models import Feedback, SavedItem
    from app.services.approval_data import rated_outcomes

    s = _seller(db)
    new, legacy = _product(db, s, "new", "navy wool overcoat"), _product(db, s, "legacy", "black wool overcoat")
    db.add(SavedItem(product_id=legacy.id, list_name="wishlist"))
    db.commit()
    assert client.patch(f"/api/saved/{new.id}", json={"list_name": "wishlist"}).status_code == 200
    assert (
        client.patch(f"/api/saved/{new.id}", json={"list_name": "liked", "note": "check sleeve length"}).status_code
        == 200
    )
    assert client.patch(f"/api/saved/{legacy.id}", json={"note": "older entry"}).status_code == 200
    mine = Feedback.product_id.in_([new.id, legacy.id])  # (the app's own seed examples are left aside)
    events = [(f.product_id, f.action, (f.context or {}).get("surface")) for f in db.query(Feedback).filter(mine)]
    assert events == [(new.id, "like", "save")]
    labels = {o.product_id: o.y for o in rated_outcomes(db)}
    assert labels.get(new.id) == 1 and legacy.id not in labels


# ----------------------------------------------------------------- ownership is scoped, category exclusions explicit
def _world(db, n_each=4):
    """Navy and grey overcoats and navy trousers, with photo vectors."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller
    from app.services import embeddings as emb

    rng = np.random.default_rng(0)
    s = Seller(name="own", slug="own", platform="manual")
    db.add(s)
    db.flush()
    out = {}
    for kind in ("navy wool overcoat", "grey wool overcoat", "navy wool trousers"):
        for i in range(n_each):
            p = upsert_listing(
                db, s, RawListing(f"{kind}{i}", f"https://own.example/{kind}/{i}", f"{kind} {i}")
            ).product
            p.image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
            p.embedding_backend = emb.get_image_backend().name
            out.setdefault(kind, []).append(p)
    db.commit()
    return out


def test_already_own_applies_to_similar_pieces_not_the_whole_category(env, db):
    from app.models import Feedback
    from app.services import selective
    from app.services.presenter import Context
    from app.services.recommender import build_profile, collapse_families, feed_candidates, rank_products

    w = _world(db)
    for p in w["navy wool overcoat"][:2]:  # said twice: the old rule banned every overcoat in the strict modes
        db.add(Feedback(product_id=p.id, action="dislike", reasons=["already_own"]))
    db.commit()
    prof = build_profile(db)
    ctx = Context(db)
    cands = feed_candidates(db, prof)
    ranked, _ = collapse_families(rank_products(db, cands, prof))
    gate = selective.evaluate(db, ranked, prof, "high", quality_lookup={p.id: ctx.quality(p) for p in cands})
    why = {c.product.id: c.excluded["high"] for c in gate.candidates}
    for p in w["navy wool overcoat"][2:]:
        assert why[p.id] == "already_own"  # like the pieces you said you own
    for p in w["grey wool overcoat"] + w["navy wool trousers"]:
        assert why[p.id] != "already_own"  # another colour, or another kind of garment
    by_id = {r.product.id: r for r in ranked}
    assert any("already own" in c for c in by_id[w["navy wool overcoat"][2].id].cautions)
    assert not any("already own" in c for c in by_id[w["grey wool overcoat"][0].id].cautions)


def test_a_category_is_hidden_only_when_you_say_so_and_can_be_shown_again(env, db, monkeypatch):
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: 0)
    from app.main import create_app

    w = _world(db)
    coats = {p.id for k in ("navy wool overcoat", "grey wool overcoat") for p in w[k]}
    with TestClient(create_app(), headers=AUTH) as c:
        assert coats & {i["id"] for i in c.get("/api/feed", params={"limit": 50}).json()["items"]}
        cfg = c.get("/api/settings").json()["discover"]
        assert cfg == {"hidden_categories": []}
        assert c.put("/api/settings/discover", json={"hidden_categories": ["overcoat"]}).status_code == 200
        feed = c.get("/api/feed", params={"limit": 50}).json()
        assert not coats & {i["id"] for i in feed["items"]}
        assert feed["hidden"] == {"categories": ["overcoat"], "pieces": len(coats)}  # said where it is decided
        learning = c.get("/api/feed/learning", params={"limit": 50}).json()
        assert not coats & {i["id"] for i in learning["items"]}
        listed = {i["id"] for i in c.get("/api/products", params={"page_size": 100}).json()["items"]}
        assert coats <= listed  # Browse all still shows everything
        assert c.put("/api/settings/discover", json={"hidden_categories": []}).status_code == 200
        assert coats & {i["id"] for i in c.get("/api/feed", params={"limit": 50}).json()["items"]}
        assert c.put("/api/settings/discover", json={"hidden_categories": "overcoat"}).status_code == 422


# ----------------------------------------------------------------- families and novelty
def test_listings_of_one_garment_are_one_piece_of_evidence(env, db):
    from app.services import preference_model as pm
    from app.services.approval_data import rated_outcomes

    s1, s2, s3 = _seller(db, "f1"), _seller(db, "f2"), _seller(db, "f3")
    ps = [_product(db, s, f"fam{i}", "navy wool overcoat") for i, s in enumerate((s1, s2, s3))]
    for p in ps:
        p.group_id = ps[0].id
    db.commit()
    for i, p in enumerate(ps):
        _fb(db, p, "like", minutes=i)
    assert len(rated_outcomes(db)) == 1
    space = pm.build_feature_space(db, {}, "lite-v1")
    _, ys, ws, n_real, fams = pm.observations_with_families(db, space)
    assert n_real == 1 and sum(w for w, f in zip(ws, fams, strict=True) if f is not None) == pytest.approx(1.0)


def test_a_kind_of_piece_you_have_never_rated_is_not_treated_as_disliked(env, db):
    """Novelty is not a negative: decisions about other kinds of garment barely move a never-rated kind (the weak
    "typical, not chosen" background is the only path), and never below what you rejected."""
    from app.services import preference_model as pm

    w = _world(db, n_each=6)
    hoodie = _product(db, _seller(db, "nov"), "hoodie", "red cotton hoodie")
    space0 = pm.build_feature_space(db, {}, "lite-v1")
    before = float(pm.fit(db, space0).mean_logit([hoodie.id])[0])
    for p in w["navy wool overcoat"][:4]:
        _fb(db, p, "like")
    for p in w["navy wool trousers"][:4]:
        _fb(db, p, "dislike", ["silhouette"])
    space = pm.build_feature_space(db, {}, "lite-v1")
    post = pm.fit(db, space)
    after = float(post.mean_logit([hoodie.id])[0])
    rejected = post.mean_logit([w["navy wool trousers"][0].id])[0]
    assert after > rejected and abs(after - before) < 0.5


def test_undo_still_restores_the_earlier_reading(client, db):
    from app.services.approval_data import rated_outcomes

    s = _seller(db)
    p = _product(db, s, "undo", "navy wool overcoat")
    client.post("/api/feedback", json={"product_id": p.id, "action": "like"})
    fid = client.post("/api/feedback", json={"product_id": p.id, "action": "dislike", "reasons": ["colour"]}).json()[
        "feedback_id"
    ]
    assert {o.product_id: o.y for o in rated_outcomes(db)} == {p.id: 0}
    client.delete(f"/api/feedback/{fid}")
    db.expire_all()
    assert {o.product_id: o.y for o in rated_outcomes(db)} == {p.id: 1}
    assert [i["saved"]["list"] for i in client.get("/api/saved").json()["items"] if i["id"] == p.id] == ["liked"]


def test_the_interpretation_change_retires_calibrations_fitted_under_the_old_reading(env, db):
    from app.models import Calibration
    from app.services import calibration

    cal = Calibration(engine_version="x", method=calibration.METHOD, n_labels=30, n_positive=15, label_hash="h",
                      active=True, params={"a": 1, "b": 0, "cov": [[0, 0], [0, 0]], "label_keys": [],
                                           "regime": {**calibration.regime(db), "labels": "approval-v1"}})  # fmt: skip
    db.add(cal)
    db.commit()
    assert calibration.invalid_reason(db, cal, calibration.evidence(db)) == "labels"
    _ = timedelta  # the module's helpers are shared with test_approval_labels
