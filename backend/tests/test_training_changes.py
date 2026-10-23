"""Changes to what the taste model learns from, and the calibration resting on it (services/calibration.py lifecycle,
preference_model.training_fingerprints).

An amendment to evidence a calibration was fitted on (a reason edited, a rated piece re-analysed or regrouped, a
wardrobe rating or an inspiration photo changed or removed) makes a refit due at once; new evidence only counts
towards ``recalibrate_every``; things that do not change any training contribution (marking pieces as seen, a reason
that narrows nothing) change nothing. A fit is never activated over evidence amended while it ran, nor over a
calibration fitted or chosen meanwhile.

SYNTHETIC FIXTURE (as in test_calibration_lifecycle): lifecycle rules only, nothing about predicting a real person.
"""

from __future__ import annotations

import math

import numpy as np

from . import test_calibration_lifecycle as lifecycle
from .test_calibration_lifecycle import calibration_status, catalogue, everywhere, rate, surfaces

held = lifecycle.held  # the fixture: background jobs are recorded, and run by held.run()


def _calibrated(held, db, n=36):
    ids = catalogue(db)
    fids = rate(held, ids[:n], approvals=n // 2)
    held.run()
    assert surfaces(held, ids[44]) == everywhere("calibrated")
    return ids, fids


def _dislike_ids(fids, n):
    return fids[n // 2 :]  # rate(): the first half approved, the rest disliked


def test_editing_a_reason_makes_a_refit_due_at_once_and_every_surface_agrees(held, db):
    from app.services import calibration
    from app.services import preference_model as pm

    ids, fids = _calibrated(held, db)
    first = calibration_status(held)["calibration"]["id"]
    space = calibration._space(db)
    before = pm.fit(db, space).mean.copy()
    held.queue.clear()

    target = _dislike_ids(fids, 36)[0]
    r = held.post(f"/api/feedback/{target}/reasons", json={"reasons": ["colour"]})
    assert r.status_code == 200
    # The model learns something different from that decision now ...
    assert np.max(np.abs(pm.fit(db, calibration._space(db)).mean - before)) > 1e-4
    # ... so the calibration is due for a refit, still usable meanwhile, and every surface says the same.
    assert surfaces(held, ids[44]) == everywhere("stale")
    st = calibration_status(held)
    assert st["since"]["amended"] == 1 and st["since"]["new_decisions"] == 0 and st["since"]["refit_due"] is True
    assert "withdrawn" not in st
    assert ("recalibrate", {}) in held.queue
    held.run()
    assert surfaces(held, ids[44]) == everywhere("calibrated")
    st = calibration_status(held)
    assert st["calibration"]["id"] != first and st["since"]["amended"] == 0


def test_a_reason_that_narrows_nothing_new_changes_nothing(held, db):
    """A colour rejection that also says "too expensive" teaches exactly what the colour rejection did."""
    ids, fids = _calibrated(held, db)
    target = _dislike_ids(fids, 36)[0]
    held.post(f"/api/feedback/{target}/reasons", json={"reasons": ["colour"]})
    held.run()
    first = calibration_status(held)["calibration"]["id"]
    held.queue.clear()
    assert held.post(f"/api/feedback/{target}/reasons", json={"reasons": ["colour", "too_expensive"]}).status_code == 200
    assert surfaces(held, ids[44]) == everywhere("calibrated")
    assert calibration_status(held)["since"]["amended"] == 0 and not held.queue
    from app.services import calibration

    assert calibration.recalibrate(db)["status"] == "unchanged"
    assert calibration_status(held)["calibration"]["id"] == first


def test_marking_pieces_as_seen_changes_no_training_contribution(held, db):
    from app.models import Product
    from app.services import calibration

    ids, _ = _calibrated(held, db)
    ev = calibration.evidence(db)
    db.query(Product).update({Product.is_new: False})
    db.commit()
    assert calibration.evidence(db).training == ev.training
    assert surfaces(held, ids[44]) == everywhere("calibrated")


def test_re_analysing_a_rated_piece_amends_the_evidence(held, db):
    from app.models import Product
    from app.services import embeddings as emb

    ids, _ = _calibrated(held, db)
    p = db.get(Product, ids[3])
    rng = np.random.default_rng(7)
    p.image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))  # same method
    db.commit()
    assert surfaces(held, ids[44]) == everywhere("stale")
    assert calibration_status(held)["since"]["amended"] == 1


