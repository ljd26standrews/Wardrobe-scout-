"""The held-out contract (services/provenance.py, eval/heldout.py): nothing labelled that is linked to a held-out
garment teaches a model evaluated on it, however the garment's listings were matched, split, deleted or re-imported,
and predictions do not depend on what is left out. Synthetic catalogue, disposable databases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parents[1]
KINDS = [("navy", "overcoat"), ("black", "hoodie"), ("cream", "trousers")]


def _world(db, n=36, decided=18, seed=0):
    """Listings of three kinds, each with a photo vector and one photographed image (perceptual hash); the first
    ``decided`` get a like (navy, cream) or dislike (black)."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, ProductImage, Seller
    from app.services import embeddings as emb

    rng = np.random.default_rng(seed)
    sellers = [Seller(name=f"h{i}", slug=f"h{i}", platform="manual") for i in range(3)]
    db.add_all(sellers)
    db.flush()
    backend = emb.get_image_backend().name
    out = []
    for i in range(n):
        colour, cat = KINDS[i % 3]
        raw = RawListing(f"h{i}", f"https://shop.example/h{i}", f"{colour} wool {cat} {i}", price=300.0)
        p = upsert_listing(db, sellers[i % 3], raw).product
        v = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
        p.image_embedding, p.embedding_backend = v, backend
        db.add(ProductImage(product_id=p.id, url=f"https://img.example/h{i}.jpg", phash=f"{int(rng.integers(0, 2**62)):016x}",
                            embedding=v, embedding_backend=backend, fetch_status="ok", role="garment"))  # fmt: skip
        out.append(p)
    db.flush()
    for p in out[:decided]:
        db.add(Feedback(product_id=p.id, action="dislike" if "black" in p.colours else "like"))
    db.commit()
    return out


def _link_everything(db, target, matched, suspected):
    """Every kind of labelled record that is about ``target``'s garment: a decision on it, a listing matched with it
    later (and a decision on that), a listing sharing one photo with it (and a decision), a decision about a deleted
    listing at its address, wardrobe pieces linked by listing and by address, and an inspiration photo that is its
    photograph."""
    from app.models import Feedback, InspirationImage, ProductImage, ProductRelation, WardrobeItem
    from app.services import dedup
    from app.services.recommender import snapshot

    db.add(ProductRelation(a_id=min(target.id, matched.id), b_id=max(target.id, matched.id), relation="shared_photos",
                           score=0.9, signals={}))  # fmt: skip
    db.add(ProductRelation(a_id=min(target.id, suspected.id), b_id=max(target.id, suspected.id),
                           relation="similar_photo", score=0.6, signals={}))  # fmt: skip
    db.flush()
    dedup.recompute_groups(db)
    db.add(Feedback(product_id=target.id, action="love", snapshot=snapshot(target)))
    db.add(Feedback(product_id=matched.id, action="like", snapshot=snapshot(matched)))
    db.add(Feedback(product_id=suspected.id, action="dislike", snapshot=snapshot(suspected)))
    db.add(Feedback(product_id=None, action="like", snapshot={**snapshot(target), "title": "deleted listing"}))
    db.add(WardrobeItem(name="the same coat, owned", product_id=target.id, rating=5, colours=["navy"],
                        category="overcoat"))  # fmt: skip
    variant = "http://SHOP.EXAMPLE" + urlsplit(target.source_url).path + "/"  # the same address, written differently
    db.add(WardrobeItem(name="bought elsewhere", source_url=variant, rating=1, colours=["navy"], category="overcoat"))
    img = db.query(ProductImage).filter_by(product_id=target.id).one()
    db.add(InspirationImage(path="insp.jpg", polarity=1, phash=img.phash, embedding=img.embedding,
                            embedding_backend=img.embedding_backend))  # fmt: skip
    db.commit()


def _predict(db, held):
    from app.services import calibration
    from app.services import preference_model as pm

    pm._space_cache.clear()
    space = calibration._space(db)
    rows, ys, ws, n_real, _ = pm.observations_with_families(db, space, exclude=held)
    post = pm.fit_rows(space, rows, ys, ws, n_real)
    pids = sorted(space.row)
    m, s = post.z(pids)
    return dict(zip(pids, m, strict=True)), dict(zip(pids, s, strict=True)), n_real


