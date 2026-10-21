"""Bayesian preference model: learns P(you like an item) with honest uncertainty.

Why this replaced the hand-weighted score (audit, Oct 2026)
-----------------------------------------------------------
In a simulation with hidden-taste users, the previous engine got *worse* as feedback accumulated when the
written profile was right (precision@10 0.87 → 0.48). Ablations traced this to a z-scored nearest-prototype
visual term. This model, evaluated on the same simulation, reached 0.96 (written profile right) and
0.53 vs 0.40 (profile partly wrong). See ``backend/eval/`` to reproduce.

Model
-----
Logistic regression with a diagonal Gaussian posterior over weights (online Laplace approximation,
Chapelle & Li, 2011, "An Empirical Evaluation of Thompson Sampling").

Features x(item):
* ``prior``      your written taste profile's score for the item. Its weight starts at 1 (trust it) and is
                 learned like any other, so behaviour that contradicts the profile turns it down.
* one-hot colour / category / fibre / fit / logo / brand. Seller is deliberately NOT a taste feature.
* ``img:k``      16 principal components of the image embedding (CLIP or the lite fallback).

* ``bias``       intercept (absorbs the base rate so "everything shown was loved" is not mistaken for
                 "trust the written profile more").

Observations: love (y=1, weight 2), like (1, 1), purchased (1, 1.5), maybe (1, 0.3), dislike (0, 1).
Positive-unlabelled learning: a small *background* sample of undecided catalogue items is added as weak
label-0 observations (total weight BACKGROUND_WEIGHT). It does not claim you dislike them; it lets the model
learn what distinguishes the items you choose from a typical item (without it, a run of loves of yellow
pieces barely moved the yellow weight). Skips are still never used.
Rejection reasons mask the observation to the attribute group they name, so "dislike the colour" only
moves colour weights. "Too expensive", "already own" and "construction" never update taste.
Inspiration photos, anti-inspiration photos and wardrobe pieces you rate highly are image/attribute
observations too. Skips teach nothing.

Uncertainty is used for exploration (Thompson sampling) and shown in explanations.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Feedback, InspirationImage, Product, WardrobeItem
from . import embeddings as emb

GROUPS = ("colour", "category", "fibre", "fit", "logo", "brand")
PCA_DIM = 16
IMG_SCALE = 0.5
PRIOR_VAR_ATTR = 0.15
PRIOR_VAR_IMG = 0.1
PRIOR_VAR_TRUST = 1.5  # how quickly behaviour may override the written profile (audit: 0.3 too sticky)
PRIOR_MEAN_TRUST = 1.0
PRIOR_VAR_BIAS = 1.0
BACKGROUND_WEIGHT = 2.0  # total weight of the background sample (≈ two weak "typical, not chosen" items)
BACKGROUND_SAMPLE = 300
PRIOR_SCALE = 2.0  # written-profile score (0..1) -> logit units: (score - 0.5) * PRIOR_SCALE. Calibrated on seed 1:
# 2.0 vs 4.0 gave up a little when the profile is right (0.95→0.89) but gained when it is partly or
# wholly wrong (0.46→0.54, 0.16→0.23), the best average. Held-out seeds are reported in eval/RESULTS.md.

LABEL = {"love": (1, 2.0), "like": (1, 1.0), "purchased": (1, 1.5), "maybe": (1, 0.3), "dislike": (0, 1.0)}
REASON_GROUPS = {
    "colour": {"colour"},
    "fabric": {"fibre"},
    "logo": {"logo", "brand"},
    "silhouette": {"fit", "category", "img"},
    "specific_item": {"img"},
}
NON_TASTE_REASONS = {"too_expensive", "already_own", "construction"}

GROUP_LABEL = {
    "colour": "colour",
    "category": "garment type",
    "fibre": "material",
    "fit": "fit",
    "logo": "branding",
    "brand": "brand/style",
}


def attr_keys(obj) -> list[tuple[str, str]]:
    """Attribute features for a Product or WardrobeItem (no seller: seller affinity is not taste)."""
    g = obj.get if isinstance(obj, dict) else (lambda k, _o=obj: getattr(_o, k, None))
    keys = [("colour", c) for c in (g("colours") or [])]
    keys += [("fibre", m["fibre"]) for m in (g("materials") or []) if isinstance(m, dict) and m.get("fibre")]
    keys += [("fit", f) for f in (g("fit_tags") or [])]
    for group, value in (("category", g("category")), ("brand", g("brand"))):
        if value:
            keys.append((group, value))
    logo = g("logo_level")
    if logo and logo != "unknown":
        keys.append(("logo", logo))
    return keys


def _sigmoid(z):
    return 1 / (1 + np.exp(-z))


@dataclass
class FeatureSpace:
    vocab: dict[tuple[str, str], int]
    row: dict[int, int]  # product id -> row in X
    X: np.ndarray
    pca_mean: np.ndarray | None
    pca_components: np.ndarray | None
    pca_scale: np.ndarray | None
    backend: str
    d: int

    @property
    def img_slice(self) -> slice:
        return slice(1 + len(self.vocab), 1 + len(self.vocab) + PCA_DIM)

    @property
    def bias(self) -> int:
        return self.d - 1

    def project_image(self, vec: np.ndarray | None) -> np.ndarray:
        out = np.zeros(PCA_DIM)
        if vec is None or self.pca_components is None or len(vec) != self.pca_components.shape[1]:
            return out
        return ((vec - self.pca_mean) @ self.pca_components.T / self.pca_scale) * IMG_SCALE

    def vector(self, keys: list[tuple[str, str]], prior: float = 0.0, image: np.ndarray | None = None) -> np.ndarray:
        x = np.zeros(self.d)
        x[0] = prior
        for k in keys:
            i = self.vocab.get(k)
            if i is not None:
                x[1 + i] = 1.0
        x[self.img_slice] = self.project_image(image)
        x[self.bias] = 1.0
        return x

    def group_mask(self, groups: set[str]) -> np.ndarray:
        mask = np.zeros(self.d)
        for (group, _), i in self.vocab.items():
            if group in groups:
                mask[1 + i] = 1.0
        if "img" in groups:
            mask[self.img_slice] = 1.0
        return mask

    def feature_name(self, j: int) -> tuple[str, str]:
        if j == 0:
            return ("prior", "your written preferences")
        if j == self.bias:
            return ("bias", "baseline")
        if j >= self.img_slice.start:
            return ("img", f"visual pattern {j - self.img_slice.start + 1}")
        for k, i in self.vocab.items():
            if 1 + i == j:
                return k
        return ("?", "?")


_cache_lock = threading.Lock()
_space_cache: dict[str, FeatureSpace] = {}


def build_feature_space(db: Session, taste: dict, backend: str) -> FeatureSpace:
    """Feature matrix for every product. Cached until products, taste profile or image model change."""
    from .recommender import rules_component

    n, last = db.query(func.count(Product.id), func.max(Product.updated_at)).one()
    key = hashlib.sha1(
        json.dumps([n, str(last), backend, taste, PRIOR_SCALE], sort_keys=True, default=str).encode()
    ).hexdigest()
    with _cache_lock:
        if key in _space_cache:
            return _space_cache[key]
    products = db.query(Product).all()
    vocab_keys = sorted({k for p in products for k in attr_keys(p)})
    # Wardrobe attributes extend the vocabulary so owned pieces can inform taste.
    vocab_keys = sorted(set(vocab_keys) | {k for w in db.query(WardrobeItem) for k in attr_keys(w)})
    vocab = {k: i for i, k in enumerate(vocab_keys)}
    d = 1 + len(vocab) + PCA_DIM + 1  # prior, attributes, image PCs, bias

    ids = [p.id for p in products]
    vecs = {
        p.id: emb.from_bytes(p.image_embedding)
        for p in products
        if p.image_embedding is not None and p.embedding_backend == backend
    }
    pca_mean = pca_components = pca_scale = None
    if len(vecs) > PCA_DIM + 1:
        E = np.vstack(list(vecs.values()))
        pca_mean = E.mean(0)
        _, _, vt = np.linalg.svd(E - pca_mean, full_matrices=False)
        pca_components = vt[:PCA_DIM]
        proj = (E - pca_mean) @ pca_components.T
        pca_scale = proj.std(0) + 1e-6

    space = FeatureSpace(
        vocab,
        {pid: r for r, pid in enumerate(ids)},
        np.zeros((len(ids), d)),
        pca_mean,
        pca_components,
        pca_scale,
        backend,
        d,
    )
    for r, p in enumerate(products):
        rules, _ = rules_component(p, taste)
        space.X[r] = space.vector(attr_keys(p), prior=(rules - 0.5) * PRIOR_SCALE, image=vecs.get(p.id))
    with _cache_lock:
        _space_cache.clear()  # keep only the current space
        _space_cache[key] = space
    return space


@dataclass
class Posterior:
    space: FeatureSpace
    mean: np.ndarray
    precision: np.ndarray
    n_obs: int

    def z(self, pids: list[int]) -> tuple[np.ndarray, np.ndarray]:
        """Posterior mean and s.d. of the logit for each product."""
        X = self.space.X[[self.space.row[p] for p in pids]]
        return X @ self.mean, np.sqrt((X**2) @ (1 / self.precision))

    def sample_z(self, pids: list[int], rng: np.random.Generator, scale: float = 0.5) -> np.ndarray:
        w = self.mean + scale * rng.standard_normal(self.space.d) / np.sqrt(self.precision)
        return self.space.X[[self.space.row[p] for p in pids]] @ w

    def explain(self, pid: int, top: int = 3) -> tuple[list[dict], list[dict]]:
        """Largest positive and negative feature contributions to this item's predicted appeal."""
        x = self.space.X[self.space.row[pid]]
        contrib = x * self.mean
        sd = np.abs(x) / np.sqrt(self.precision)
        items = []
        for j in np.argsort(-np.abs(contrib)):
            if abs(contrib[j]) < 0.05:
                break
            if j == self.space.bias:
                continue
            group, label = self.space.feature_name(int(j))
            items.append({"group": group, "label": label, "effect": float(contrib[j]), "sd": float(sd[j])})
        return [i for i in items if i["effect"] > 0][:top], [i for i in items if i["effect"] < 0][:top]

    def summary(self, limit: int = 40) -> dict:
        trust_sd = 1 / math.sqrt(self.precision[0])
        feats = []
        for j in range(1, self.space.img_slice.start):  # attribute features only
            sd = 1 / math.sqrt(self.precision[j])
            if abs(self.mean[j]) < 0.02 and sd > 0.95 * math.sqrt(PRIOR_VAR_ATTR):
                continue  # nothing learned about this feature yet
            group, label = self.space.feature_name(j)
            feats.append({"group": group, "label": label, "mean": round(float(self.mean[j]), 3), "sd": round(sd, 3)})
        feats.sort(key=lambda f: -abs(f["mean"]))
        return {
            "observations": self.n_obs,
            "prior_trust": {"mean": round(float(self.mean[0]), 3), "sd": round(trust_sd, 3)},
            "features": feats[:limit],
        }