def test_re_analysing_much_of_the_catalogue_makes_a_refit_due(held, db):
    """Unrated pieces re-analysed with the same method move the photo components every score is computed from."""
    from app.models import Feedback, Product
    from app.services import embeddings as emb
    from app.services.calibration import CATALOGUE_CHANGE_REFIT

    ids, _ = _calibrated(held, db)
    rng = np.random.default_rng(11)
    decided = {pid for (pid,) in db.query(Feedback.product_id)}  # your decisions (and the seed examples)
    unrated = [p for p in db.query(Product).order_by(Product.id) if p.id not in decided]
    need = math.ceil(len(db.query(Product).all()) * CATALOGUE_CHANGE_REFIT)
    for p in unrated[: need - 1]:  # one short of the share: not yet
        p.image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
    db.commit()
    assert surfaces(held, ids[44]) == everywhere("calibrated")
    unrated[need - 1].image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
    db.commit()
    assert surfaces(held, ids[44]) == everywhere("stale")
    assert calibration_status(held)["since"]["photos_reanalysed"] is True


def test_regrouping_a_rated_piece_amends_the_evidence(held, db):
    """Two rated listings found to be one garment share one decision's weight from then on."""
    ids, _ = _calibrated(held, db)
    r = held.post(f"/api/products/{ids[0]}/match", json={"other_id": ids[3], "same": True})
    assert r.status_code == 200, r.text
    st = calibration_status(held)
    assert st["since"]["amended"] >= 1 and st["since"]["refit_due"] is True


def test_wardrobe_ratings_and_inspiration_photos(held, db):
    """Adding a rated wardrobe piece or a photo is new evidence (counted); changing or removing one amends what the
    calibration was fitted on (a refit at once)."""
    from app.models import InspirationImage
    from app.services import calibration
    from app.services import embeddings as emb

    ids, _ = _calibrated(held, db)
    r = held.post("/api/wardrobe", data={"name": "navy overcoat I own", "category": "overcoat", "colours": "navy",
                                         "rating": "5"})  # fmt: skip
    assert r.status_code == 200, r.text
    item = r.json()["id"]
    st = calibration_status(held)
    assert st["since"]["other_evidence"] == 1 and st["since"]["amended"] == 0 and st["state"] == "calibrated"
    # A piece added since is new evidence: re-rating it before a refit is still just new evidence.
    assert held.patch(f"/api/wardrobe/{item}", json={"rating": 4}).status_code == 200
    assert calibration_status(held)["since"]["amended"] == 0
    held.post("/api/selective/recalibrate")  # refitted with it
    held.run()
    assert calibration_status(held)["since"]["other_evidence"] == 0

    assert held.patch(f"/api/wardrobe/{item}", json={"rating": 1}).status_code == 200
    st = calibration_status(held)
    assert st["since"]["amended"] == 1 and st["state"] == "stale"
    held.run()
    assert calibration_status(held)["state"] == "calibrated"

    assert held.delete(f"/api/wardrobe/{item}").status_code == 200
    assert calibration_status(held)["since"]["amended"] == 1
    held.run()

    vec = emb.to_bytes(emb.normalise(np.random.default_rng(3).standard_normal(192).astype(np.float32)))
    img = InspirationImage(path="x.jpg", polarity=1, embedding=vec, embedding_backend=emb.get_image_backend().name)
    db.add(img)
    db.commit()
    assert calibration_status(held)["since"]["other_evidence"] == 1
    held.post("/api/selective/recalibrate")  # refitted with it
    held.run()
    img.polarity = -1  # now an anti-inspiration photo
    db.commit()
    assert calibration_status(held)["since"]["amended"] == 1
    assert calibration.lifecycle(db)[1] == "stale"