def _held(db, target):
    from app.services.provenance import Identity

    return Identity(db).resolve(families={f"p{target.id}"}, product_ids={target.id})


def test_nothing_linked_to_a_held_out_garment_reaches_training(env, db):
    from app.models import Feedback
    from app.services import calibration
    from app.services import preference_model as pm

    ps = _world(db)
    target, matched, suspected = ps[20], ps[23], ps[26]
    db.add(Feedback(product_id=ps[30].id, action="like"))
    db.commit()
    _, _, unrelated = _predict(db, frozenset())
    _link_everything(db, target, matched, suspected)
    _, _, everything = _predict(db, frozenset())
    # target and matched are one family (one decision's weight), suspected another; two wardrobe pieces; one photo
    assert everything == unrelated + 2 + 2 + 1
    held = _held(db, target)
    _, _, kept = _predict(db, held)
    assert kept == unrelated
    fams = {f for f in pm.observations_with_families(db, calibration._space(db), exclude=held)[4] if f}
    assert not fams & {f"g{min(target.id, matched.id)}", f"p{suspected.id}", f"p{target.id}"}
    assert held.wardrobe_ids and held.inspiration_ids and {matched.id, suspected.id} <= held.product_ids


def test_predictions_do_not_depend_on_held_out_labels(env, db):
    """Flip every label linked to the held-out garment (and add more): every prediction stays the same. Changing one
    unrelated decision does move them, so the comparison is not vacuous."""
    from app.models import Feedback, InspirationImage, WardrobeItem

    ps = _world(db)
    target, matched, suspected = ps[20], ps[23], ps[26]
    _link_everything(db, target, matched, suspected)
    m0, s0, _ = _predict(db, _held(db, target))

    flip = {"love": "dislike", "like": "dislike", "dislike": "love"}
    linked = [fb for fb in db.query(Feedback) if fb.product_id in (target.id, matched.id, suspected.id)
              or fb.product_id is None]  # fmt: skip
    for fb in linked:
        fb.action = flip[fb.action]
    for w in db.query(WardrobeItem):
        w.rating = 6 - w.rating
    for i in db.query(InspirationImage):
        i.polarity = -i.polarity
    db.add(Feedback(product_id=matched.id, action="love"))
    db.add(WardrobeItem(name="another one", source_url=target.source_url, rating=5, colours=["navy"], category="overcoat"))
    db.commit()
    m1, s1, _ = _predict(db, _held(db, target))
    assert m1.keys() == m0.keys()
    np.testing.assert_allclose([m1[k] for k in m0], [m0[k] for k in m0], atol=1e-6)
    np.testing.assert_allclose([s1[k] for k in s0], [s0[k] for k in s0], atol=1e-6)

    db.query(Feedback).filter(Feedback.product_id == ps[0].id).update({Feedback.action: "dislike"})
    db.commit()
    m2, _, _ = _predict(db, _held(db, target))
    assert max(abs(m2[k] - m1[k]) for k in m1) > 1e-3


def test_leaving_out_in_memory_equals_deleting_from_a_copy(env, db):
    """Out-of-fold calibration leaves held-out records out without deleting them; the evaluators delete them from a
    copy. Both give the same predictions."""
    from eval.heldout import purge_copy

    ps = _world(db)
    target, matched, suspected = ps[20], ps[23], ps[26]
    _link_everything(db, target, matched, suspected)
    held = _held(db, target)
    m_mem, s_mem, n_mem = _predict(db, held)
    removed = purge_copy(db, held)  # this database is disposable
    assert removed == {"decisions": 4, "wardrobe_pieces": 2, "inspiration_photos": 1}
    m_del, s_del, n_del = _predict(db, frozenset())
    assert n_mem == n_del
    np.testing.assert_allclose([m_del[k] for k in m_mem], [m_mem[k] for k in m_mem], atol=1e-6)
    np.testing.assert_allclose([s_del[k] for k in s_mem], [s_mem[k] for k in s_mem], atol=1e-6)


# ----------------------------------------------------------------- later matches, splits, deletion, re-import
def _judge(db, p, taste=3):
    from app.models import Judgement
    from app.services.evaluation import family_key
    from app.services.recommender import snapshot

    j = Judgement(product_id=p.id, family_key=family_key(p), taste=taste, snapshot=snapshot(p))
    db.add(j)
    db.commit()
    return j


