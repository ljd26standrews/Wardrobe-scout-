"""Approval estimates: turning raw compatibility into a probability you can rely on, only when your own decisions
support it (docs/selective-recommendation/02-design.md).

Method
------
1. **Labels**: your explicit decisions on pieces you were shown (``approval_data.rated_outcomes``): one per garment
   family; approved = love, like or purchased; not approved = dislike or maybe.
2. **Out-of-fold compatibility**: the rated families are split into folds (a stable hash of the family). For each
   fold, the taste model is refitted without that fold's decisions and scores the fold's pieces, so no label is
   scored by a model that saw it.
3. **Bayesian Platt scaling** on the out-of-fold mean logits m: approval logit u = a·m + b, with priors
   a ~ N(1, 0.5²) and b ~ N(0, 1²) and a Laplace posterior over (a, b).
   Platt is used only if it beats the identity map (a = 1, b = 0, i.e. the model's own probabilities) on
   out-of-fold Brier score: with few labels, re-estimating two parameters can add more noise than it removes
   (it did on the synthetic fixture in tests/test_calibration.py).
4. **Per piece**: estimate p̂ = σ(â·m + b̂); conservative bound at confidence γ: σ(â·m + b̂ − z_γ·σ_u), where
   σ_u² = (â² + var a)·s² + var(a)·m² + 2·cov(a, b)·m + var(b) combines how little the model knows about this piece
   (s, the posterior s.d. of its logit) with the calibration's own uncertainty.
5. **Diagnostics**, also out-of-fold (the Platt step is refitted without each fold before scoring it): Brier score,
   log loss, expected calibration error and reliability bins, for the raw σ(m) and for p̂, with counts. The figures
   reported for the map in use are nested: the identity-or-Platt choice is remade without each fold before scoring
   it, so the choice cannot flatter itself. AUC counts ties as one half.

Below ``min_labels`` rated families, or without ``min_each_class`` of both outcomes, there is no calibration and no
approval estimate is produced anywhere.

Lifecycle (below): a calibration is used only under the regime it was fitted for (taste model, features, written
profile, photo representation, label reading) and only while every decision it was fitted on still stands. New
decisions make it stale and lead to a refit; an amendment to what it was fitted on (a reason edited, a rated piece
re-analysed or regrouped, a wardrobe rating or inspiration photo changed or removed) makes a refit due at once; an
undone, unsaved or reversed decision, or too few remaining, withdraws it at once. What each decision contributes to
training is fingerprinted (preference_model.training_fingerprints), so an edit is never mistaken for "unchanged".
A fit is activated only if its evidence still holds when it finishes and nothing newer was activated meanwhile, and a
rollback only if it could be used now. Every surface reports the same state (``lifecycle``).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property
from statistics import NormalDist

import numpy as np
from scipy.stats import rankdata
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..logging_setup import get_logger
from ..models import Calibration, Feedback, InspirationImage, Job, Product, WardrobeItem, utcnow
from . import embeddings as emb
from . import preference_model as pm
from . import revisions
from .approval_data import LABEL_VERSION, RatedOutcome, family_of, rated_outcomes
from .provenance import HeldOut, Identity
from .settings_store import get_setting

log = get_logger(__name__)

# v2: held-out folds leave out everything linked to their garments (wardrobe ratings, aliases; services/provenance.py)
# and keep possible aliases in one fold. A v1 fit may have seen its own labels through those paths, so it is refitted.
METHOD = "platt-bayes-v2"
MIN_BRIER_GAIN = 0.001  # Platt must improve the out-of-fold Brier score by at least this much to be used
FOLDS = 5
PRIOR_A = (1.0, 0.5)  # slope: mean, s.d. ("trust the model's scale" unless the evidence says otherwise)
PRIOR_B = (0.0, 1.0)  # intercept


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


@dataclass
class ApprovalMap:
    """The active calibration: u = a·m + b with posterior covariance ``cov`` over (a, b)."""

    id: int
    a: float
    b: float
    cov: np.ndarray
    n_labels: int
    n_positive: int
    created_at: datetime
    diagnostics: dict

    def estimate(self, m: np.ndarray, s: np.ndarray, confidence: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
        """(approval estimate, conservative bound at ``confidence``) for raw logits ``m`` with s.d. ``s``."""
        m, s = np.asarray(m, float), np.asarray(s, float)
        mu = self.a * m + self.b
        var = (self.a**2 + self.cov[0, 0]) * s**2 + self.cov[0, 0] * m**2 + 2 * self.cov[0, 1] * m + self.cov[1, 1]
        z = NormalDist().inv_cdf(confidence) if confidence > 0.5 else 0.0
        return _sigmoid(mu), _sigmoid(mu - z * np.sqrt(np.maximum(var, 0.0)))


# ----------------------------------------------------------------- data
def _space(db: Session) -> pm.FeatureSpace:
    return pm.build_feature_space(db, get_setting(db, "taste_profile"), emb.get_image_backend().name)


def label_matrix(space: pm.FeatureSpace, outcomes: list[RatedOutcome]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Feature rows, labels and families for rated outcomes (a deleted product is scored from its snapshot)."""
    rows, ys, fams = [], [], []
    for o in outcomes:
        if o.product_id is not None and o.product_id in space.row:
            rows.append(space.X[space.row[o.product_id]])
        elif o.snapshot:
            rows.append(space.vector(pm.attr_keys(o.snapshot)))
        else:
            continue
        ys.append(o.y)
        fams.append(o.family)
    X = np.vstack(rows) if rows else np.zeros((0, space.d))
    return X, np.array(ys, float), fams


