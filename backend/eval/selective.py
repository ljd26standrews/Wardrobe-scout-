"""Measure selective recommendation on your own outcomes: is an 80% approval estimate right about 80% of the
time, and does each mode deliver what it aims for?

Usage (from backend/):
    .venv/bin/python -m eval.selective               # both protocols, on a copy of your database
    .venv/bin/python -m eval.selective --db PATH     # another database file
    .venv/bin/python -m eval.selective --block 20    # rolling-origin block size (default 10)
    .venv/bin/python -m eval.selective --labels approval-v1   # the earlier reading of decisions, for comparison

Protocols
---------
1. **Logged, rolling origin.** Your explicit decisions (one label per garment family, see
   app/services/approval_data.py) in time order. At each origin the taste model and the whole calibration
   procedure are fitted only on what was known before it, with the families being predicted left out entirely
   (background sample included), and the next block is predicted. Every decision is predicted once, by a model
   that never saw it or anything after it.
   Caveat: these are pieces you were shown and chose to rate, under whichever feed served them. The numbers
   describe that population, not the whole catalogue, and no inverse-propensity correction is claimed for it
   (the main feed is deterministic; only Learning picks log inclusion probabilities).
2. **Representative.** Your Evaluate-page grades: pieces drawn uniformly from readable undecided listings,
   graded for taste alone (2-3 = approval). Training uses every decision except those about graded families.
   Coverage here estimates the share of candidates a mode would show. Thresholds are suggested on one half of
   the graded families and reported on the other.

Metrics: Brier score, log loss, expected calibration error and reliability bins (with counts and intervals) for
the raw σ(m) and the calibrated estimate; AUC (ties count one half); precision-coverage for gating on σ(m), the
estimate and the conservative bounds; per mode, precision with a 95% Clopper-Pearson interval and coverage, against
the unchanged taste ranking at the same coverage (and, representative protocol, the full Discover score); learning
progression by how many labels the model had; the same for pieces from sellers you had never rated before (the
unfamiliar-seller slice). Comparisons at the same coverage count tied scores at the cut in expectation, as if
ties were ordered at random, so no method gains from an arbitrary tie order.

3. **Served feeds**, from Discover's own logs: how often a feed request came back empty, approvals per piece you
   actually saw (useful discoveries per inspection), and how often a piece you had already seen was shown again
   (repetition), each with counts and intervals.

Refusals: no calibration metric below MIN_PREDICTIONS predictions or MIN_EACH of either outcome; no precision
below MIN_SELECTED selected pieces. Nothing here uses synthetic data; tests/test_eval_selective.py checks the
machinery on a clearly labelled synthetic fixture, for correctness only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

MIN_PREDICTIONS, MIN_EACH, MIN_SELECTED = 50, 5, 10
BLOCK = 10
FIRST_ORIGIN = 10  # labels before the first prediction (calibrated estimates only once there are enough)
MIN_JUDGEMENTS, MIN_LIKED = 40, 5
TRAINING_SIZES = (10, 30, 60, 120, 240)


@dataclass
class Prediction:
    family: str
    y: int
    n_train: int  # labels the model had
    block: int
    m: float  # raw compatibility (posterior mean logit)
    s: float  # its posterior s.d.
    raw: float  # σ(m), the model's own probability
    estimate: float | None = None  # calibrated estimate (None: too few labels to calibrate)
    bounds: dict[str, float] = field(default_factory=dict)  # conservative bound per mode
    score: float | None = None  # representative protocol: the full Discover score
    familiar_seller: bool | None = None  # had you rated a piece from its seller before? (None: seller unknown)


# ----------------------------------------------------------------- metrics
def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.asarray(x, float)))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    from app.services.selective import clopper_pearson as cp

    return cp(k, n, alpha)


def calibration_metrics(p, y) -> dict:
    """Brier, log loss, ECE (equal-count bins) and reliability bins with 95% intervals; refused when too small."""
    from app.services.calibration import _auc, _ece

    p, y = np.asarray(p, float), np.asarray(y, float)
    n, pos = len(y), int(y.sum())
    out: dict = {"n": n, "approvals": pos}
    if n < MIN_PREDICTIONS or pos < MIN_EACH or n - pos < MIN_EACH:
        return out | {"refused": f"needs {MIN_PREDICTIONS} predictions with {MIN_EACH} of each outcome"}
    eps = 1e-9
    ece, bins = _ece(p, y, max(2, min(10, n // 10)))
    for b in bins:
        b["interval"] = [round(v, 3) for v in clopper_pearson(round(b["observed"] * b["n"]), b["n"])]
    # ECE is biased upwards in small bins: what a perfectly calibrated estimate would typically score here.
    floor = sum(b["n"] / n * math.sqrt(2 * b["mean_estimate"] * (1 - b["mean_estimate"]) / (math.pi * b["n"]))
                for b in bins)  # fmt: skip
    return out | {
        "brier": round(float(np.mean((p - y) ** 2)), 4),
        "logloss": round(float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))), 4),
        "ece": round(ece, 4),
        "ece_noise_floor": round(floor, 4),
        "auc": None if (a := _auc(p, y)) is None else round(a, 4),
        "reliability": bins,
    }


def precision(selected_y) -> dict:
    return precision_of(float(sum(selected_y)), len(selected_y))


def precision_of(approved: float, n: int) -> dict:
    """Precision with a 95% Clopper-Pearson interval; ``approved`` may be an expectation over tie orders."""
    k = int(approved) if float(approved).is_integer() else round(approved, 2)
    out = {"selected": n, "approved": k, "precision": None, "interval": None}
    if n >= MIN_SELECTED:
        lo, hi = clopper_pearson(approved, n)
        out.update(precision=round(approved / n, 4), interval=[round(lo, 4), round(hi, 4)])
    return out


def expected_top(score, y, k: int) -> float:
    """Approvals among the top ``k`` pieces by ``score``, in expectation over the order of tied scores (exact):
    every piece above the cut counts, and those tied at the cut count in proportion to the places left."""
    score, y = np.asarray(score, float), np.asarray(y, float)
    if k <= 0:
        return 0.0
    if k >= len(y):
        return float(y.sum())
    cut = np.sort(score)[::-1][k - 1]
    above, tied = score > cut, score == cut
    return float(y[above].sum() + (k - above.sum()) * y[tied].mean())


def precision_coverage(score, y, levels=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0)) -> list[dict]:
    """Precision of the top share of pieces by ``score``, at several coverage levels (ties at the cut in
    expectation)."""
    score, y = np.asarray(score, float), np.asarray(y, int)
    out = []
    for c in levels:
        k = max(1, round(c * len(y)))
        cut = float(np.sort(score)[::-1][k - 1])
        out.append({"coverage": c, "threshold": round(cut, 4), **precision_of(expected_top(score, y, k), k)})
    return out


# ----------------------------------------------------------------- predicting
def _modes(cfg: dict) -> dict[str, tuple[float, float]]:
    from app.services.selective import MODES, thresholds

    return {m: thresholds(cfg, m) for m in MODES}


def predict(db, space, cfg, outcomes, *, train, exclude, before, block, n_train, version=None) -> list[Prediction]:
    """Score ``outcomes`` with a taste model and calibration fitted only on ``train`` (and the rest of what was
    known ``before``), with the ``exclude``d families left out of everything; decisions read as ``version``."""
    from app.services import calibration
    from app.services import preference_model as pm
    from app.services.approval_data import LABEL_VERSION

    version = version or LABEL_VERSION
    obs = pm.observations_with_families(db, space, exclude=exclude, before=before, version=version)
    post = pm.fit_rows(space, *obs[:4])
    X, y, fams = calibration.label_matrix(space, outcomes)
    m = X @ post.mean
    s = post.logit_sd(X)
    amap = None
    if calibration.sufficiency(train, cfg)["sufficient"]:
        a, b, cov, *_ = calibration.fit_map(db, space, train, exclude=exclude, before=before, version=version)
        amap = calibration.ApprovalMap(0, a, b, cov, len(train), sum(o.y for o in train), datetime.now(UTC), {})
    preds = [Prediction(f, int(yi), n_train, block, float(mi), float(si), float(_sigmoid(mi)))
             for f, yi, mi, si in zip(fams, y, m, s, strict=True)]  # fmt: skip
    if amap is not None:
        for mode, (_, conf) in _modes(cfg).items():
            est, low = amap.estimate(m, s, confidence=conf)
            for p, e, lo in zip(preds, est, low, strict=True):
                p.estimate, p.bounds[mode] = float(e), float(lo)
    return preds


def rolling_origin(
    db, *, block: int = BLOCK, first: int = FIRST_ORIGIN, version: str | None = None
) -> list[Prediction]:
    from app.services import calibration
    from app.services.approval_data import LABEL_VERSION, rated_outcomes
    from app.services.settings_store import get_setting

    version = version or LABEL_VERSION
    cfg = get_setting(db, "selective")
    outcomes = rated_outcomes(db, version=version)
    space = calibration._space(db)
    seller = _sellers(db, outcomes)
    preds: list[Prediction] = []
    for b, i in enumerate(range(first, len(outcomes), block)):
        test = outcomes[i : i + block]
        cutoff = test[0].created_at
        held = frozenset(o.family for o in test)
        train = [o for o in rated_outcomes(db, before=cutoff, version=version) if o.family not in held]
        new = predict(db, space, cfg, test, train=train, exclude=held, before=cutoff, block=b, n_train=len(train),
                      version=version)  # fmt: skip
        known = {seller.get(o.feedback_id) for o in train} - {None}
        by_family = {o.family: o for o in test}  # one label per family; a piece that cannot be scored is skipped
        for p in new:
            s = seller.get(by_family[p.family].feedback_id)
            p.familiar_seller = None if s is None else s in known
        preds += new
    return preds


def _sellers(db, outcomes) -> dict[int, str]:
    """The seller of each rated piece (by feedback id), by name: the product's, or the one recorded with the
    decision for a product since deleted."""
    from app.models import Product, Seller

    ids = [o.product_id for o in outcomes if o.product_id is not None]
    names = dict(db.query(Product.id, Seller.name).join(Seller, Product.seller_id == Seller.id)
                 .filter(Product.id.in_(ids))) if ids else {}  # fmt: skip
    out = {}
    for o in outcomes:
        name = names.get(o.product_id) if o.product_id is not None else (o.snapshot or {}).get("seller")
        if name:
            out[o.feedback_id] = name
    return out


# ----------------------------------------------------------------- summarising
def gate_report(preds: list[Prediction], cfg: dict) -> dict:
    """Per mode: what the gate selects, against gating on the raw σ(m) or the estimate alone (ablations), and the
    taste ranking at the same coverage within each block (baseline)."""
    usable = [p for p in preds if p.estimate is not None]
    out: dict = {"predictions": len(usable)}
    if not usable:
        return out | {"refused": "no prediction had enough earlier labels to be calibrated"}
    out["all_rated"] = precision([p.y for p in usable])  # showing everything: the base rate
    for mode, (t, conf) in _modes(cfg).items():
        chosen = [p for p in usable if p.bounds[mode] >= t]
        by_block: dict[int, list[Prediction]] = {}
        for p in usable:
            by_block.setdefault(p.block, []).append(p)
        # The unchanged comparator: the plain taste ranking, taking as many pieces in each block as the gate did.
        baseline = sum(
            expected_top([p.raw for p in ps], [p.y for p in ps], sum(1 for p in chosen if p.block == blk))
            for blk, ps in by_block.items()
        )
        est_only = [p.y for p in usable if p.estimate >= t]
        raw_only = [p.y for p in usable if p.raw >= t]
        out[mode] = {
            "target": t, "confidence": conf, "coverage": round(len(chosen) / len(usable), 4),
            "gate": precision([p.y for p in chosen]),
            "taste_ranking_same_coverage": precision_of(baseline, len(chosen)),
            "ablation_estimate_only": precision(est_only) | {"coverage": round(len(est_only) / len(usable), 4)},
            "ablation_raw_probability": precision(raw_only) | {"coverage": round(len(raw_only) / len(usable), 4)},
        }  # fmt: skip
    return out


def learning_progression(preds: list[Prediction]) -> list[dict]:
    out = []
    edges = [*TRAINING_SIZES, math.inf]
    for lo, hi in zip(edges, edges[1:], strict=False):
        ps = [p for p in preds if lo <= p.n_train < hi]
        if not ps:
            continue
        cal = [p for p in ps if p.estimate is not None]
        out.append({
            "labels_known": [lo, None if hi == math.inf else hi], "predictions": len(ps),
            "raw": calibration_metrics([p.raw for p in ps], [p.y for p in ps]),
            "calibrated": calibration_metrics([p.estimate for p in cal], [p.y for p in cal]),
        })  # fmt: skip
    return out


def summarise(preds: list[Prediction], cfg: dict) -> dict:
    cal = [p for p in preds if p.estimate is not None]
    y = [p.y for p in preds]
    return {
        "predictions": len(preds), "approvals": int(sum(y)), "calibrated_predictions": len(cal),
        "raw": calibration_metrics([p.raw for p in preds], y),
        "calibrated": calibration_metrics([p.estimate for p in cal], [p.y for p in cal]),
        "precision_coverage": {
            "raw": precision_coverage([p.raw for p in cal], [p.y for p in cal]) if cal else [],
            "estimate": precision_coverage([p.estimate for p in cal], [p.y for p in cal]) if cal else [],
            **{f"bound_{m}": precision_coverage([p.bounds[m] for p in cal], [p.y for p in cal]) if cal else []
               for m in _modes(cfg)},
        },
        "modes": gate_report(preds, cfg),
        "learning": learning_progression(preds),
        "unfamiliar_sellers": unfamiliar_slice(preds, cfg),
    }  # fmt: skip


def unfamiliar_slice(preds: list[Prediction], cfg: dict) -> dict:
    """Pieces from sellers you had never rated before: does the model hold up where seller familiarity cannot
    help?"""
    ps = [p for p in preds if p.familiar_seller is False]
    cal = [p for p in ps if p.estimate is not None]
    return {
        "predictions": len(ps), "approvals": int(sum(p.y for p in ps)),
        "raw": calibration_metrics([p.raw for p in ps], [p.y for p in ps]),
        "calibrated": calibration_metrics([p.estimate for p in cal], [p.y for p in cal]),
        "modes": gate_report(ps, cfg),
    }  # fmt: skip


# ----------------------------------------------------------------- representative protocol
def representative(db, seed: int = 0) -> dict:
    """Train on everything except graded families; test on your uniformly sampled Evaluate grades.

    Deletes the graded families' decisions from the session's database: run it on a copy, and last."""
    from app.models import Feedback, Judgement, Product
    from app.services import calibration
    from app.services.approval_data import rated_outcomes
    from app.services.recommender import rank_products
    from app.services.settings_store import get_setting

    cfg = get_setting(db, "selective")
    judged = [j for j in db.query(Judgement).order_by(Judgement.id) if j.product_id is not None]
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_([j.product_id for j in judged]))}
    judged = [j for j in judged if j.product_id in products]
    liked = sum(j.taste >= 2 for j in judged)
    summary = {"judgements": len(judged), "liked": liked}
    if len(judged) < MIN_JUDGEMENTS or liked < MIN_LIKED:
        return {"summary": summary | {"refused": f"needs {MIN_JUDGEMENTS} graded pieces including {MIN_LIKED} liked"}}
    families = frozenset(j.family_key for j in judged)
    members = {p.id for p in db.query(Product) if (f"g{p.group_id}" if p.group_id else f"p{p.id}") in families}
    removed = db.query(Feedback).filter(Feedback.product_id.in_(members)).delete(synchronize_session=False)
    db.commit()  # the copy: nothing about a graded family can reach training, the background or the ranking
    summary["training_decisions_removed"] = removed
    space = calibration._space(db)
    train = rated_outcomes(db)
    from app.services.approval_data import RatedOutcome

    test = [RatedOutcome(j.family_key, j.product_id, -j.id, int(j.taste >= 2), "graded", [], j.created_at)
            for j in judged]  # fmt: skip
    preds = predict(db, space, cfg, test, train=train, exclude=frozenset(), before=None, block=0, n_train=len(train))
    ranked = {r.product.id: r.score for r in rank_products(db, [products[j.product_id] for j in judged])}
    known = set(_sellers(db, train).values())
    for p, j in zip(preds, judged, strict=True):
        p.score = ranked.get(j.product_id)
        seller = products[j.product_id].seller
        p.familiar_seller = None if seller is None else seller.name in known
    out = {"summary": summary, **summarise(preds, cfg)}
    cal = [p for p in preds if p.estimate is not None]
    if not cal:
        return out
    for mode, (t, _) in _modes(cfg).items():  # the full Discover score (taste, quality, value, ...) as baseline
        n = sum(1 for p in cal if p.bounds[mode] >= t)
        score = [p.score if p.score is not None else -1e9 for p in cal]
        out["modes"][mode]["discover_score_same_coverage"] = precision_of(expected_top(score, [p.y for p in cal], n), n)
    out["suggested_thresholds"] = suggest_thresholds(cal, cfg, seed)
    return out


def suggest_thresholds(preds: list[Prediction], cfg: dict, seed: int = 0) -> dict:
    """Per mode: on a validation half (by family), the lowest bound threshold whose precision reaches the
    target with at least MIN_SELECTED pieces; then that threshold's precision on the other half."""

    def half(f: str) -> int:
        return int(hashlib.sha256(f"{seed}:{f}".encode()).hexdigest(), 16) % 2

    val = [p for p in preds if half(p.family) == 0]
    test = [p for p in preds if half(p.family) == 1]
    out = {}
    for mode, (t, _) in _modes(cfg).items():
        found = None
        for c in sorted({round(p.bounds[mode], 4) for p in val}):
            sel = [p.y for p in val if p.bounds[mode] >= c]
            if len(sel) < MIN_SELECTED:
                break
            if sum(sel) / len(sel) >= t:
                found = c
                break
        if found is None:
            out[mode] = {"target": t, "refused": "no threshold reached the target on the validation half"}
            continue
        out[mode] = {"target": t, "threshold": found, "validation": len(val), "test": len(test),
                     **precision([p.y for p in test if p.bounds[mode] >= found])}  # fmt: skip
    return out


