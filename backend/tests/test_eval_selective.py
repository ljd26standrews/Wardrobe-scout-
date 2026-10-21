"""The selective evaluator (eval/selective.py).

SYNTHETIC FIXTURE: decisions and grades are drawn from known approval probabilities (tests/test_selective.TRUTH)
at increasing times. These tests check the evaluator's machinery (no future or own label is ever seen,
refusals, aggregates only, the source database untouched) - not how well the app predicts anyone's taste.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import numpy as np
import pytest

from .test_selective import TRUTH


def timed_world(db, per=20, rated=14, seed=0, judged=0):
    """TRUTH world with one decision a minute; ``judged`` undecided pieces per kind get an Evaluate grade."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, Judgement, Seller, utcnow
    from app.services import embeddings as emb

    rng = np.random.default_rng(seed)
    sellers = [Seller(name=f"ev{i}", slug=f"ev{i}", platform="manual") for i in range(3)]
    db.add_all(sellers)
    db.flush()
    backend = emb.get_image_backend().name
    t0 = utcnow() - timedelta(days=30)
    decisions, i = [], 0
    for (colour, cat), pt in TRUTH.items():
        for j in range(per):
            raw = RawListing(f"e{i}", f"https://shop.example/e{i}", f"{colour} wool {cat} {i}", price=300.0)
            p = upsert_listing(db, sellers[i % 3], raw).product
            p.image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
            p.embedding_backend = backend
            db.flush()
            if j < rated:
                decisions.append((p, "like" if rng.random() < pt else "dislike"))
            elif j < rated + judged:
                db.add(Judgement(product_id=p.id, family_key=f"p{p.id}", taste=3 if rng.random() < pt else 0))
            i += 1
    order = rng.permutation(len(decisions))  # decisions arrive in a random order over time
    for k, idx in enumerate(order):
        p, action = decisions[idx]
        db.add(Feedback(product_id=p.id, action=action, created_at=t0 + timedelta(minutes=k)))
    db.commit()


def test_clopper_pearson_matches_known_values():
    from eval.selective import clopper_pearson

    assert clopper_pearson(0, 10) == pytest.approx((0.0, 0.3085), abs=1e-4)
    assert clopper_pearson(5, 10) == pytest.approx((0.1871, 0.8129), abs=1e-4)
    assert clopper_pearson(10, 10) == pytest.approx((0.6915, 1.0), abs=1e-4)


def test_small_samples_are_refused_not_reported():
    from eval.selective import calibration_metrics, precision, precision_coverage

    assert "refused" in calibration_metrics([0.7] * 20, [1] * 15 + [0] * 5)
    assert "refused" in calibration_metrics([0.7] * 60, [1] * 58 + [0] * 2)  # too few of one outcome
    assert precision([1, 1, 0])["precision"] is None
    curve = precision_coverage(np.linspace(0, 1, 40), [0] * 20 + [1] * 20)
    assert curve[-1]["coverage"] == 1.0 and curve[-1]["precision"] == 0.5  # all pieces: the base rate
    assert curve[0]["precision"] is None  # 2 pieces at 5% coverage: too few for a rate


def test_rolling_origin_never_sees_a_label_it_predicts_or_anything_later(env, db):
    from app.models import Feedback
    from app.services.approval_data import rated_outcomes
    from eval.selective import FIRST_ORIGIN, rolling_origin

    timed_world(db)
    outcomes = rated_outcomes(db)
    preds = rolling_origin(db, block=12)
    assert len(preds) == len(outcomes) - FIRST_ORIGIN
    assert len({p.family for p in preds}) == len(preds)  # each label predicted exactly once
    assert all(p.n_train <= FIRST_ORIGIN + 12 * p.block for p in preds)
    assert any(p.estimate is not None for p in preds) and any(p.estimate is None for p in preds)

    last = preds[-1].block
    victim = next(o for o in outcomes if o.family == next(p.family for p in preds if p.block == last))
    fb = db.get(Feedback, victim.feedback_id)
    fb.action = "dislike" if fb.action == "like" else "like"  # change a label in the final block
    db.commit()
    again = rolling_origin(db, block=12)
    for a, b in zip(preds, again, strict=True):
        if a.block < last or a.family == victim.family:  # earlier blocks, and the changed label's own prediction
            assert (a.m, a.estimate) == pytest.approx((b.m, b.estimate))
    assert (
        next(b for b in again if b.family == victim.family).y != next(a for a in preds if a.family == victim.family).y
    )