def fold_of(family: str, k: int) -> int:
    return int(hashlib.sha1(family.encode()).hexdigest(), 16) % k


def out_of_fold(
    db: Session,
    space: pm.FeatureSpace,
    outcomes: list[RatedOutcome],
    folds: int = FOLDS,
    *,
    exclude: frozenset[str] | HeldOut = frozenset(),
    before: datetime | None = None,
    version: str = LABEL_VERSION,
):
    """(m, s, y, fold) for every rated piece, each scored by a taste model fitted without its fold's decisions.

    A held-out fold is left out of training entirely, under the shared held-out policy (services/provenance.py):
    decisions about its garments under any listing or address, wardrobe pieces linked to them and inspiration photos
    that are their photographs, also from the background sample and its weight, so its pieces are scored exactly as
    an undecided candidate would be. Families that are, or may become, one garment share a fold. ``exclude``/``before``
    restrict the training data further (rolling-origin evaluation)."""
    X, y, fams = label_matrix(space, outcomes)
    ident = Identity(db)
    unit = ident.components(fams)
    k = max(2, min(folds, len(set(unit.values()))))
    fold = np.array([fold_of(unit[f], k) for f in fams], dtype=int)
    m, s = np.zeros(len(y)), np.zeros(len(y))
    for i in range(k):
        idx = np.flatnonzero(fold == i)
        if not len(idx):
            continue
        held_families = {fams[j] for j in idx}
        if isinstance(exclude, HeldOut):
            held = ident.resolve(families=exclude.families | held_families, product_ids=exclude.product_ids,
                                 urls=exclude.urls)  # fmt: skip
        else:
            held = ident.resolve(families=set(exclude) | held_families)
        obs = pm.observations_with_families(db, space, exclude=held, before=before, version=version)
        post = pm.fit_rows(space, *obs[:4])
        m[idx] = X[idx] @ post.mean
        s[idx] = post.logit_sd(X[idx])
    return m, s, y, fold


# ----------------------------------------------------------------- the calibration map
def fit_platt(m: np.ndarray, y: np.ndarray) -> tuple[float, float, np.ndarray]:
    """Bayesian logistic calibration u = a·m + b (Gaussian priors, Laplace posterior)."""
    w0 = np.array([PRIOR_A[0], PRIOR_B[0]])
    P0 = np.diag([1 / PRIOR_A[1] ** 2, 1 / PRIOR_B[1] ** 2])
    Z = np.column_stack([m, np.ones_like(m)])
    w = w0.copy()
    for _ in range(100):
        p = _sigmoid(Z @ w)
        g = Z.T @ (p - y) + P0 @ (w - w0)
        H = (Z * (p * (1 - p))[:, None]).T @ Z + P0
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-10:
            break
    p = _sigmoid(Z @ w)
    H = (Z * (p * (1 - p))[:, None]).T @ Z + P0
    return float(w[0]), float(w[1]), np.linalg.inv(H)


def _ece(p: np.ndarray, y: np.ndarray, bins: int) -> tuple[float, list[dict]]:
    """Expected calibration error over equal-count bins, and the bins."""
    order = np.argsort(p, kind="stable")
    out, ece = [], 0.0
    for chunk in np.array_split(order, bins):
        if not len(chunk):
            continue
        mp, oy = float(p[chunk].mean()), float(y[chunk].mean())
        ece += len(chunk) / len(p) * abs(mp - oy)
        out.append({"lo": float(p[chunk].min()), "hi": float(p[chunk].max()), "n": int(len(chunk)),
                    "mean_estimate": round(mp, 4), "observed": round(oy, 4)})  # fmt: skip
    return float(ece), out