# ----------------------------------------------------------------- served feeds (Discover's own logs)
def _rate(k: int, n: int) -> dict:
    out = {"count": k, "of": n, "rate": None, "interval": None}
    if n >= MIN_SELECTED:
        lo, hi = clopper_pearson(k, n)
        out.update(rate=round(k / n, 4), interval=[round(lo, 4), round(hi, 4)])
    return out


def served_feeds(db) -> dict:
    """What Discover served, from its logs (models.FeedRequest and models.Impression).

    * Empty feeds: feed requests that returned nothing, by policy and mode. Discover asks for a feed on every visit
      and after every decision, so these are requests, not sessions.
    * Useful discoveries per inspection: of the garment families you actually saw, the share you then approved
      (love, like or purchased, decided after first seeing it), once per family, by the feed that first showed it.
    * Repetition: a family shown again in a later feed after you had already seen it."""
    from app.models import FeedRequest, Impression, Product
    from app.services.approval_data import rated_outcomes

    feeds = {f.id: f for f in db.query(FeedRequest)}
    requests: dict[str, list[int]] = {}
    for f in feeds.values():
        r = requests.setdefault(f.policy if not f.mode else f"{f.policy}:{f.mode}", [0, 0])
        r[0] += 1
        r[1] += not f.items
    group_of = dict(db.query(Product.id, Product.group_id))
    labels = {o.family: o for o in rated_outcomes(db)}
    first: dict[str, tuple] = {}  # family -> (first seen, the feed policy that showed it)
    seen_in: dict[str, set] = {}
    displays = repeats = 0
    query = db.query(Impression).filter(Impression.product_id.isnot(None))
    for imp in query.order_by(Impression.shown_at, Impression.id):
        gid = group_of.get(imp.product_id)
        family = f"g{gid}" if gid is not None else f"p{imp.product_id}"
        feeds_seen = seen_in.setdefault(family, set())
        if imp.feed_id in feeds_seen:
            continue  # shown twice by one feed (as a card and in the grid): one display
        displays += 1
        repeats += bool(feeds_seen)
        feeds_seen.add(imp.feed_id)
        if family not in first:
            fr = feeds.get(imp.feed_id)
            first[family] = (imp.shown_at, "learning" if imp.explore else (fr.policy if fr else "unlogged"))
    found: dict[str, list[int]] = {}
    for family, (at, policy) in first.items():
        o = labels.get(family)
        f = found.setdefault(policy, [0, 0])
        f[0] += bool(o is not None and o.y == 1 and o.created_at >= at)
        f[1] += 1
    total = [sum(v[0] for v in found.values()), sum(v[1] for v in found.values())]
    return {
        "empty_feeds": {k: _rate(e, n) for k, (n, e) in sorted(requests.items())},
        "discoveries_per_inspection": {"all": _rate(*total), **{k: _rate(*v) for k, v in sorted(found.items())}},
        "repetition": _rate(repeats, displays),
    }


