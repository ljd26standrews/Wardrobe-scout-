"""Independent review probes, using disposable databases only."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_approval_labels import _product, _seller
from .test_calibration_lifecycle import catalogue
from .test_eval_selective import timed_world


@pytest.fixture()
def client(env, monkeypatch):
    from app.main import create_app
    from app.services import jobs
    monkeypatch.setattr(jobs, "enqueue", lambda *args, **kwargs: 0)
    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def test_review_undo_dislike_restores_saved_notes(client, db):
    p = _product(db, _seller(db), "undo-notes")
    fields = {"list_name": "wishlist", "priority": 1, "note": "Measure sleeves", "target_size": "M"}
    assert client.patch(f"/api/saved/{p.id}", json=fields).status_code == 200
    fid = client.post("/api/feedback", json={"product_id": p.id, "action": "dislike"}).json()["feedback_id"]
    assert client.delete(f"/api/feedback/{fid}").status_code == 200
    saved = next(i["saved"] for i in client.get("/api/saved").json()["items"] if i["id"] == p.id)
    assert (saved["list"], saved["priority"], saved["note"], saved["target_size"]) == ("wishlist", 1, "Measure sleeves", "M")


def test_review_undo_old_removal_respects_newer_dislike(client, db):
    # Adapted as the brief allows: a stale Undo may be refused (409) as long as the newer state is preserved.
    p = _product(db, _seller(db), "undo-old")
    client.patch(f"/api/saved/{p.id}", json={"list_name": "wishlist"})
    old = client.delete(f"/api/saved/{p.id}").json()["feedback_id"]
    client.post("/api/feedback", json={"product_id": p.id, "action": "dislike"})
    r = client.delete(f"/api/feedback/{old}")
    assert r.status_code in (200, 409)
    if r.status_code == 409:
        assert "decided about this piece again" in r.json()["detail"]
    assert p.id not in {i["id"] for i in client.get("/api/saved").json()["items"]}


def test_review_representative_excludes_families_after_matching(env, db):
    from app.models import Feedback, Judgement, Product
    from eval.selective import representative
    timed_world(db, per=20, rated=12, judged=8)
    j = db.query(Judgement).first()
    p = db.get(Product, j.product_id)
    prior = db.query(Feedback).first()
    other = db.get(Product, prior.product_id)
    p.group_id = other.group_id = 9999
    db.add(Feedback(product_id=p.id, action="love"))
    db.commit()
    result = representative(db)
    remaining = db.query(Feedback).filter(Feedback.product_id.in_([p.id, other.id])).count()
    print("matched evaluation:", {"removed": result["summary"]["training_decisions_removed"], "held_family_labels_remaining": remaining})
    assert remaining == 0


def test_review_family_holdout_excludes_linked_wardrobe_rating(env, db):
    from app.models import WardrobeItem
    from app.services import preference_model as pm
    p = _product(db, _seller(db), "wardrobe-leak")
    db.add(WardrobeItem(name="Rated held-out garment", product_id=p.id, rating=5, colours=["navy"], category="overcoat"))
    db.commit()
    space = pm.build_feature_space(db, {}, "lite-v1")
    _, _, _, n_real, _ = pm.observations_with_families(db, space, exclude=frozenset({f"p{p.id}"}))
    assert n_real == 0


def test_review_reason_change_refits_when_training_changes(env, db):
    from app.models import Feedback
    from app.services import calibration
    from app.services import preference_model as pm
    ids = catalogue(db)
    for i, pid in enumerate(ids[:36]):
        db.add(Feedback(product_id=pid, action="like" if i % 2 else "dislike", reasons=[]))
    db.commit()
    assert calibration.recalibrate(db)["status"] == "calibrated"
    old_space = calibration._space(db)
    old_post = pm.fit(db, old_space)
    fb = db.query(Feedback).filter_by(action="dislike").first()
    fb.reasons = ["colour"]
    db.commit()
    new_post = pm.fit(db, calibration._space(db))
    shift = float(np.max(np.abs(old_post.mean-new_post.mean)))
    result = calibration.recalibrate(db)
    print("reason change:", {"max_coefficient_change": shift, "recalibration": result["status"]})
    assert shift > 0.0001
    assert result["status"] != "unchanged"


def test_review_frozen_judgement_ids_do_not_reuse_replacement_grades(client, db, tmp_path):
    # Adapted as the brief asks: the contract is that evaluation consumes the frozen records, not that ids are never
    # reused (SQLite still reuses the id below; the benchmark is unaffected).
    import json

    from app.models import Feedback, Judgement, Product
    from eval import real
    timed_world(db, per=21, rated=12, judged=8)
    output = tmp_path / "benchmark"
    assert real.main(["--freeze", "--data-dir", str(output)]) == 0
    version = output / "evaluation" / "benchmarks" / "default" / "v1"
    frozen = [json.loads(line) for line in (version / "records.jsonl").read_text().splitlines()]
    assert real.main(["--benchmark", "default@v1", "--data-dir", str(output)]) == 0
    first = json.loads(sorted((output / "evaluation").glob("results-*.json"))[-1].read_text())
    old = db.query(Judgement).order_by(Judgement.id.desc()).first()
    old_id, old_product = old.id, old.product_id
    excluded = {j.product_id for j in db.query(Judgement)} | {f.product_id for f in db.query(Feedback) if f.product_id is not None}
    replacement = db.query(Product).filter(~Product.id.in_(excluded), Product.image_embedding.isnot(None)).first()
    assert client.delete(f"/api/eval/judgements/{old_id}").status_code == 200
    response = client.post("/api/eval/judgements", json={"product_id": replacement.id, "taste": 3})
    assert response.status_code == 200, response.text
    new_id = response.json()["id"]
    assert real.main(["--benchmark", "default@v1", "--data-dir", str(output)]) == 0
    second = json.loads(sorted((output / "evaluation").glob("results-*.json"))[-1].read_text())
    frozen_products = {r["listing"]["product_id"] for r in frozen}
    print("freeze replacement:", {"old_id": old_id, "new_id": new_id, "old_product": old_product, "new_product": replacement.id, "replacement_in_records": replacement.id in frozen_products})
    assert old_product in frozen_products and replacement.id not in frozen_products
    assert second["summary"]["test_set_hash"] == first["summary"]["test_set_hash"]
    assert second["all"] == first["all"]