def _auc(score: np.ndarray, y: np.ndarray) -> float | None:
    """P(an approved piece scores above a rejected one), ties counting one half (Mann-Whitney U with average ranks).
    None without both outcomes."""
    score, y = np.asarray(score, float), np.asarray(y)
    pos, neg = score[y == 1], score[y == 0]
    if not len(pos) or not len(neg):
        return None
    ranks = rankdata(np.concatenate([pos, neg]), method="average")
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _platt_out_of_fold(m: np.ndarray, y: np.ndarray, fold: np.ndarray) -> np.ndarray:
    cal = np.zeros(len(y))
    for i in np.unique(fold):
        a, b, _ = fit_platt(m[fold != i], y[fold != i])
        cal[fold == i] = _sigmoid(a * m[fold == i] + b)
    return cal


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _prefer_platt(m: np.ndarray, y: np.ndarray, fold: np.ndarray) -> bool:
    """Platt only if it beats the identity map out of fold, by MIN_BRIER_GAIN."""
    if len(np.unique(fold)) < 2:
        return False
    return _brier(_platt_out_of_fold(m, y, fold), y) < _brier(_sigmoid(m), y) - MIN_BRIER_GAIN


def _nested(m: np.ndarray, y: np.ndarray, fold: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Out-of-fold estimates of the whole procedure, its choice between identity and Platt included: for each fold
    the choice is made on the other folds alone, then applied to it. Reporting the better of the two out-of-fold
    scores instead would flatter the chosen map (it was chosen on those same scores)."""
    out, choices = np.zeros(len(y)), []
    for i in np.unique(fold):
        inner = fold != i
        platt = _prefer_platt(m[inner], y[inner], fold[inner])
        if platt:
            a, b, _ = fit_platt(m[inner], y[inner])
            out[fold == i] = _sigmoid(a * m[fold == i] + b)
        else:
            out[fold == i] = _sigmoid(m[fold == i])
        choices.append("platt" if platt else "identity")
    return out, choices


def diagnostics(m: np.ndarray, y: np.ndarray, fold: np.ndarray) -> dict:
    """Out-of-fold quality of the raw σ(m), of the Platt map (refitted without each fold), and of the whole
    procedure including its choice between the two (nested): the last is what the chosen map is reported with."""
    cal = _platt_out_of_fold(m, y, fold)
    nested, choices = _nested(m, y, fold)
    raw = _sigmoid(m)
    bins = max(2, min(10, len(y) // 10))
    eps = 1e-9

    def logloss(p):
        return float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))

    ece_raw, bins_raw = _ece(raw, y, bins)
    ece_cal, bins_cal = _ece(cal, y, bins)
    ece_nested, bins_nested = _ece(nested, y, bins)
    return {
        "n": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 4),
        "auc_out_of_fold": None if (a := _auc(m, y)) is None else round(a, 4),
        "brier_raw": round(_brier(raw, y), 4), "brier_calibrated": round(_brier(cal, y), 4),
        "brier_nested": round(_brier(nested, y), 4),
        "logloss_raw": round(logloss(raw), 4), "logloss_calibrated": round(logloss(cal), 4),
        "logloss_nested": round(logloss(nested), 4),
        "ece_raw": round(ece_raw, 4), "ece_calibrated": round(ece_cal, 4), "ece_nested": round(ece_nested, 4),
        "reliability": bins_cal, "reliability_raw": bins_raw, "reliability_nested": bins_nested,
        "nested_choices": choices,
    }  # fmt: skip


def fit_map(
    db: Session,
    space: pm.FeatureSpace,
    outcomes: list[RatedOutcome],
    *,
    exclude: frozenset[str] | HeldOut = frozenset(),
    before: datetime | None = None,
    version: str = LABEL_VERSION,
) -> tuple[float, float, np.ndarray, dict, np.ndarray, np.ndarray]:
    """The whole calibration procedure on these labels, storing nothing: out-of-fold logits, Bayesian Platt,
    nested diagnostics, and the identity map unless Platt beats it out of fold. (a, b, cov, diagnostics, y, fold)"""
    m, _, y, fold = out_of_fold(db, space, outcomes, exclude=exclude, before=before, version=version)
    a, b, cov = fit_platt(m, y)
    diag = diagnostics(m, y, fold)
    use_platt = _prefer_platt(m, y, fold)
    if not use_platt:  # the model's own probabilities did as well: keep them (the uncertainty in cov stays)
        a, b = 1.0, 0.0
    diag["map"] = "platt" if use_platt else "identity"
    # Reported with the nested estimate: the choice of map is part of what is being measured.
    diag["brier_chosen"] = diag["brier_nested"]
    diag["ece_chosen"] = diag["ece_nested"]
    diag["reliability_chosen"] = diag["reliability_nested"]
    return a, b, cov, diag, y, fold


# ----------------------------------------------------------------- lifecycle
# A calibration maps one quantity (the taste model's raw compatibility, given its features, your written profile and
# the photo representation) to one kind of label (approval, as approval_data reads your decisions). It is used only
# while all of that is unchanged, while your decisions still suffice, and while every decision it was fitted on still
# stands. New evidence only makes it stale: it stays in use and is refitted once enough has accumulated. A decision it
# was fitted on that you undo, unsave or reverse withdraws it at once, as does too little evidence or a changed regime.
CATALOGUE_CHANGE_REFIT = 0.2  # refit when the catalogue grew or shrank by this share since the fit
MAX_ATTEMPTS = 3  # fits per job before giving up when your decisions keep changing during the fit
RETRY_AFTER_FAILURE = timedelta(minutes=10)
PROFILE_FIELDS_WITHOUT_EFFECT = frozenset({"notes"})  # free text: not part of the written-profile score

WITHDRAWN = {
    "insufficient": "too few rated pieces remain for approval estimates",
    "withdrawn": "a decision it was fitted on was undone or unsaved",
    "corrected": "you changed your mind about a piece it was fitted on",
    "model": "the taste model has changed since it was fitted",
    "labels": "the way your decisions are read has changed since it was fitted",
    "representation": "photo analysis has changed since it was fitted",
    "profile": "your written taste profile has changed since it was fitted",
    # Reasons a finished fit is not activated (never reasons to withdraw one in use):
    "amended": "a decision or other evidence it was fitted on changed while it was being fitted",
    "newer": "another calibration was fitted or chosen while it was being fitted",
}
_REGIME_REASON = {"engine": "model", "method": "model", "features": "model", "labels": "labels",
                  "representation": "representation", "profile": "profile"}  # fmt: skip


class NotUsable(ValueError):
    """A stored calibration that may not be used now; ``reason`` is a WITHDRAWN key."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(WITHDRAWN[reason])


def regime(db: Session) -> dict:
    """What a raw compatibility score and a label mean now. A calibration fitted under another regime maps a
    different quantity, so it is never applied."""
    taste = {k: v for k, v in get_setting(db, "taste_profile").items() if k not in PROFILE_FIELDS_WITHOUT_EFFECT}
    return {
        "engine": pm.ENGINE_VERSION,
        "method": METHOD,
        "features": f"interactions={pm.INTERACTIONS};prior_scale={pm.PRIOR_SCALE};pca={pm.PCA_DIM}",
        "labels": LABEL_VERSION,
        "representation": emb.get_image_backend().name,
        "profile": hashlib.sha256(json.dumps(taste, sort_keys=True, default=str).encode()).hexdigest()[:16],
    }


@dataclass
class Evidence:
    """What a calibration would be fitted on now."""

    outcomes: list[RatedOutcome]
    regime: dict
    # Inspiration photos and rated wardrobe pieces ("i<id>:<polarity>:<fingerprint>", "w<id>:<rating>:<fingerprint>"):
    # the taste model learns from them; they are not labels.
    aux: list[str]
    n_products: int
    suff: dict
    # Every decision the taste model learns from, with a fingerprint of what it contributes
    # (preference_model.training_fingerprints): "<decision id>:<fingerprint>:<listing>".
    training: list[str] = field(default_factory=list)
    photo_rev: int = 0  # how many listing photo vectors had been written (revisions "photos")

    @cached_property
    def keys(self) -> list[str]:
        return _label_keys(self.outcomes)

    @cached_property
    def by_family(self) -> dict[str, int]:
        return {o.family: o.y for o in self.outcomes}

    @cached_property
    def feedback_ids(self) -> frozenset[int]:
        return frozenset(o.feedback_id for o in self.outcomes)


# What evidence() reads. Everything it depends on bumps one of these (services/revisions.py); marking a piece as seen
# does not, so a feed does not recompute it.
EVIDENCE_REVISIONS = ("feedback", "products", "photos", "addresses", "wardrobe", "inspiration", "settings")
_evidence_lock = threading.Lock()
_evidence_cache: OrderedDict[str, Evidence] = OrderedDict()


def evidence(db: Session) -> Evidence:
    """What a calibration would be fitted on now: labels, the fingerprinted training contributions, inspiration and
    rated wardrobe pieces, the regime. Computed once per change of the data it reads."""
    reg = regime(db)
    revs = revisions.current(db, *EVIDENCE_REVISIONS)
    key = json.dumps([revs, reg], sort_keys=True)
    with _evidence_lock:
        if key in _evidence_cache:
            _evidence_cache.move_to_end(key)
            return _evidence_cache[key]
    outcomes = rated_outcomes(db)
    aux = [f"i{i.id}:{i.polarity}:{pm.content_fingerprint(i)}"
           for i in db.query(InspirationImage).filter(InspirationImage.embedding.isnot(None))]  # fmt: skip
    aux += [f"w{w.id}:{w.rating}:{pm.content_fingerprint(w)}" for w in db.query(WardrobeItem) if (w.rating or 3) != 3]
    n_products = db.query(func.count(Product.id)).scalar() or 0
    ev = Evidence(outcomes, reg, sorted(aux), n_products, sufficiency(outcomes, get_setting(db, "selective")),
                  pm.training_fingerprints(db), revs[EVIDENCE_REVISIONS.index("photos") + 1])  # fmt: skip
    with _evidence_lock:
        _evidence_cache[key] = ev
        while len(_evidence_cache) > 4:
            _evidence_cache.popitem(last=False)
    return ev


def _label_keys(outcomes: list[RatedOutcome]) -> list[str]:
    return sorted(f"{o.family}:{o.y}:{o.feedback_id}" for o in outcomes)


def _label_hash(ev: Evidence) -> str:
    """Identifies everything a fit depends on: the regime, the labels, every training contribution and the other
    evidence. Equal hashes: refitting would reproduce the same calibration."""
    return hashlib.sha256(json.dumps([ev.regime, ev.keys, ev.training, ev.aux], sort_keys=True).encode()).hexdigest()


def _training_changes(then: list[str] | None, now: list[str]) -> tuple[int, int]:
    """(new, amended) between two training fingerprint lists. New: decisions the model learns from now and did not
    (a newer decision about a listing included). Amended: decisions it learnt from whose contribution changed (a
    reason edited, the piece re-analysed or regrouped), or that no longer teach it with nothing newer about their
    listing (withdrawn evidence that is not a label: a label's withdrawal withdraws the calibration itself)."""
    t = {fid: (fp, listing) for fid, fp, listing in (k.split(":", 2) for k in then or [])}
    n = {fid: (fp, listing) for fid, fp, listing in (k.split(":", 2) for k in now)}
    listings_now = {listing for _, listing in n.values()}
    new = sum(1 for fid in n if fid not in t)
    amended = sum(1 for fid, (fp, listing) in t.items()
                  if (fid in n and n[fid][0] != fp) or (fid not in n and listing not in listings_now))  # fmt: skip
    return new, amended


def _aux_changes(then: list[str], now: list[str]) -> tuple[int, int]:
    """(new, amended) for inspiration and rated wardrobe pieces: added ones are new evidence; a changed rating,
    polarity, photo or attributes, or a removed one, amends what the model was fitted on."""
    t = {k.split(":", 1)[0]: k for k in then}
    n = {k.split(":", 1)[0]: k for k in now}
    return len(n.keys() - t.keys()), sum(1 for i, k in t.items() if n.get(i) != k)


def sufficiency(outcomes: list[RatedOutcome], cfg: dict) -> dict:
    n, pos = len(outcomes), sum(o.y for o in outcomes)
    need, each = int(cfg["min_labels"]), int(cfg["min_each_class"])
    return {
        "labels": n, "approvals": pos, "non_approvals": n - pos, "needed": need, "needed_each": each,
        "sufficient": n >= need and pos >= each and n - pos >= each,
    }  # fmt: skip


def label_change(db: Session, fitted_keys: list[str], ev: Evidence) -> str | None:
    """Whether a decision behind ``fitted_keys`` no longer stands as it was: "withdrawn", "corrected" or None.

    A decision stands if it is still the label of its garment family, or if the family's current label (after a
    re-rating, or after its listings were matched with others) is still the same outcome."""
    fitted = []
    for k in fitted_keys:
        family, y, fid = k.rsplit(":", 2)  # a family of a deleted product contains its URL, colons and all
        if int(fid) not in ev.feedback_ids:
            fitted.append((family, int(y), int(fid)))
    if not fitted:
        return None
    rows = dict(db.query(Feedback.id, Feedback.product_id).filter(Feedback.id.in_([f for *_, f in fitted])))
    pids = {pid for pid in rows.values() if pid is not None}
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(pids))} if pids else {}
    for family, y, fid in fitted:
        pid = rows.get(fid)
        if pid in products:  # superseded, not deleted: where that piece's decision lives now
            family = family_of(products[pid], None, pid)
        now = ev.by_family.get(family)
        if now is None:
            return "withdrawn"
        if now != y:
            return "corrected"
    return None


