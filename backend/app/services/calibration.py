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
   log loss, expected calibration error and reliability bins, for the raw σ(m) and for p̂, with counts.

Below ``min_labels`` rated families, or without ``min_each_class`` of both outcomes, there is no calibration and no
approval estimate is produced anywhere.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import NormalDist

import numpy as np
from sqlalchemy.orm import Session

from ..logging_setup import get_logger
from ..models import Calibration, Job, Product, utcnow
from . import embeddings as emb
from . import preference_model as pm
from .approval_data import RatedOutcome, rated_outcomes
from .settings_store import get_setting

log = get_logger(__name__)

METHOD = "platt-bayes-v1"
MIN_BRIER_GAIN = 0.001  # Platt must improve the out-of-fold Brier score by at least this much to be used
FOLDS = 5
PRIOR_A = (1.0, 0.5)  # slope: mean, s.d. ("trust the model's scale" unless the evidence says otherwise)
PRIOR_B = (0.0, 1.0)  # intercept
CATALOGUE_GROWTH_REFIT = 0.2  # refit when the catalogue grew by this share since the last fit


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
    exclude: frozenset[str] = frozenset(),
    before: datetime | None = None,
):
    """(m, s, y, fold) for every rated piece, each scored by a taste model fitted without its fold's decisions.

    A held-out family is left out of training entirely (also from the background sample and its weight), so its
    pieces are scored exactly as an undecided candidate would be. ``exclude``/``before`` restrict the training
    data further (rolling-origin evaluation)."""
    X, y, fams = label_matrix(space, outcomes)
    k = max(2, min(folds, len(set(fams))))
    fold = np.array([fold_of(f, k) for f in fams], dtype=int)
    m, s = np.zeros(len(y)), np.zeros(len(y))
    for i in range(k):
        idx = np.flatnonzero(fold == i)
        if not len(idx):
            continue
        held = exclude | {fams[j] for j in idx}
        post = pm.fit_rows(space, *pm.observations_with_families(db, space, exclude=held, before=before)[:4])
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
    pos, neg = score[y == 1], score[y == 0]
    if not len(pos) or not len(neg):
        return None
    ranks = np.argsort(np.argsort(np.concatenate([pos, neg]), kind="stable"), kind="stable") + 1.0
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def diagnostics(m: np.ndarray, y: np.ndarray, fold: np.ndarray) -> dict:
    """Out-of-fold quality of the raw σ(m) and of the calibrated estimate (Platt refitted without each fold)."""
    cal = np.zeros(len(y))
    for i in np.unique(fold):
        a, b, _ = fit_platt(m[fold != i], y[fold != i])
        cal[fold == i] = _sigmoid(a * m[fold == i] + b)
    raw = _sigmoid(m)
    bins = max(2, min(10, len(y) // 10))
    eps = 1e-9

    def logloss(p):
        return float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))

    ece_raw, bins_raw = _ece(raw, y, bins)
    ece_cal, bins_cal = _ece(cal, y, bins)
    return {
        "n": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 4),
        "auc_out_of_fold": None if (a := _auc(m, y)) is None else round(a, 4),
        "brier_raw": round(float(np.mean((raw - y) ** 2)), 4), "brier_calibrated": round(float(np.mean((cal - y) ** 2)), 4),
        "logloss_raw": round(logloss(raw), 4), "logloss_calibrated": round(logloss(cal), 4),
        "ece_raw": round(ece_raw, 4), "ece_calibrated": round(ece_cal, 4), "reliability": bins_cal,
        "reliability_raw": bins_raw,
    }  # fmt: skip


def fit_map(
    db: Session,
    space: pm.FeatureSpace,
    outcomes: list[RatedOutcome],
    *,
    exclude: frozenset[str] = frozenset(),
    before: datetime | None = None,
) -> tuple[float, float, np.ndarray, dict, np.ndarray, np.ndarray]:
    """The whole calibration procedure on these labels, storing nothing: out-of-fold logits, Bayesian Platt,
    nested diagnostics, and the identity map unless Platt beats it out of fold. (a, b, cov, diagnostics, y, fold)"""
    m, _, y, fold = out_of_fold(db, space, outcomes, exclude=exclude, before=before)
    a, b, cov = fit_platt(m, y)
    diag = diagnostics(m, y, fold)
    use_platt = diag["brier_calibrated"] < diag["brier_raw"] - MIN_BRIER_GAIN
    if not use_platt:  # the model's own probabilities did as well: keep them (the uncertainty in cov stays)
        a, b = 1.0, 0.0
    diag["map"] = "platt" if use_platt else "identity"
    diag["brier_chosen"] = diag["brier_calibrated"] if use_platt else diag["brier_raw"]
    diag["ece_chosen"] = diag["ece_calibrated"] if use_platt else diag["ece_raw"]
    diag["reliability_chosen"] = diag["reliability"] if use_platt else diag["reliability_raw"]
    return a, b, cov, diag, y, fold


# ----------------------------------------------------------------- lifecycle
def _label_keys(outcomes: list[RatedOutcome]) -> list[str]:
    return sorted(f"{o.family}:{o.y}:{o.feedback_id}" for o in outcomes)


