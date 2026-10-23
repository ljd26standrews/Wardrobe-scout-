"""Remembered feed choices (api/products.py, "feed selections").

A feed's choice of pieces is remembered while nothing it depends on has changed. The same request then gets the same
pieces under a new feed id, and its cards are built afresh. Any change that could alter the choice produces a new one,
and that new choice is exactly what a server that remembers nothing would make.

These tests use a SYNTHETIC catalogue (tests/test_calibration_lifecycle.catalogue) and synthetic decisions. Nothing
here measures taste.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from . import test_calibration_lifecycle as lifecycle
from .test_calibration_lifecycle import catalogue, decide, rate

client = lifecycle.client
held = lifecycle.held


def answer(c, path: str = "/api/feed", **params) -> dict:
    r = c.get(path, params={"limit": 30, **params})
    assert r.status_code == 200, r.text
    return r.json()


def from_scratch(c, path: str = "/api/feed", **params) -> dict:
    """The answer of a server that remembers nothing. A new instance number makes every cache keyed by the data miss
    (services/revisions.py), as after a restore."""
    from sqlalchemy import text

    from app.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("UPDATE data_revisions SET n = n + 1 WHERE name = 'instance'"))
    return answer(c, path, **params)


def body(ans: dict) -> dict:
    return {k: v for k, v in ans.items() if k != "feed_id"}


def choice(ans: dict) -> list:
    """What was chosen and why: the pieces in order, with their scores, confidence and other offers."""
    return [(i["id"], i["recommendation"], i.get("confidence"), i["family"]) for i in ans["items"]] + [ans["gate"]]


# ----------------------------------------------------------------- the same request, nothing changed
@pytest.mark.parametrize("path", ["/api/feed", "/api/feed/learning"])
def test_the_same_request_gets_the_same_choice_under_a_new_feed_id(client, db, path):
    from app.db import SessionLocal
    from app.models import FeedRequest

    ids = catalogue(db)
    rate(client, ids[:6], approvals=3)
    first, second = answer(client, path), answer(client, path)
    assert first["items"] and body(second) == body(first)
    assert first["feed_id"] != second["feed_id"]
    with SessionLocal() as s:  # both are logged, each with what it served
        logged = [s.get(FeedRequest, a["feed_id"]) for a in (first, second)]
        assert all(fr is not None for fr in logged)
        assert logged[0].items == logged[1].items
        assert [it["product_id"] for it in logged[1].items] == [i["id"] for i in second["items"]]
    # What happens on the second answer is attributed to it
    shown = [{"product_id": i["id"]} for i in second["items"][:2]]
    r = client.post(f"/api/feed/{second['feed_id']}/viewed", json={"surface": "grid", "items": shown})
    assert r.json() == {"logged": 2}
    r = client.post("/api/feedback", json={"product_id": second["items"][0]["id"], "action": "like",
                                           "context": {"feed_id": second["feed_id"], "position": 0}})  # fmt: skip
    assert r.status_code == 200


def test_cards_are_built_afresh_while_the_choice_is_remembered(client, db):
    from app.db import SessionLocal
    from app.models import Product, ProductImage

    ids = catalogue(db)
    rate(client, ids[:6], approvals=3)
    before = answer(client)
    pid = before["items"][0]["id"]
    assert before["items"][0]["is_new"] is True
    client.get(f"/api/products/{pid}")  # seen
    seen_at = dt.datetime(2026, 1, 2, 3, 4, 5)
    with SessionLocal() as s:  # a scan saw the listing again, and fetched a photo
        p = s.get(Product, before["items"][2]["id"])
        p.last_seen_at = seen_at
        s.add(ProductImage(product_id=p.id, url="https://shop.example/photo.jpg", position=0))
        s.commit()
    after = answer(client)
    cards = {i["id"]: i for i in after["items"]}
    assert [i["id"] for i in after["items"]] == [i["id"] for i in before["items"]]
    assert cards[pid]["is_new"] is False
    assert cards[before["items"][2]["id"]]["last_seen_at"] == seen_at.isoformat()
    assert [i["remote_url"] for i in cards[before["items"][2]["id"]]["images"]] == ["https://shop.example/photo.jpg"]
    assert body(after) == body(from_scratch(client))


# ----------------------------------------------------------------- every change the choice depends on
def _decision(c, db, ids, top):
    decide(c, top, "like")


def _undo(c, db, ids, top):
    """An Undo of a decision made before the first answer."""
    from app.models import Feedback

    fid = db.query(Feedback.id).filter(Feedback.product_id == ids[0]).scalar()
    assert c.delete(f"/api/feedback/{fid}").status_code == 200


def _product_edit(c, db, ids, top):
    assert c.patch(f"/api/products/{top}", json={"price": 20000}).status_code == 200


def _evidence(c, db, ids, top):
    ev = {"kind": "buyer_report", "aspect": "construction", "claim": "Seams split after a week", "polarity": -1,
          "strength": 1.0, "source_url": "https://forum.example/t/1"}  # fmt: skip
    assert c.post(f"/api/products/{top}/evidence", json=ev).status_code == 200


def _match(c, db, ids, top):
    other = next(i for i in ids[6:] if i != top)
    assert c.post(f"/api/products/{top}/match", json={"other_id": other, "same": True}).status_code == 200


def _hidden_category(c, db, ids, top):
    cur = c.get("/api/settings").json()["discover"]
    assert c.put("/api/settings/discover", json={**cur, "hidden_categories": ["hoodie"]}).status_code == 200


def _currency_rate(c, db, ids, top):
    cur = c.get("/api/settings").json()["currency"]
    assert c.put("/api/settings/currency", json={**cur, "cny_to_gbp": cur["cny_to_gbp"] * 5}).status_code == 200


def _wardrobe(c, db, ids, top):
    r = c.post("/api/wardrobe", data={"name": "Navy wool overcoat", "category": "overcoat", "colours": "navy",
                                      "rating": "5"})  # fmt: skip
    assert r.status_code == 200, r.text


def _inspiration(c, db, ids, top):
    from app.db import SessionLocal
    from app.models import InspirationImage
    from app.services import embeddings as emb

    vec = emb.normalise(np.random.default_rng(3).standard_normal(192).astype(np.float32))  # like the catalogue's
    with SessionLocal() as s:
        s.add(InspirationImage(path="uploads/i.png", polarity=1, embedding=emb.to_bytes(vec),
                               embedding_backend=emb.get_image_backend().name))  # fmt: skip
        s.commit()


def _seller(c, db, ids, top):
    sid = c.get(f"/api/products/{top}").json()["seller"]["id"]
    assert c.patch(f"/api/sellers/{sid}", json={"reputation_status": "concerns"}).status_code == 200


def _import(c, db, ids, top):
    r = c.post("/api/import/manual", data={"title": "Navy wool overcoat, new arrival", "price": "300",
                                           "category": "overcoat", "colours": "navy"})  # fmt: skip
    assert r.status_code == 200, r.text


def _reanalysis(c, db, ids, top):
    from app.db import SessionLocal
    from app.models import Product
    from app.services import embeddings as emb

    rng = np.random.default_rng(7)
    with SessionLocal() as s:  # new photo vectors, as a re-analysis writes them
        for p in s.query(Product).filter(Product.id.in_(ids[6:30])):
            p.image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
        s.commit()


def _removed(c, db, ids, top):
    from app.db import SessionLocal
    from app.models import Product

    with SessionLocal() as s:  # a scan found the listing gone
        s.get(Product, top).status = "removed"
        s.commit()


def _backup_import(c, db, ids, top):
    backup = c.get("/api/backup/export", params={"include_images": False}).content
    decide(c, top, "dislike")  # changed since the backup, then the backup is put back
    r = c.post("/api/backup/import", files={"file": ("b.json", backup, "application/json")})
    assert r.status_code == 200, r.text


CHANGES = {
    "decision": _decision,
    "undo": _undo,
    "product edit": _product_edit,
    "evidence": _evidence,
    "match": _match,
    "hidden category": _hidden_category,
    "currency rate": _currency_rate,
    "wardrobe": _wardrobe,
    "inspiration": _inspiration,
    "seller reputation": _seller,
    "import": _import,
    "reanalysis": _reanalysis,
    "listing removed": _removed,
    "backup import": _backup_import,
}


# Requests under which the change can be seen: every catalogue piece costs the same, so a rate matters to a filter
REQUESTS = {"currency rate": {"max_price": 40, "price_currency": "GBP"}}


@pytest.mark.parametrize("change", list(CHANGES))
def test_a_change_makes_a_new_choice_identical_to_one_made_from_scratch(client, db, change):
    ids = catalogue(db)
    rate(client, ids[:6], approvals=3)
    request = REQUESTS.get(change, {})
    before = answer(client, **request)
    assert before["items"] and body(answer(client, **request)) == body(before)  # remembered
    CHANGES[change](client, db, ids, before["items"][0]["id"])
    after = answer(client, **request)
    assert choice(after) != choice(before), "the change should alter what the feed chooses"
    assert body(after) == body(from_scratch(client, **request))


def test_learning_picks_follow_a_decision(client, db):
    ids = catalogue(db)
    rate(client, ids[:6], approvals=3)
    before = answer(client, "/api/feed/learning")
    decide(client, before["items"][0]["id"], "dislike")
    after = answer(client, "/api/feed/learning")
    assert before["items"][0]["id"] not in {i["id"] for i in after["items"]}
    assert body(after) == body(from_scratch(client, "/api/feed/learning"))


# ----------------------------------------------------------------- what changes with no data written
def test_a_skipped_piece_comes_back_when_its_time_is_up(client, db, monkeypatch):
    from app.api import products
    from app.models import utcnow
    from app.services import recommender

    ids = catalogue(db)
    rate(client, ids[:6], approvals=3)
    skipped = answer(client)["items"][0]["id"]
    decide(client, skipped, "skip")
    assert skipped not in {i["id"] for i in answer(client)["items"]}
    later = utcnow() + dt.timedelta(days=11)  # skipped pieces come back after skip_resurface_days (10)
    monkeypatch.setattr(products, "utcnow", lambda: later)
    monkeypatch.setattr(recommender, "utcnow", lambda: later)
    assert skipped in {i["id"] for i in answer(client)["items"]}


def test_a_refit_that_finishes_or_fails_is_reported_at_once(held, db, monkeypatch):
    from app.db import SessionLocal
    from app.models import Job
    from app.services import calibration

    ids = catalogue(db)
    rate(held, ids[:30], approvals=15)
    assert answer(held)["gate"]["state"] == "pending"  # a refit is queued
    assert answer(held)["gate"]["state"] == "pending"

    def broken(*a, **k):
        raise RuntimeError("numerical failure")

    with monkeypatch.context() as m:
        m.setattr(calibration, "fit_map", broken)
        held.run()  # the refit fails: no calibration is written
    assert answer(held)["gate"]["state"] == "failed"
    with SessionLocal() as s:  # the back-off has passed
        for job in s.query(Job).filter(Job.kind == "recalibrate"):
            job.finished_at = job.finished_at - calibration.RETRY_AFTER_FAILURE - dt.timedelta(minutes=1)
        s.commit()
    held.queue.clear()
    assert answer(held)["gate"]["state"] == "pending"
    assert [k for k, _ in held.queue] == ["recalibrate"]  # and the refit is queued again
    held.run()
    after = answer(held)
    assert after["gate"]["state"] == "calibrated"
    assert body(after) == body(from_scratch(held))


# ----------------------------------------------------------------- shared listings stay untouched
def test_cards_never_touch_the_shared_listings_even_for_a_piece_deleted_meanwhile(client, db):
    from app.db import SessionLocal
    from app.models import Product
    from app.services.presenter import Context, load_images, product_card, rebind
    from app.services.recommender import Ranked, active_products

    ids = catalogue(db, n=6)
    shared = {p.id: p for p in active_products(db)}
    fields = ("price", "title_en", "colours", "materials", "sizes")

    def state(p):
        return {k: repr(getattr(p, k)) for k in fields}

    before = {pid: state(p) for pid, p in shared.items()}
    with SessionLocal() as s:  # deleted after the feed chose it, before its card is built
        s.delete(s.get(Product, ids[0]))
        s.commit()
    items = [Ranked(shared[i], 0.5, {}, {}, 0.5, [], [], None) for i in ids[:3]]
    with SessionLocal() as s:
        out, offers = rebind(s, items, {ids[1]: [shared[ids[4]]]})
        load_images(s, [r.product for r in out])
        cards = [product_card(Context(s), r.product, r) for r in out]
        assert [c["id"] for c in cards] == ids[:3] and cards[0]["images"] == []
        assert cards[0]["title"] == shared[ids[0]].title_en  # shown as it was when chosen
        assert all(isinstance(r.product, Product) for r in out) and isinstance(offers[ids[1]][0], Product)
    for pid, p in shared.items():  # the shared records are unchanged, and cannot be changed or given photos
        assert state(p) == before[pid]
        with pytest.raises(AttributeError):
            p.price = 1.0
        assert not hasattr(p, "images")


def test_remembered_answer_does_not_keep_the_catalogue_feature_matrix_alive(client, db):
    import gc
    import weakref

    from app.services import preference_model as pm

    ids = catalogue(db)
    rate(client, ids[:6], approvals=3)
    first = answer(client)
    assert first["items"]
    matrix_owner = weakref.ref(next(iter(pm._space_cache.values())))
    # Simulate model caches evicting an old catalogue while its feed answer is still remembered.
    pm._space_cache.clear()
    pm._fit_memo.clear()
    pm._post_cache.clear()
    gc.collect()
    assert matrix_owner() is None
    assert body(answer(client)) == body(first)