def invalid_reason(db: Session, cal: Calibration, ev: Evidence) -> str | None:
    """Why ``cal`` may not be used now (a WITHDRAWN key), or None when it may."""
    stored = (cal.params or {}).get("regime")
    if not stored:
        return "model"  # fitted before regimes were recorded: what it maps cannot be checked
    for key, value in ev.regime.items():
        if stored.get(key) != value:
            return _REGIME_REASON[key]
    if not ev.suff["sufficient"]:
        return "insufficient"
    return label_change(db, (cal.params or {}).get("label_keys") or [], ev)


def drift(cal: Calibration, ev: Evidence, cfg: dict) -> dict:
    """What changed since ``cal`` went into service (fitted, or rolled back to). It stays valid, and is refitted
    * at once when evidence it was fitted on was amended: a reason edited, a piece re-analysed or regrouped, a
      wardrobe rating or an inspiration photo changed or removed, a decision that taught it withdrawn (a withdrawn or
      reversed label withdraws the calibration itself: ``invalid_reason``);
    * once ``recalibrate_every`` new decisions (or as many new inspiration photos and rated wardrobe pieces) have
      arrived;
    * when the catalogue grew or shrank, or its photos were re-analysed, by CATALOGUE_CHANGE_REFIT."""
    ref = _service(cal)
    new_training, amended = _training_changes(ref.get("training"), ev.training)
    new = max(len(set(ev.keys) - set(ref["keys"])), new_training)
    aux_new, aux_amended = _aux_changes(ref["aux"], ev.aux)
    catalogue = _catalogue_changed(cal, ev)
    photos = _photos_changed(cal, ev)
    every = int(cfg["recalibrate_every"])
    return {"new_decisions": new, "other_evidence": aux_new, "amended": amended + aux_amended,
            "catalogue_changed": catalogue, "photos_reanalysed": photos,
            "refit_due": new >= every or aux_new >= every or catalogue or photos or amended + aux_amended > 0}  # fmt: skip