def sufficiency(outcomes: list[RatedOutcome], cfg: dict) -> dict:
    n, pos = len(outcomes), sum(o.y for o in outcomes)
    need, each = int(cfg["min_labels"]), int(cfg["min_each_class"])
    return {
        "labels": n, "approvals": pos, "non_approvals": n - pos, "needed": need, "needed_each": each,
        "sufficient": n >= need and pos >= each and n - pos >= each,
    }  # fmt: skip


def active(db: Session) -> Calibration | None:
    return (
        db.query(Calibration)
        .filter(
            Calibration.active.is_(True), Calibration.method == METHOD, Calibration.engine_version == pm.ENGINE_VERSION
        )  # fmt: skip
        .order_by(Calibration.id.desc())
        .first()
    )


def active_map(db: Session) -> ApprovalMap | None:
    cal = active(db)
    if cal is None:
        return None
    p = cal.params
    return ApprovalMap(cal.id, float(p["a"]), float(p["b"]), np.array(p["cov"], float), cal.n_labels,
                       cal.n_positive, cal.created_at, cal.diagnostics or {})  # fmt: skip


def recalibrate(db: Session, *, force: bool = False) -> dict:
    """Fit and activate a new calibration from your current decisions, unless nothing relevant changed."""
    cfg = get_setting(db, "selective")
    outcomes = rated_outcomes(db)
    suff = sufficiency(outcomes, cfg)
    if not suff["sufficient"]:
        # e.g. after undoing decisions: no estimate is better than one the evidence no longer supports
        db.query(Calibration).filter(Calibration.active.is_(True)).update({Calibration.active: False})
        db.commit()
        return {"status": "insufficient", **suff}
    keys = _label_keys(outcomes)
    label_hash = hashlib.sha256("|".join([pm.ENGINE_VERSION, *keys]).encode()).hexdigest()
    n_products = db.query(Product.id).count()
    cur = active(db)
    if cur is not None and not force and cur.label_hash == label_hash and not _catalogue_grew(cur, n_products):
        return {"status": "unchanged", "calibration_id": cur.id, **suff}
    a, b, cov, diag, y, fold = fit_map(db, _space(db), outcomes)
    db.query(Calibration).filter(Calibration.active.is_(True)).update({Calibration.active: False})
    cal = Calibration(engine_version=pm.ENGINE_VERSION, method=METHOD, n_labels=len(y), n_positive=int(y.sum()),
                      label_hash=label_hash, active=True,
                      params={"a": a, "b": b, "cov": cov.tolist(), "folds": int(fold.max() + 1),
                              "n_products": n_products, "label_keys": keys},
                      diagnostics=diag)  # fmt: skip
    db.add(cal)
    db.commit()
    log.info("calibrated", calibration_id=cal.id, n=len(y), a=round(a, 3), b=round(b, 3))
    return {"status": "calibrated", "calibration_id": cal.id, "a": a, "b": b, "diagnostics": diag, **suff}


def _catalogue_grew(cal: Calibration, n_products: int) -> bool:
    before = int((cal.params or {}).get("n_products") or 0)
    return before > 0 and n_products > before * (1 + CATALOGUE_GROWTH_REFIT)


def activate(db: Session, calibration_id: int) -> Calibration:
    """Rollback: make an earlier calibration the active one."""
    cal = db.get(Calibration, calibration_id)
    if cal is None:
        raise ValueError("calibration not found")
    db.query(Calibration).filter(Calibration.active.is_(True)).update({Calibration.active: False})
    cal.active = True
    db.commit()
    return cal


def needs_refit(db: Session) -> bool:
    cfg = get_setting(db, "selective")
    if not cfg["enabled"]:
        return False
    outcomes = rated_outcomes(db)
    if not sufficiency(outcomes, cfg)["sufficient"]:
        return False
    cur = active(db)
    if cur is None:
        return True
    changed = set(_label_keys(outcomes)) ^ set((cur.params or {}).get("label_keys") or [])
    return len(changed) >= int(cfg["recalibrate_every"]) or _catalogue_grew(cur, db.query(Product.id).count())


RETRY_AFTER_FAILURE = timedelta(minutes=10)


def maybe_schedule(db: Session) -> None:
    """Queue a recalibration when enough has changed (one at a time; not again for a while after a failure).
    Never raises: a decision must always be recorded and a feed always served."""
    try:
        if not needs_refit(db):
            return
        failed = (
            db.query(Job.id)
            .filter(Job.kind == "recalibrate", Job.status == "failed", Job.finished_at > utcnow() - RETRY_AFTER_FAILURE)
            .first()
        )
        if failed is None:
            from . import jobs

            jobs.enqueue("recalibrate", {})
    except Exception as e:  # noqa: BLE001
        log.warning("recalibration_not_scheduled", error=str(e)[:200])


def status(db: Session) -> dict:
    cfg = get_setting(db, "selective")
    suff = sufficiency(rated_outcomes(db), cfg)
    cur = active(db)
    out = {"state": "calibrated" if cur is not None else ("pending" if suff["sufficient"] else "insufficient"), **suff}
    if cur is not None:
        d = cur.diagnostics or {}
        out["calibration"] = {
            "id": cur.id, "created_at": cur.created_at.isoformat(), "n_labels": cur.n_labels,
            "n_positive": cur.n_positive, "auc_out_of_fold": d.get("auc_out_of_fold"),
            "map": d.get("map"), "brier": d.get("brier_chosen"), "ece": d.get("ece_chosen"),
            "base_rate": d.get("base_rate"),
        }  # fmt: skip
    return out