# ----------------------------------------------------------------- report
def _pct(v) -> str:
    return "–" if v is None else f"{100 * v:.0f}%"


def _prec(r: dict) -> str:
    if r["precision"] is None:
        return f"{r['approved']}/{r['selected']} (too few)"
    lo, hi = r["interval"]
    return f"{_pct(r['precision'])} [{_pct(lo)}, {_pct(hi)}] of {r['selected']}"


def _cal_line(name: str, c: dict) -> str:
    if "refused" in c:
        return f"| {name} | {c['n']} | refused: {c['refused']} | | | |"
    ece = f"{_pct(c['ece'])} (noise floor ~{_pct(c['ece_noise_floor'])})"
    return f"| {name} | {c['n']} | {c['brier']:.3f} | {c['logloss']:.3f} | {ece} | {c['auc']} |"


def report(result: dict) -> str:
    lines = ["# Selective recommendation: measured on your own outcomes", "",
             f"Decisions read as `{result.get('labels', 'aesthetic-v2')}` (approval_data.py).", ""]  # fmt: skip
    for key, title in (("logged", "Logged decisions, rolling origin"), ("representative", "Evaluate grades")):
        r = result.get(key) or {}
        lines += [f"## {title}", ""]
        if "refused" in r or "refused" in r.get("summary", {}):
            lines += [f"Not measured: {r.get('refused') or r['summary']['refused']}.", ""]
            continue
        if not r.get("predictions"):
            lines += ["Not measured: no predictions.", ""]
            continue
        lines += [f"{r['predictions']} predictions ({r['approvals']} approvals), {r['calibrated_predictions']} with a "
                  "calibrated estimate.", "", "| Estimate | n | Brier | Log loss | ECE | AUC |", "|---|---|---|---|---|---|",
                  _cal_line("raw σ(m)", r["raw"]), _cal_line("calibrated", r["calibrated"]), ""]  # fmt: skip
        modes = r["modes"]
        if "refused" in modes:
            lines += [f"Modes: refused ({modes['refused']}).", ""]
        else:
            lines += ["| Mode | Coverage | Gate precision [95% CI] | Taste ranking, same coverage | Estimate only "
                      "(its coverage) | Raw σ(m) (its coverage) |", "|---|---|---|---|---|---|"]  # fmt: skip
            for mode in ("balanced", "high", "extreme"):
                g = modes[mode]
                est, raw = g["ablation_estimate_only"], g["ablation_raw_probability"]
                lines.append(f"| {mode} ({_pct(g['target'])} at {_pct(g['confidence'])}) | {_pct(g['coverage'])} | "
                             f"{_prec(g['gate'])} | {_prec(g['taste_ranking_same_coverage'])} | "
                             f"{_prec(est)} ({_pct(est['coverage'])}) | {_prec(raw)} ({_pct(raw['coverage'])}) |")  # fmt: skip
            lines += ["", f"Showing every rated piece instead (the base rate): {_prec(modes['all_rated'])}.", ""]
        u = r.get("unfamiliar_sellers") or {}
        if u.get("predictions"):
            lines += [f"Sellers you had never rated before: {u['predictions']} predictions ({u['approvals']} "
                      "approvals).", "", "| Estimate | n | Brier | Log loss | ECE | AUC |", "|---|---|---|---|---|---|",
                      _cal_line("raw σ(m)", u["raw"]), _cal_line("calibrated", u["calibrated"]), ""]  # fmt: skip
    served = result.get("served")
    if served:
        lines += ["## Served feeds (Discover's logs)", "", "| | Count | Rate [95% CI] |", "|---|---|---|"]
        for k, r in served["empty_feeds"].items():
            lines.append(f"| Empty feed requests, {k} | {r['count']} of {r['of']} | {_share(r)} |")
        for k, r in served["discoveries_per_inspection"].items():
            lines.append(f"| Approved after first seeing it, {k} | {r['count']} of {r['of']} | {_share(r)} |")
        r = served["repetition"]
        lines += [f"| Shown again after you had seen it | {r['count']} of {r['of']} | {_share(r)} |", ""]
    lines += ["Targets are aspirations: a mode meets its target only when the lower end of its interval clears it. "
              "Rates below the minimum sample are shown as counts only."]  # fmt: skip
    return "\n".join(lines)