def _service(cal: Calibration) -> dict:
    """The evidence when ``cal`` went into service (older calibrations: what they were fitted on)."""
    p = cal.params or {}
    return p.get("service") or {"keys": p.get("label_keys") or [], "training": p.get("training_keys"),
                                "aux": p.get("aux_keys") or [], "n_products": p.get("n_products") or 0}  # fmt: skip


def _catalogue_changed(cal: Calibration, ev: Evidence) -> bool:
    before = int(_service(cal)["n_products"] or 0)
    return before > 0 and abs(ev.n_products - before) > before * CATALOGUE_CHANGE_REFIT


def _photos_changed(cal: Calibration, ev: Evidence) -> bool:
    """Photo vectors written since (new listings and re-analysis, same method or not) reach CATALOGUE_CHANGE_REFIT of
    the catalogue: the photo components every compatibility is computed from have moved."""
    ref = _service(cal)
    if ref.get("photo_rev") is None:
        return False
    return ev.photo_rev - int(ref["photo_rev"]) >= max(1, int(ref["n_products"] or 0)) * CATALOGUE_CHANGE_REFIT


def active(db: Session) -> Calibration | None:
    """The calibration marked active (see ``serving`` for whether it may be used)."""
    return db.query(Calibration).filter(Calibration.active.is_(True)).order_by(Calibration.id.desc()).first()


