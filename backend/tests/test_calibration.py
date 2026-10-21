"""Approval-estimate calibration (selective recommendation, M3).

SYNTHETIC FIXTURE: the "decisions" below are drawn from a known logistic function of colour and category, so the
tests can check that the machinery recovers a known relationship and reports its uncertainty honestly. They
verify correctness only and say nothing about how well the app predicts a real person's taste.
"""

from __future__ import annotations

import numpy as np
import pytest

COLOUR = {"navy": 1.6, "black": 0.8, "cream": 0.4, "grey": 0.0, "red": -1.8}
CATEGORY = {"overcoat": 0.8, "trousers": 0.0, "hoodie": -0.6}


def true_p(colour: str, category: str) -> float:
    return float(1 / (1 + np.exp(-(COLOUR[colour] + CATEGORY[category] - 0.2))))


def _world(db, n=160, rated=120, seed=0, prefix="cal"):
    """Products with known approval probabilities; the first `rated` get a decision drawn from them."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, Seller
    from app.services import embeddings as emb

    rng = np.random.default_rng(seed)
    sellers = [Seller(name=f"{prefix}{i}", slug=f"{prefix}{i}", platform="manual") for i in range(4)]
    db.add_all(sellers)
    db.flush()
    backend = emb.get_image_backend().name
    out = []
    for i in range(n):
        colour, category = list(COLOUR)[i % 5], list(CATEGORY)[(i // 5) % 3]
        raw = RawListing(f"{prefix}{i}", f"https://shop.example/{prefix}{i}", f"{colour} wool {category} {i}",
                         price=300.0)  # fmt: skip
        p = upsert_listing(db, sellers[i % 4], raw).product
        v = rng.standard_normal(192).astype(np.float32)
        p.image_embedding, p.embedding_backend = emb.to_bytes(emb.normalise(v)), backend
        out.append((p, true_p(colour, category)))
    db.flush()
    for p, pt in out[:rated]:
        db.add(Feedback(product_id=p.id, action="like" if rng.random() < pt else "dislike", reasons=[]))
    db.commit()
    return out


def test_no_estimates_without_enough_decisions_of_both_kinds(env, db):
    from app.models import Feedback
    from app.services import calibration

    world = _world(db, n=40, rated=20)
    assert calibration.recalibrate(db)["status"] == "insufficient"
    assert calibration.active_map(db) is None and calibration.status(db)["state"] == "insufficient"
    db.query(Feedback).update({Feedback.action: "like"})  # 20 approvals, no rejection at all
    for p, _ in world[20:]:
        db.add(Feedback(product_id=p.id, action="love"))
    db.commit()
    res = calibration.recalibrate(db)
    assert res["status"] == "insufficient" and res["labels"] == 40 and res["non_approvals"] == 0


def test_a_label_never_scores_itself(env, db):
    from app.models import Feedback
    from app.services import calibration
    from app.services.approval_data import rated_outcomes

    world = _world(db, n=60, rated=50)
    target = world[3][0]
    space = calibration._space(db)
    m1, _, _, _ = calibration.out_of_fold(db, space, rated_outcomes(db))
    idx = [o.product_id for o in rated_outcomes(db)].index(target.id)
    fb = db.query(Feedback).filter_by(product_id=target.id).one()
    fb.action = "dislike" if fb.action == "like" else "like"  # flip its own label
    db.commit()
    m2, _, y2, _ = calibration.out_of_fold(db, space, rated_outcomes(db))
    assert m1[idx] == pytest.approx(m2[idx])  # its out-of-fold score did not see its label
    assert not np.allclose(m1, m2)  # while other folds' models did change


def test_calibration_recovers_a_known_relationship(env, db):
    from app.services import calibration

    world = _world(db)
    res = calibration.recalibrate(db)
    assert res["status"] == "calibrated"
    d = res["diagnostics"]
    # The labels are noisy draws: scoring them with the TRUE probabilities gives an AUC of 0.71 on this sample.
    assert d["n"] == 120 and d["auc_out_of_fold"] > 0.6
    assert d["map"] in ("platt", "identity") and d["brier_chosen"] <= min(d["brier_raw"], d["brier_calibrated"])
    assert d["ece_chosen"] <= 0.12
    amap = calibration.active_map(db)
    from app.services import preference_model as pm

    post = pm.fit(db, calibration._space(db))
    unrated = world[120:]
    X = post.rows([p.id for p, _ in unrated])
    est, low = amap.estimate(X @ post.mean, post.logit_sd(X), confidence=0.9)
    truth = np.array([pt for _, pt in unrated])
    assert np.mean(np.abs(est - truth)) < 0.15  # close to the known approval probabilities
    assert np.mean(truth >= low) >= 0.85  # the 90% conservative bound is rarely above the truth
    assert np.all(low <= est + 1e-12)


def test_recalibration_lifecycle_and_rollback(env, db):
    from app.models import Calibration, Feedback
    from app.services import calibration

    world = _world(db, n=160, rated=100)
    first = calibration.recalibrate(db)["calibration_id"]
    assert calibration.recalibrate(db)["status"] == "unchanged" and not calibration.needs_refit(db)
    rng = np.random.default_rng(1)
    for p, pt in world[100:105]:  # five new decisions: enough to refit (recalibrate_every = 5)
        db.add(Feedback(product_id=p.id, action="like" if rng.random() < pt else "dislike"))
    db.commit()
    assert calibration.needs_refit(db)
    second = calibration.recalibrate(db)["calibration_id"]
    assert second != first and calibration.active(db).id == second
    calibration.activate(db, first)  # rollback
    assert calibration.active(db).id == first
    assert db.query(Calibration).filter(Calibration.active.is_(True)).count() == 1
    for fb in db.query(Feedback).all()[10:]:  # undo most decisions: no longer enough evidence
        db.delete(fb)
    db.commit()
    assert calibration.recalibrate(db)["status"] == "insufficient" and calibration.active(db) is None


def test_decisions_queue_a_recalibration_when_enough_changed(env, db, monkeypatch):
    from app.services import calibration, jobs

    queued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: queued.append(kind) or 1)
    _world(db, n=40, rated=20)
    calibration.maybe_schedule(db)
    assert queued == []  # not enough decisions yet
    _world(db, n=40, rated=40, seed=2, prefix="more")
    calibration.maybe_schedule(db)
    assert queued == ["recalibrate"]


def test_a_failed_recalibration_is_retried_only_after_a_pause(env, db, monkeypatch):
    from datetime import timedelta

    from app.models import Job, utcnow
    from app.services import calibration, jobs

    queued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: queued.append(kind) or 1)
    _world(db, n=60, rated=50)
    db.add(Job(kind="recalibrate", status="failed", finished_at=utcnow()))
    db.commit()
    calibration.maybe_schedule(db)
    assert queued == []  # not straight back into a failing job
    db.query(Job).update({Job.finished_at: utcnow() - timedelta(minutes=11)})
    db.commit()
    calibration.maybe_schedule(db)
    assert queued == ["recalibrate"]


def test_a_held_out_label_cannot_leak_through_the_background(env, db):
    """With little approval evidence the background sample's weight scales with the approvals in the training
    data. A held-out piece's own label must not count towards it, or its out-of-fold score would depend on it."""
    from app.models import Feedback
    from app.services import calibration
    from app.services import preference_model as pm
    from app.services.approval_data import rated_outcomes

    ps = [p for p, _ in _world(db, n=30, rated=0)]
    target = ps[0]
    db.add(Feedback(product_id=ps[1].id, action="like"))  # one approval: the background weight is not capped
    db.add_all(Feedback(product_id=p.id, action="dislike") for p in ps[2:10])
    fb = Feedback(product_id=target.id, action="dislike")
    db.add(fb)
    db.commit()
    space = calibration._space(db)
    held = frozenset({f"p{target.id}"})

    def background():
        _, ys, ws, _, fams = pm.observations_with_families(db, space, exclude=held)
        return [w for w, y, f in zip(ws, ys, fams, strict=True) if f is None and y == 0]

    def own_score():
        outcomes = rated_outcomes(db)
        m, *_ = calibration.out_of_fold(db, space, outcomes)
        return m[[o.product_id for o in outcomes].index(target.id)]

    bg, m = background(), own_score()
    fb.action = "love"  # the held-out label flips from rejection to the strongest approval
    db.commit()
    assert background() == bg and len(bg) > 0
    assert own_score() == pytest.approx(m)