def _share(r: dict) -> str:
    if r["rate"] is None:
        return "too few"
    return f"{_pct(r['rate'])} [{_pct(r['interval'][0])}, {_pct(r['interval'][1])}]"


def run_all(db, *, block: int = BLOCK, seed: int = 0, labels: str | None = None) -> dict:
    from app.services.approval_data import LABEL_VERSION
    from app.services.preference_model import ENGINE_VERSION
    from app.services.settings_store import get_setting

    labels_version = labels or LABEL_VERSION
    cfg = get_setting(db, "selective")
    preds = rolling_origin(db, block=block, version=labels_version)
    logged = summarise(preds, cfg) if preds else {"refused": f"needs more than {FIRST_ORIGIN} rated pieces"}
    # Aggregates only: no per-decision records are saved. The hash identifies the label set for comparisons.
    label_hash = hashlib.sha256("|".join(sorted(f"{p.family}:{p.y}" for p in preds)).encode()).hexdigest()[:16]
    served = served_feeds(db)  # before the representative protocol, which deletes decisions from the copy
    return {"engine_version": ENGINE_VERSION, "labels": labels_version, "settings": cfg, "block": block,
            "label_set_hash": label_hash, "logged": logged, "served": served,
            "representative": representative(db, seed)}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="database file (default: this installation's)")
    ap.add_argument("--data-dir", help="where results are saved (default: WS_DATA_DIR)")
    ap.add_argument("--block", type=int, default=BLOCK)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--labels", choices=("approval-v1", "aesthetic-v2"), help="reading of decisions (default: current)")
    args = ap.parse_args(argv)

    from app.config import get_settings

    from .dbcopy import open_copy, source_database

    src = source_database(args.db)
    if not src.is_file():
        print(f"database not found: {src}", file=sys.stderr)
        return 2
    out_dir = (Path(args.data_dir) if args.data_dir else get_settings().data_dir) / "evaluation"
    with tempfile.TemporaryDirectory(prefix="ws-eval-") as tmp:
        db = open_copy(src, Path(tmp))
        try:
            result = run_all(db, block=args.block, seed=args.seed, labels=args.labels)
        finally:
            db.close()
    print(report(result))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"selective-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(result, indent=1, default=str))
    print(f"\nSaved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