def _covered(db, judgements) -> set[int]:
    """Ids of decisions the evaluators' held-out set covers."""
    from app.models import Feedback
    from app.services.provenance import Identity
    from eval.heldout import held_out

    held = held_out(Identity(db), judgements)
    return {fb.id for fb in db.query(Feedback) if held.covers_feedback(fb)}


def test_a_match_made_after_grading_cannot_bypass_the_holdout(env, db):
    from app.models import Feedback
    from app.services import dedup

    ps = _world(db, decided=0)
    j = _judge(db, ps[5])
    other = Feedback(product_id=ps[9].id, action="love")
    db.add(other)
    db.commit()
    assert other.id not in _covered(db, [j])
    dedup.set_user_decision(db, ps[5].id, ps[9].id, same=True)  # later: the same garment
    assert other.id in _covered(db, [j])


def test_a_split_keeps_the_graded_listing_held_out(env, db):
    from app.models import Feedback
    from app.services import dedup

    ps = _world(db, decided=0)
    dedup.set_user_decision(db, ps[5].id, ps[9].id, same=True)
    db.refresh(ps[9])
    j = _judge(db, ps[9])  # recorded as the family "g<5>"
    assert j.family_key == f"g{ps[5].id}"
    mine = Feedback(product_id=ps[9].id, action="love")
    db.add(mine)
    db.commit()
    dedup.set_user_decision(db, ps[5].id, ps[9].id, same=False)  # later: you separated them
    assert mine.id in _covered(db, [j])


def test_deleting_and_reimporting_a_graded_listing_cannot_bypass_the_holdout(env, db):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, Product, Seller
    from app.services.recommender import snapshot

    ps = _world(db, decided=0)
    target = ps[7]
    url, seller_id = target.source_url, target.seller_id
    j = _judge(db, target)
    before = Feedback(product_id=target.id, action="love", snapshot=snapshot(target))
    db.add(before)
    db.commit()
    db.delete(db.get(Product, target.id))  # the listing goes; its decision and grade keep its address
    db.commit()
    db.refresh(before)
    db.refresh(j)
    assert before.product_id is None and j.product_id is None
    again = upsert_listing(db, db.get(Seller, seller_id), RawListing("reimported", url, "navy wool overcoat")).product
    after = Feedback(product_id=again.id, action="love")
    db.add(after)
    db.commit()
    assert {before.id, after.id} <= _covered(db, [j])


def test_a_decision_on_a_deleted_listing_and_on_its_reimport_are_one_label(env, db):
    from app.models import Feedback
    from app.services.approval_data import rated_outcomes
    from app.services.recommender import snapshot

    ps = _world(db, decided=0)
    p = ps[4]
    db.add(Feedback(product_id=None, action="dislike", snapshot={**snapshot(p), "title": "an earlier import"}))
    db.add(Feedback(product_id=p.id, action="like"))
    db.commit()
    outcomes = rated_outcomes(db)
    assert len(outcomes) == 1 and outcomes[0].family == f"p{p.id}" and outcomes[0].y == 1


# ----------------------------------------------------------------- grades about one garment
def test_grades_about_one_garment_count_once_and_disagreements_are_left_out(env, db):
    from app.models import ProductRelation
    from app.services import dedup
    from app.services.provenance import Identity
    from eval.heldout import graded_garments

    ps = _world(db, decided=0)
    agree = [_judge(db, ps[1], 3), _judge(db, ps[2], 2)]
    clash = [_judge(db, ps[3], 3), _judge(db, ps[4], 0)]
    maybe = [_judge(db, ps[5], 2), _judge(db, ps[6], 0)]
    alone = _judge(db, ps[7], 1)
    dedup.set_user_decision(db, ps[1].id, ps[2].id, same=True)
    dedup.set_user_decision(db, ps[3].id, ps[4].id, same=True)
    db.add(ProductRelation(a_id=ps[5].id, b_id=ps[6].id, relation="similar_photo", score=0.6, signals={}))
    db.commit()
    garments, report = graded_garments(db, [*agree, *clash, *maybe, alone], Identity(db))
    by_ids = {tuple(g.judgement_ids): g for g in garments}
    assert set(by_ids) == {(agree[0].id, agree[1].id), (maybe[0].id,), (maybe[1].id,), (alone.id,)}
    assert by_ids[(agree[0].id, agree[1].id)].taste == 3  # the earliest grade
    assert report["conflicts"] == [{"judgement_ids": [clash[0].id, clash[1].id], "grades": [3, 0]}]
    # possibly one garment (one shared photo): separate records, kept on one side of every split
    assert by_ids[(maybe[0].id,)].unit == by_ids[(maybe[1].id,)].unit
    assert by_ids[(alone.id,)].unit != by_ids[(maybe[0].id,)].unit