def withdraw(db: Session, cal: Calibration, reason: str) -> None:
    cal.active = False
    cal.params = {**(cal.params or {}), "withdrawn": {"reason": reason, "at": utcnow().isoformat()}}
    db.commit()
    log.info("calibration_withdrawn", calibration_id=cal.id, reason=reason)


def serving(db: Session, ev: Evidence | None = None) -> tuple[Calibration | None, Evidence]:
    """The calibration that may be used now, and the evidence it was judged on. An active calibration that may no
    longer be used is withdrawn here (if that cannot be recorded right now, it is still not used)."""
    ev = ev or evidence(db)
    cur = active(db)
    if cur is None:
        return None, ev
    reason = invalid_reason(db, cur, ev)
    if reason is None:
        return cur, ev
    try:
        withdraw(db, cur, reason)
    except SQLAlchemyError as e:
        db.rollback()
        log.warning("calibration_withdrawal_not_recorded", calibration_id=cur.id, error=str(e)[:200])
    return None, ev


def to_map(cal: Calibration) -> ApprovalMap:
    p = cal.params
    return ApprovalMap(cal.id, float(p["a"]), float(p["b"]), np.array(p["cov"], float), cal.n_labels,
                       cal.n_positive, cal.created_at, cal.diagnostics or {})  # fmt: skip


def active_map(db: Session) -> ApprovalMap | None:
    cal, _ = serving(db)
    return None if cal is None else to_map(cal)