def observations(db: Session, space: FeatureSpace) -> tuple[list[np.ndarray], list[int], list[float]]:
    """Training data from feedback (latest decision per product), inspiration photos and the wardrobe."""
    latest: dict[int, Feedback] = {}
    for fb in db.query(Feedback).order_by(Feedback.created_at):
        if fb.product_id is None or fb.action == "skip":
            continue
        if fb.action == "unsave":
            latest.pop(fb.product_id, None)
            continue
        latest[fb.product_id] = fb
    rows, ys, ws = [], [], []
    for pid, fb in latest.items():
        if fb.action not in LABEL or pid not in space.row:
            continue
        reasons = set(fb.reasons or [])
        if fb.action == "dislike" and reasons and reasons <= NON_TASTE_REASONS:
            continue  # value/redundancy/construction judgements are not taste
        y, w = LABEL[fb.action]
        x = space.X[space.row[pid]].copy()
        if fb.action == "dislike" and reasons - NON_TASTE_REASONS:
            groups = set().union(*(REASON_GROUPS.get(r, set()) for r in reasons))
            x *= space.group_mask(groups)  # learn only about what the reason names
        rows.append(x)
        ys.append(y)
        ws.append(w)
    for img in db.query(InspirationImage).filter(InspirationImage.embedding.isnot(None)):
        if img.embedding_backend == space.backend:
            rows.append(space.vector([], image=emb.from_bytes(img.embedding)))
            ys.append(1 if img.polarity > 0 else 0)
            ws.append(1.0)
    for item in db.query(WardrobeItem):
        rating = item.rating or 3
        if rating == 3:
            continue
        image = emb.from_bytes(item.embedding) if item.embedding_backend == space.backend else None
        rows.append(space.vector(attr_keys(item), image=image))
        ys.append(1 if rating > 3 else 0)
        ws.append(0.5)
    # Positive-unlabelled background: weak "typical, not chosen" observations from undecided items.
    if rows:
        undecided = [pid for pid in space.row if pid not in latest]
        if undecided:
            rng = np.random.default_rng(len(rows))
            pick = rng.choice(len(undecided), size=min(BACKGROUND_SAMPLE, len(undecided)), replace=False)
            for j in pick:
                rows.append(space.X[space.row[undecided[j]]])
                ys.append(0)
                ws.append(BACKGROUND_WEIGHT / len(pick))
    return rows, ys, ws


def fit(db: Session, space: FeatureSpace) -> Posterior:
    m0 = np.zeros(space.d)
    m0[0] = PRIOR_MEAN_TRUST
    q0 = np.full(space.d, 1 / PRIOR_VAR_ATTR)
    q0[space.img_slice] = 1 / PRIOR_VAR_IMG
    q0[0] = 1 / PRIOR_VAR_TRUST
    q0[space.bias] = 1 / PRIOR_VAR_BIAS
    rows, ys, ws = observations(db, space)
    if not rows:
        return Posterior(space, m0, q0, 0)
    n_real = sum(1 for w in ws if w >= 0.3)
    A, y, c = np.vstack(rows), np.array(ys, float), np.array(ws)

    def loss(w):
        z = A @ w
        p = _sigmoid(z)
        value = 0.5 * np.sum(q0 * (w - m0) ** 2) + np.sum(c * (np.logaddexp(0, z) - y * z))
        return value, q0 * (w - m0) + A.T @ (c * (p - y))

    w = minimize(loss, m0.copy(), jac=True, method="L-BFGS-B").x
    p = _sigmoid(A @ w)
    return Posterior(space, w, q0 + (A**2).T @ (c * p * (1 - p)), n_real)
