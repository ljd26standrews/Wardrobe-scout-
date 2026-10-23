"""Evaluation metrics against trusted references: AUC against scikit-learn, tie handling against brute force over
every order of the tied pieces, and the nested calibration estimate against a hand computation.

SYNTHETIC inputs throughout: these check arithmetic, not anyone's taste.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import numpy as np
import pytest
from sklearn.metrics import roc_auc_score


# ----------------------------------------------------------------- AUC
@pytest.mark.parametrize(
    "score,y",
    [
        ([0.5, 0.5, 0.5, 0.9, 0.1, 0.5], [1, 0, 1, 1, 0, 0]),  # ties across the outcomes
        ([0.3] * 6, [1, 0, 1, 0, 1, 0]),  # constant: no separation at all
        ([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]),  # perfect
        ([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]),  # reversed
        ([1.0, 1.0, 0.0, 0.0], [1, 0, 1, 0]),  # approvals listed first within ties
    ],
)
def test_auc_matches_scikit_learn(score, y):
    from app.services.calibration import _auc

    assert _auc(np.array(score), np.array(y)) == pytest.approx(roc_auc_score(y, score))


def test_auc_matches_scikit_learn_on_heavily_tied_random_scores():
    from app.services.calibration import _auc

    rng = np.random.default_rng(0)
    for _ in range(50):
        y = rng.integers(0, 2, 40)
        if y.min() == y.max():
            continue
        score = rng.integers(0, 4, 40) / 4  # four distinct values: ties everywhere
        assert _auc(score, y) == pytest.approx(roc_auc_score(y, score))


def test_auc_needs_both_outcomes():
    from app.services.calibration import _auc

    assert _auc(np.array([0.2, 0.9]), np.array([1, 1])) is None
    with pytest.warns(UserWarning):  # the reference calls it undefined too
        assert np.isnan(roc_auc_score([1, 1], [0.2, 0.9]))


# ----------------------------------------------------------------- ties at a coverage cut
def _brute_top(score, y, k):
    """Mean approvals in the top k over every order of the tied pieces."""
    order = sorted(range(len(y)), key=lambda i: -score[i])
    groups = [list(g) for _, g in itertools.groupby(order, key=lambda i: score[i])]
    totals = []
    for perms in itertools.product(*(itertools.permutations(g) for g in groups)):
        ranked = [i for p in perms for i in p]
        totals.append(sum(y[i] for i in ranked[:k]))
    return sum(totals) / len(totals)


def test_expected_top_equals_the_average_over_every_tie_order():
    from eval.selective import expected_top

    score = [0.9, 0.5, 0.5, 0.5, 0.5, 0.2, 0.2]
    y = [1, 0, 1, 1, 0, 1, 0]
    for k in range(len(y) + 1):
        assert expected_top(score, y, k) == pytest.approx(_brute_top(score, y, k))


def test_precision_at_coverage_does_not_depend_on_how_ties_are_listed():
    from eval.selective import precision_coverage

    score = [0.7] * 20 + [0.1] * 20
    y = [1] * 10 + [0] * 10 + [0] * 20
    flipped = [0] * 10 + [1] * 10 + [0] * 20  # the same pieces, approvals listed last within the tie
    a = precision_coverage(score, y, levels=(0.25,))[0]
    b = precision_coverage(score, flipped, levels=(0.25,))[0]
    assert a["precision"] == b["precision"] == 0.5  # 10 places among 20 tied pieces, half approved


def test_ranking_metrics_with_ties_equal_the_average_over_every_tie_order():
    from eval.real import ranking_metrics, scored_metrics

    grades = {"a": 3, "b": 0, "c": 2, "d": 0, "e": 1, "f": 3, "g": 0}
    scores = {"a": 0.4, "b": 0.9, "c": 0.4, "d": 0.4, "e": 0.1, "f": 0.9, "g": 0.1}
    order = sorted(grades, key=lambda i: -scores[i])
    groups = [list(g) for _, g in itertools.groupby(order, key=lambda i: scores[i])]
    runs = [ranking_metrics([i for p in perms for i in p], grades)
            for perms in itertools.product(*(itertools.permutations(g) for g in groups))]  # fmt: skip
    exact = scored_metrics(scores, grades)
    for m in exact:
        assert exact[m] == pytest.approx(sum(r[m] for r in runs) / len(runs))


def test_a_constant_score_is_exactly_the_random_baseline():
    from eval.real import random_metrics, ranking_metrics, scored_metrics

    grades = {i: g for i, g in enumerate([3, 0, 2, 0, 1, 0, 0, 2])}
    runs = [ranking_metrics(list(p), grades) for p in itertools.permutations(grades)]
    for m, v in random_metrics(grades).items():
        assert v == pytest.approx(sum(r[m] for r in runs) / len(runs))
    assert scored_metrics(dict.fromkeys(grades, 1.0), grades) == random_metrics(grades)


# ----------------------------------------------------------------- the calibration's reported error is nested
def test_the_reported_calibration_error_includes_the_choice_of_map():
    """Choosing identity or Platt on the same out-of-fold scores that are then reported would flatter the choice.
    The reported figures remake the choice without each fold, by hand here."""
    from app.services import calibration as cal

    rng = np.random.default_rng(3)
    m = rng.normal(0, 1.5, 120)
    y = (rng.random(120) < 1 / (1 + np.exp(-(0.4 * m + 0.3)))).astype(float)  # the raw scale is overconfident
    fold = np.arange(120) % 5
    d = cal.diagnostics(m, y, fold)
    by_hand = np.zeros(120)
    for i in range(5):
        inner = fold != i
        if cal._prefer_platt(m[inner], y[inner], fold[inner]):
            a, b, _ = cal.fit_platt(m[inner], y[inner])
            by_hand[fold == i] = cal._sigmoid(a * m[fold == i] + b)
        else:
            by_hand[fold == i] = cal._sigmoid(m[fold == i])
    assert d["brier_nested"] == pytest.approx(round(float(np.mean((by_hand - y) ** 2)), 4))
    assert len(d["nested_choices"]) == 5


# ----------------------------------------------------------------- served feeds
def test_served_feed_metrics_count_empty_feeds_discoveries_and_repeats(env, db):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, FeedRequest, Impression, Seller, utcnow
    from eval.selective import served_feeds

    seller = Seller(name="sf", slug="sf", platform="manual")
    db.add(seller)
    db.flush()
    ps = [upsert_listing(db, seller, RawListing(f"s{i}", f"https://shop.example/s{i}", f"piece {i}")).product
          for i in range(4)]  # fmt: skip
    db.flush()
    t0 = utcnow() - timedelta(hours=1)

    def feed(fid, policy, mode, items, minutes):
        db.add(FeedRequest(id=fid, engine="bayes", engine_version="x", policy=policy, mode=mode,
                           items=[{"product_id": p.id} for p in items], created_at=t0 + timedelta(minutes=minutes)))  # fmt: skip

    feed("f1", "selective", "balanced", ps[:3], 0)
    feed("f2", "selective", "high", [], 1)  # empty
    feed("f3", "selective", "balanced", ps[1:3], 2)
    db.flush()
    from app.services import exposure

    # Records of the earlier definition (rendered tiles, visible or not): reported, never a denominator.
    for p in ps:
        db.add(Impression(feed_id="f1", product_id=p.id, surface="grid", shown_at=t0))
    db.add(Feedback(product_id=ps[0].id, action="like", created_at=t0 + timedelta(minutes=5)))  # after seeing it
    db.add(Feedback(product_id=ps[1].id, action="dislike", created_at=t0 + timedelta(minutes=5)))
    db.commit()
    out = served_feeds(db)
    assert out["empty_feeds"]["selective:high"] | {} == {"count": 1, "of": 1, "rate": None, "interval": None}
    assert out["empty_feeds"]["selective:balanced"]["count"] == 0
    assert out["legacy_records"] == 4
    assert "unavailable" in out["discoveries_per_exposure"] and "unavailable" in out["repetition"]

    for i, (fid, p, minutes, surface) in enumerate((("f1", ps[0], 0, "card"), ("f1", ps[0], 0, "grid"),
                                                    ("f1", ps[1], 0, "card"), ("f3", ps[1], 2, "card"))):  # fmt: skip
        db.add(Impression(feed_id=fid, product_id=p.id, surface=surface, shown_at=t0 + timedelta(minutes=minutes),
                          definition=exposure.DEFINITION, event_id=f"event-{i:04d}", dwell_ms=1200))  # fmt: skip
    db.commit()
    out = served_feeds(db)
    assert out["exposure_definition"] == exposure.DEFINITION and out["legacy_records"] == 4
    disc = out["discoveries_per_exposure"]["all"]
    assert disc["count"] == 1 and disc["of"] == 2  # the legacy records of ps[2] and ps[3] are not exposures
    assert out["repetition"]["count"] == 1 and out["repetition"]["of"] == 3  # ps[1] again in f3; the grid is not