def recalibrate(db: Session, *, force: bool = False) -> dict:
    """Fit and activate a calibration on your current decisions, unless nothing relevant changed.

    "Nothing relevant" is exact: the regime, the labels, every decision's training contribution (its reason, weight,
    family and the piece's content) and the other evidence hash the same (``_label_hash``). The fit is activated only
    if what it was fitted on still holds when it finishes: decisions undone, reversed or amended in the meantime, too
    few left, or a changed model, photo representation, profile or label reading send it back to be redone (at most
    MAX_ATTEMPTS times); decisions merely added in the meantime are left to the next refit. A calibration fitted or
    rolled back to by someone else meanwhile is never replaced by this older fit."""
    now: Evidence | None = None
    for attempt in range(MAX_ATTEMPTS):
        cur, ev = serving(db)
        if not ev.suff["sufficient"]:
            return {"status": "insufficient", **ev.suff}
        label_hash = _label_hash(ev)
        if (cur is not None and not force and cur.label_hash == label_hash and not _catalogue_changed(cur, ev)
                and not _photos_changed(cur, ev)):  # fmt: skip
            return {"status": "unchanged", "calibration_id": cur.id, **ev.suff}
        marker = _marker(db)
        a, b, cov, diag, y, fold = fit_map(db, _space(db), ev.outcomes)
        db.commit()  # end this read: what follows sees decisions made while fitting,
        db.expire_all()  # including edits to rows this session had already loaded
        now = evidence(db)
        if _marker(db) != marker:  # fitted or rolled back meanwhile: that choice stands; whether a refit is due is
            log.info("calibration_superseded", reason="newer", attempt=attempt + 1)  # decided afresh (lifecycle)
            return {"status": "superseded", "reason": "newer", "message": WITHDRAWN["newer"], **now.suff}
        reason = _superseded(db, ev, now)
        if reason is None:
            cal = _store(db, ev, label_hash, a, b, cov, diag, y, fold)
            return {"status": "calibrated", "calibration_id": cal.id, "a": a, "b": b, "diagnostics": diag, **ev.suff}
        log.info("calibration_superseded", reason=reason, attempt=attempt + 1)
    return {"status": "superseded", "message": "Your decisions changed during every attempt; it will be retried.",
            **(now.suff if now else {})}  # fmt: skip


def _superseded(db: Session, then: Evidence, now: Evidence) -> str | None:
    """Why a fit on ``then`` may not be activated given ``now`` (a WITHDRAWN key), or None. Decisions merely added
    meanwhile do not stop it (they make it stale); anything it was fitted on that changed does."""
    for key, value in now.regime.items():
        if then.regime.get(key) != value:
            return _REGIME_REASON[key]
    if not now.suff["sufficient"]:
        return "insufficient"
    if (reason := label_change(db, then.keys, now)) is not None:
        return reason
    if _training_changes(then.training, now.training)[1] or _aux_changes(then.aux, now.aux)[1]:
        return "amended"
    return None


def _marker(db: Session) -> tuple:
    """Changes when a calibration is stored or activated (by this process or another): a fit that started before
    must not replace it."""
    newest = db.query(func.max(Calibration.id)).scalar()
    cur = (db.query(Calibration.id, Calibration.params).filter(Calibration.active.is_(True))
           .order_by(Calibration.id.desc()).first())  # fmt: skip
    return newest, cur[0] if cur else None, ((cur[1] or {}).get("service") or {}).get("since") if cur else None


def _service_of(ev: Evidence) -> dict:
    """The evidence a calibration is measured against from when it goes into service."""
    return {"keys": ev.keys, "training": ev.training, "aux": ev.aux, "n_products": ev.n_products,
            "photo_rev": ev.photo_rev, "since": utcnow().isoformat()}  # fmt: skip


def _store(db: Session, ev: Evidence, label_hash: str, a, b, cov, diag, y, fold) -> Calibration:
    db.query(Calibration).filter(Calibration.active.is_(True)).update({Calibration.active: False})
    service = _service_of(ev)
    cal = Calibration(engine_version=pm.ENGINE_VERSION, method=METHOD, n_labels=len(y), n_positive=int(y.sum()),
                      label_hash=label_hash, active=True,
                      params={"a": a, "b": b, "cov": cov.tolist(), "folds": int(fold.max() + 1),
                              "n_products": ev.n_products, "label_keys": ev.keys, "training_keys": ev.training,
                              "aux_keys": ev.aux, "regime": ev.regime, "service": service},
                      diagnostics=diag)  # fmt: skip
    db.add(cal)
    db.commit()
    log.info("calibrated", calibration_id=cal.id, n=len(y), a=round(a, 3), b=round(b, 3))
    return cal


def activate(db: Session, calibration_id: int) -> Calibration:
    """Rollback (or forward) to a stored calibration, only if it may be used now: fitted under the current model,
    photo representation, profile and label reading, on decisions that all still stand, with enough decisions
    overall. Raises LookupError or NotUsable."""
    cal = db.get(Calibration, calibration_id)
    if cal is None:
        raise LookupError("calibration not found")
    ev = evidence(db)
    reason = invalid_reason(db, cal, ev)
    if reason is not None:
        raise NotUsable(reason)
    db.query(Calibration).filter(Calibration.active.is_(True), Calibration.id != cal.id).update(
        {Calibration.active: False}
    )
    params = {k: v for k, v in (cal.params or {}).items() if k != "withdrawn"}
    # New evidence is counted from now: a rollback is not undone by the decisions that preceded it.
    cal.params = {**params, "service": _service_of(ev)}
    cal.active = True
    db.commit()
    return cal