def test_summary_reports_modes_against_baselines_and_progression(env, db):
    from app.services.settings_store import get_setting
    from eval.selective import report, rolling_origin, summarise

    timed_world(db, per=30, rated=24)
    s = summarise(rolling_origin(db, block=12), get_setting(db, "selective"))
    assert s["calibrated"].get("ece") is not None and s["raw"].get("auc") is not None
    for mode in ("balanced", "high", "extreme"):
        g = s["modes"][mode]
        assert 0 <= g["coverage"] <= 1
        assert {"gate", "taste_ranking_same_coverage", "ablation_estimate_only", "ablation_raw_probability"} <= set(g)
        assert g["taste_ranking_same_coverage"]["selected"] == g["gate"]["selected"]  # matched coverage
    cov = s["modes"]
    assert cov["extreme"]["coverage"] <= cov["high"]["coverage"] <= cov["balanced"]["coverage"]
    assert [b["labels_known"][0] for b in s["learning"]] == sorted(b["labels_known"][0] for b in s["learning"])
    text = report({"logged": s, "representative": {"summary": {"refused": "x"}}})
    assert "Gate precision" in text and "Not measured: x." in text


def test_representative_protocol_refuses_too_few_grades(env, db):
    from eval.selective import representative

    timed_world(db, per=12, rated=8, judged=2)  # 12 graded pieces: too few
    assert "refused" in representative(db)["summary"]


def test_representative_protocol_keeps_graded_families_out_of_training(env, db):
    from app.models import Feedback, Judgement
    from eval.selective import representative

    timed_world(db, per=20, rated=12, judged=8)  # 48 graded pieces
    graded = db.query(Judgement).first()
    db.add(Feedback(product_id=graded.product_id, action="love"))  # a decision about a graded piece
    db.commit()
    r = representative(db)
    assert r["summary"]["judgements"] == 48 and r["summary"]["training_decisions_removed"] == 1
    assert db.query(Feedback).filter_by(product_id=graded.product_id).count() == 0
    assert r["predictions"] == 48 and 0 <= r["modes"]["balanced"]["coverage"] <= 1
    assert "discover_score_same_coverage" in r["modes"]["balanced"]
    assert set(r["suggested_thresholds"]) == {"balanced", "high", "extreme"}


def test_command_line_reads_a_copy_and_saves_aggregates_only(env, db, tmp_path, capsys):
    from app.config import get_settings
    from eval.selective import main

    timed_world(db)
    db.close()
    src = get_settings().database_url.removeprefix("sqlite:///")
    before = hashlib.sha256(open(src, "rb").read()).hexdigest()
    out = tmp_path / "results"
    assert main(["--db", src, "--data-dir", str(out), "--block", "20"]) == 0
    assert hashlib.sha256(open(src, "rb").read()).hexdigest() == before  # the source was never written
    saved = next((out / "evaluation").glob("selective-*.json")).read_text()
    assert '"label_set_hash"' in saved and '"family"' not in saved  # no per-decision records
    assert "Logged decisions, rolling origin" in capsys.readouterr().out


def test_pieces_from_sellers_never_rated_before_are_reported_separately(env, db):
    """The unfamiliar-seller slice: pieces from a seller first rated late are unfamiliar until a block with them has
    been learned from (here all four fall in the last block)."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, Seller, utcnow
    from app.services.approval_data import rated_outcomes
    from eval.selective import rolling_origin, summarise

    timed_world(db)
    late = Seller(name="late", slug="late", platform="manual")
    db.add(late)
    db.flush()
    for k in range(4):  # rated after everything else, one block's worth apart
        p = upsert_listing(
            db, late, RawListing(f"late{k}", f"https://late.example/{k}", f"navy wool overcoat L{k}")
        ).product
        db.add(Feedback(product_id=p.id, action="like", created_at=utcnow() + timedelta(minutes=k)))
    db.commit()
    preds = rolling_origin(db, block=12)
    first_late = {o.family for o in rated_outcomes(db)[-4:]}
    unfamiliar = [p for p in preds if p.familiar_seller is False]
    assert {p.family for p in unfamiliar} == first_late  # one block: none of the four had a rated predecessor
    assert all(p.familiar_seller is not None for p in preds)
    s = summarise(preds, {"min_labels": 30, "min_each_class": 5, **_cfg(db)})
    assert s["unfamiliar_sellers"]["predictions"] == 4 and "refused" in s["unfamiliar_sellers"]["raw"]


def _cfg(db):
    from app.services.settings_store import get_setting

    return get_setting(db, "selective")