def test_a_reason_edited_while_the_job_is_fitting_is_never_activated(env, db, monkeypatch):
    """The job reads your decisions, fits, then rechecks them: a decision amended meanwhile sends the fit back to be
    redone, and what is activated was fitted on the amended decision."""
    from app.db import SessionLocal
    from app.models import Feedback
    from app.services import calibration, jobs
    from app.services import preference_model as pm

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: 0)
    ids = catalogue(db)
    for i, pid in enumerate(ids[:34]):
        db.add(Feedback(product_id=pid, action="like" if i % 2 else "dislike", reasons=[]))
    db.commit()
    edited = db.query(Feedback).filter_by(product_id=ids[4]).one().id
    real_fit, calls = calibration.fit_map, []

    def fit_while_editing(*args, **kw):
        calls.append(1)
        result = real_fit(*args, **kw)
        if len(calls) == 1:  # meanwhile, in another request: a reason added to a dislike it was fitting on
            with SessionLocal() as other:
                other.get(Feedback, edited).reasons = ["silhouette"]
                other.commit()
        return result

    monkeypatch.setattr(calibration, "fit_map", fit_while_editing)
    assert calibration.recalibrate(db)["status"] == "calibrated" and len(calls) == 2
    db.expire_all()
    cal = calibration.active(db)
    assert cal.params["training_keys"] == pm.training_fingerprints(db)
    assert calibration.lifecycle(db)[1] == "calibrated"


def test_a_fit_never_replaces_a_calibration_fitted_or_chosen_while_it_ran(env, db, monkeypatch):
    """A job that started before another calibration was fitted, or before you rolled back to an earlier one, does
    not activate its older fit over it."""
    from app.db import SessionLocal
    from app.models import Calibration, Feedback
    from app.services import calibration, jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: 0)
    ids = catalogue(db)
    for i, pid in enumerate(ids[:34]):
        db.add(Feedback(product_id=pid, action="like" if i % 2 else "dislike", reasons=[]))
    db.commit()
    assert calibration.recalibrate(db)["status"] == "calibrated"
    first = calibration.active(db).id
    for i, pid in enumerate(ids[34:40]):
        db.add(Feedback(product_id=pid, action="like" if i % 2 else "dislike", reasons=[]))
    db.commit()
    real_fit = calibration.fit_map

    def fit_while_rolling_back(*args, **kw):
        result = real_fit(*args, **kw)
        with SessionLocal() as other:  # meanwhile: you roll back to the first calibration
            calibration.activate(other, first)
        return result

    monkeypatch.setattr(calibration, "fit_map", fit_while_rolling_back)
    res = calibration.recalibrate(db)
    assert res["status"] == "superseded" and res["reason"] == "newer"
    db.expire_all()
    assert calibration.active(db).id == first and db.query(Calibration).count() == 1

    def fit_while_another_fits(*args, **kw):
        result = real_fit(*args, **kw)
        with SessionLocal() as other:  # meanwhile: another process fits and activates
            monkeypatch.setattr(calibration, "fit_map", real_fit)
            calibration.recalibrate(other, force=True)
        return result

    monkeypatch.setattr(calibration, "fit_map", fit_while_another_fits)
    res = calibration.recalibrate(db, force=True)
    assert res["status"] == "superseded" and res["reason"] == "newer"
    db.expire_all()
    newest = db.query(Calibration).order_by(Calibration.id.desc()).first()
    assert calibration.active(db).id == newest.id != first and db.query(Calibration).count() == 2