def needs_refit(db: Session, ev: Evidence | None = None) -> bool:
    cur, ev = serving(db, ev)
    if not get_setting(db, "selective")["enabled"] or not ev.suff["sufficient"]:
        return False
    return cur is None or drift(cur, ev, get_setting(db, "selective"))["refit_due"]


def _recently_failed(db: Session) -> bool:
    last = db.query(Job).filter(Job.kind == "recalibrate").order_by(Job.id.desc()).first()
    return last is not None and last.status == "failed" and last.finished_at is not None and (
        last.finished_at > utcnow() - RETRY_AFTER_FAILURE
    )  # fmt: skip


def maybe_schedule(db: Session, ev: Evidence | None = None) -> None:
    """After anything that can change what approval estimates rest on (a decision, an Undo or unsave, an import, a
    settings change, a scan, a new photo representation): withdraw the calibration at once if it no longer holds, and
    queue a refit when one is due (one at a time; not again for a while after a failure). Never raises: a decision
    must always be recorded and a feed always served."""
    try:
        if needs_refit(db, ev) and not _recently_failed(db):
            from . import jobs

            jobs.enqueue("recalibrate", {})
    except Exception as e:  # noqa: BLE001
        log.warning("recalibration_not_scheduled", error=str(e)[:200])


def waiting_state(db: Session, ev: Evidence) -> str:
    """With no calibration in use: "uncalibrated" (not enough decisions), "failed" (the last fit failed; it is
    retried after RETRY_AFTER_FAILURE) or "pending" (a fit is queued, running or about to be)."""
    if not ev.suff["sufficient"]:
        return "uncalibrated"
    return "failed" if _recently_failed(db) else "pending"


def _last_attempt(db: Session) -> dict | None:
    job = db.query(Job).filter(Job.kind == "recalibrate").order_by(Job.id.desc()).first()
    if job is None:
        return None
    error = (job.result or {}).get("error") if job.status == "failed" else None
    return {"status": job.status, "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "error": error[:200] if error else None}  # fmt: skip


def last_withdrawal(db: Session, since: datetime | None = None) -> dict | None:
    """The most recent withdrawal (after ``since``, the current calibration's creation), with its reason."""
    for cal in db.query(Calibration).order_by(Calibration.id.desc()).limit(20):
        w = (cal.params or {}).get("withdrawn")
        if w and (since is None or datetime.fromisoformat(w["at"]) > since):
            return {"calibration_id": cal.id, "reason": w["reason"], "label": WITHDRAWN[w["reason"]], "at": w["at"]}
    return None


def lifecycle(db: Session, ev: Evidence | None = None) -> tuple[Calibration | None, str, Evidence, dict]:
    """(calibration in use, state, evidence, details): the one answer every surface gives. States: "calibrated",
    "stale" (in use; new evidence awaits a refit), "pending", "failed", "uncalibrated"."""
    cfg = get_setting(db, "selective")
    cur, ev = serving(db, ev)
    if cfg["enabled"]:
        maybe_schedule(db, ev)  # e.g. after a withdrawal, or once a failed attempt's back-off has passed
    details: dict = {}
    if cur is not None:
        d = drift(cur, ev, cfg)
        state = "stale" if d["refit_due"] else "calibrated"
        details["since"] = d
    else:
        state = waiting_state(db, ev)
    w = last_withdrawal(db, cur.created_at if cur is not None else None)
    if w is not None and cur is None:
        details["withdrawn"] = w
    return cur, state, ev, details


def status(db: Session) -> dict:
    cur, state, ev, details = lifecycle(db)
    out = {"state": state, **ev.suff, "representation": ev.regime["representation"],
           "label_version": ev.regime["labels"], "calibration": None, **details}  # fmt: skip
    if (attempt := _last_attempt(db)) is not None:
        out["last_attempt"] = attempt
    if cur is not None:
        d = cur.diagnostics or {}
        out["calibration"] = {
            "id": cur.id, "created_at": cur.created_at.isoformat(), "n_labels": cur.n_labels,
            "n_positive": cur.n_positive, "auc_out_of_fold": d.get("auc_out_of_fold"),
            "map": d.get("map"), "brier": d.get("brier_chosen"), "ece": d.get("ece_chosen"),
            "base_rate": d.get("base_rate"),
        }  # fmt: skip
    return out