# ----------------------------------------------------------------- calibration and both evaluators
def test_out_of_fold_scores_ignore_wardrobe_ratings_of_the_held_out_piece(env, db):
    from app.models import WardrobeItem
    from app.services import calibration
    from app.services.approval_data import rated_outcomes

    ps = _world(db, decided=24)
    target = ps[0]  # rated (navy: like)
    w = WardrobeItem(name="the same coat", product_id=target.id, rating=5, colours=["navy"], category="overcoat")
    db.add(w)
    db.commit()

    def own():
        outcomes = rated_outcomes(db)
        m, s, *_ = calibration.out_of_fold(db, calibration._space(db), outcomes)
        i = [o.product_id for o in outcomes].index(target.id)
        return m[i], s[i]

    before = own()
    w.rating = 1
    db.commit()
    assert own() == pytest.approx(before, abs=1e-9)


def test_suspected_aliases_share_a_calibration_fold(env, db):
    from app.models import Feedback, ProductRelation
    from app.services import calibration
    from app.services.approval_data import rated_outcomes

    ps = _world(db, decided=0)
    for p in ps[:20]:
        db.add(Feedback(product_id=p.id, action="like" if "navy" in p.colours else "dislike"))
    for a, b in ((ps[0], ps[1]), (ps[2], ps[7]), (ps[4], ps[13])):
        db.add(ProductRelation(a_id=a.id, b_id=b.id, relation="similar_photo", score=0.6, signals={}))
    db.commit()
    outcomes = rated_outcomes(db)
    *_, fold = calibration.out_of_fold(db, calibration._space(db), outcomes)
    where = {o.product_id: f for o, f in zip(outcomes, fold, strict=True)}
    for a, b in ((0, 1), (2, 7), (4, 13)):
        assert where[ps[a].id] == where[ps[b].id]


def _run(module: str, env: Path) -> None:
    r = subprocess.run([sys.executable, "-m", module, "--db", str(env / "test.db"), "--data-dir", str(env)],
                       cwd=BACKEND, env=os.environ.copy(), capture_output=True, text=True, timeout=900)  # fmt: skip
    assert r.returncode == 0, r.stderr[-3000:]


def test_both_evaluators_hold_out_the_same_records(env, db):
    """eval.real and eval.selective (representative protocol), each on its own copy of one database: the same graded
    garments, the same records left out of training, and the real database untouched."""
    from app.models import Feedback, Judgement, WardrobeItem

    ps = _world(db, n=72, decided=24)
    for p in ps[24:69]:
        _judge(db, p, 3 if "navy" in p.colours else 2 if "cream" in p.colours else 0)
    _link_everything(db, ps[30], ps[31], ps[33])
    before = (db.query(Feedback).count(), db.query(Judgement).count(), db.query(WardrobeItem).count())
    _run("eval.real", env)
    _run("eval.selective", env)
    real = json.loads(sorted((env / "evaluation").glob("results-*.json"))[-1].read_text())["summary"]
    rep = json.loads(sorted((env / "evaluation").glob("selective-*.json"))[-1].read_text())["representative"]["summary"]
    for key in ("judgements", "garments", "liked", "merged", "conflicts", "training_removed", "held_out"):
        assert real[key] == rep[key], key
    assert real["training_removed"] == {"decisions": 4, "wardrobe_pieces": 2, "inspiration_photos": 1}
    db.expire_all()
    assert (db.query(Feedback).count(), db.query(Judgement).count(), db.query(WardrobeItem).count()) == before
