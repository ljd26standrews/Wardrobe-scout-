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

Observations follow the feedback contract (approval_data, "aesthetic-v2"): what you said about the look. Love
(y=1, weight 2), like (1, 1; also "like the look, not now"), dislike (0, 1). A purchase, a maybe, and a dislike
only for price, size, owning something like it, quality or the listing are not about the look and teach nothing;
they do not erase an earlier like either. (The earlier "approval-v1" reading, kept for comparison, also learned from
purchases (1, 1.5) and maybes (1, 0.3).)
Positive-unlabelled learning: a small *background* sample of undecided catalogue items is added as weak
label-0 observations. It does not claim you dislike them; it lets the model learn what distinguishes the
items you choose from a typical item (without it, a run of loves of yellow pieces barely moved the yellow
weight). Its total weight equals your positive evidence, capped at BACKGROUND_WEIGHT: with nothing chosen
yet there is nothing to contrast, and a lone colour rejection used to push trousers and cotton down. The
sample is a stable hash order of the undecided items. Skips are still never used.
Rejection reasons mask the observation to the attribute group they name, so "dislike the colour" only
moves colour weights. "Too expensive", "already own", "construction" and "this specific item" never update
taste (the specific item and its near-duplicates are hidden by the feed instead).
One garment listed by several sellers (a product family, ``Product.group_id``) counts as one decision, and
its other listings are never used as background.
Inspiration photos, anti-inspiration photos and wardrobe pieces you rate highly are image/attribute
observations too. Skips teach nothing.

Uncertainty is used for exploration (Thompson sampling) and shown in explanations.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from scipy import sparse
from scipy.linalg import cho_solve, solve_triangular
from scipy.optimize import minimize
from sqlalchemy.orm import Session

from ..models import Feedback, InspirationImage, Product, WardrobeItem
from . import embeddings as emb
from .approval_data import LABEL_VERSION, WITHDRAW, reading
from .provenance import HeldOut, Identity, live_families_by_url, norm_url

# Recorded with every feed served (impression log), so evaluations can tell model versions apart.
# s1: full-covariance posterior, logo-only logo rejections, new reasons; s2: aesthetic-v2 reading of decisions
ENGINE_VERSION = "bayes-2026.10-s2"
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
MIN_PCA_PRODUCTS = 3  # photographed products needed before image components exist (was 18)
BACKGROUND_SCALES_WITH_POSITIVES = True  # ablation switch (eval/simulate.py): False = full weight always
# Colour × garment-type combinations ("black hoodies" but not "black trousers"), selective M7. Off unless the
# simulation shows they help someone and harm no one (eval/RESULTS.md). Their prior is much tighter than a single
# attribute's, so a combination moves only with repeated evidence; no rejection reason teaches one on its own.
INTERACTIONS = False
PRIOR_VAR_COMBO = 0.05
COMBO = "combo"
PRIOR_SCALE = 2.0  # written-profile score (0..1) -> logit units: (score - 0.5) * PRIOR_SCALE. Calibrated on seed 1:
# 2.0 vs 4.0 gave up a little when the profile is right (0.95→0.89) but gained when it is partly or
# wholly wrong (0.46→0.54, 0.16→0.23), the best average. Held-out seeds are reported in eval/RESULTS.md.

LABEL = {"love": (1, 2.0), "like": (1, 1.0), "dislike": (0, 1.0)}  # (y, weight) per decision about the look
LABEL_V1 = {"love": (1, 2.0), "like": (1, 1.0), "purchased": (1, 1.5), "maybe": (1, 0.3), "dislike": (0, 1.0)}
REASON_GROUPS = {
    "colour": {"colour"},
    "fabric": {"fibre"},
    "logo": {"logo"},  # a loud logo is not a verdict on the brand
    "silhouette": {"fit", "category", "img"},
}
# Reasons that say nothing about general taste. "This specific item" is about that one piece: the feed hides it
# and its near-duplicates (recommender suppression), but no colour, shape or style weight learns from it.
NON_TASTE_REASONS = {
    "too_expensive", "already_own", "construction", "specific_item",
    "wrong_size", "quality_unclear",  # buying and quality, not the look (approval_data's contract)
    "wrong_category",  # a data error (the listing's category was wrong), not taste
    "too_repetitive",  # a feed problem (too much of the same), not taste
}  # fmt: skip
# "Not my style" is a general taste rejection: learned like a dislike given no specific reason.
GENERAL_REASONS = {"not_my_style"}

GROUP_LABEL = {
    "colour": "colour",
    "category": "garment type",
    "fibre": "material",
    "fit": "fit",
    "logo": "branding",
    "brand": "brand/style",
    COMBO: "colour and garment type together",
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
    if INTERACTIONS and g("category"):
        keys += [(COMBO, f"{c}|{g('category')}") for c in (g("colours") or [])]
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
    key: str = ""  # identifies the data the space was built from (cache key)

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
        k = len(self.pca_components)
        out[:k] = ((vec - self.pca_mean) @ self.pca_components.T / self.pca_scale) * IMG_SCALE
        return out

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
    """Feature matrix for every product. Cached until products (their content: services/revisions.py), wardrobe,
    taste profile or image model change; a decision or a piece being marked as seen does not rebuild it."""
    from . import revisions
    from .recommender import rules_component

    products_rev = revisions.current(db, "products")
    wardrobe_keys = sorted({k for w in db.query(WardrobeItem) for k in attr_keys(w)})
    history_keys = sorted({k for fb in _orphaned_feedback(db) for k in attr_keys(fb.snapshot)})
    key = hashlib.sha1(
        json.dumps(
            [products_rev, backend, taste, PRIOR_SCALE, INTERACTIONS, wardrobe_keys, history_keys],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    with _cache_lock:
        if key in _space_cache:
            return _space_cache[key]
    products = db.query(Product).all()
    vocab_keys = {k for p in products for k in attr_keys(p)}
    # Wardrobe attributes, and those of decided products since deleted, extend the vocabulary so owned
    # pieces and past decisions can inform taste.
    vocab_keys = sorted(vocab_keys | set(wardrobe_keys) | set(history_keys))
    vocab = {k: i for i, k in enumerate(vocab_keys)}
    d = 1 + len(vocab) + PCA_DIM + 1  # prior, attributes, image PCs, bias

    ids = [p.id for p in products]
    vecs = {
        p.id: emb.from_bytes(p.image_embedding)
        for p in products
        if p.image_embedding is not None and p.embedding_backend == backend
    }
    pca_mean = pca_components = pca_scale = None
    if len(vecs) >= MIN_PCA_PRODUCTS:  # a small catalogue gets fewer components, not none
        E = np.vstack(list(vecs.values()))
        pca_mean = E.mean(0)
        _, _, vt = np.linalg.svd(E - pca_mean, full_matrices=False)
        pca_components = vt[: min(PCA_DIM, len(vecs) - 1)]
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
        key,
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
    """Laplace posterior N(mean, H⁻¹) over the weights, with the *full* precision matrix H = chol·cholᵀ.

    Correlated features (a colour, a category and the image components that go with them) make the
    marginal and predictive uncertainty differ from what a diagonal approximation gives, so item-level
    uncertainty is computed from the full covariance."""

    space: FeatureSpace
    mean: np.ndarray
    precision: np.ndarray  # diagonal of H (kept for compatibility; not the marginal precision)
    n_obs: int
    chol: np.ndarray | None = None  # lower Cholesky factor of H
    _cov: np.ndarray | None = None

    @property
    def cov(self) -> np.ndarray:
        """Posterior covariance H⁻¹ (computed once)."""
        if self._cov is None:
            L = self.chol if self.chol is not None else np.diag(np.sqrt(self.precision))
            self._cov = cho_solve((L, True), np.eye(self.space.d))
        return self._cov

    @property
    def marginal_sd(self) -> np.ndarray:
        return np.sqrt(np.diag(self.cov))

    def rows(self, pids: list[int]) -> np.ndarray:
        return self.space.X[[self.space.row[p] for p in pids]]

    def mean_logit(self, pids: list[int]) -> np.ndarray:
        """Raw compatibility: the posterior mean of the logit (not a probability)."""
        return self.rows(pids) @ self.mean if pids else np.zeros(0)

    def logit_sd(self, X: np.ndarray, chunk: int = 4096) -> np.ndarray:
        """Posterior s.d. of x·w for each row: sqrt(xᵀ H⁻¹ x). Rows are mostly one-hot, so the product is
        computed sparsely, in chunks (fast for tens of thousands of items)."""
        out = np.empty(len(X))
        cov = self.cov
        for i in range(0, len(X), chunk):
            block = X[i : i + chunk]
            v = np.asarray((sparse.csr_matrix(block) @ cov) * block).sum(axis=1)
            out[i : i + chunk] = np.sqrt(np.maximum(v, 0.0))
        return out

    def z(self, pids: list[int]) -> tuple[np.ndarray, np.ndarray]:
        """Posterior mean and s.d. of the logit for each product."""
        X = self.rows(pids)
        return X @ self.mean, self.logit_sd(X)

    def sample_weights(self, rng: np.random.Generator, scale: float = 1.0) -> np.ndarray:
        """A draw from the posterior (scaled), using the full covariance."""
        eps = rng.standard_normal(self.space.d)
        if self.chol is None:
            return self.mean + scale * eps / np.sqrt(self.precision)
        return self.mean + scale * solve_triangular(self.chol.T, eps, lower=False)

    def sample_z(self, pids: list[int], rng: np.random.Generator, scale: float = 0.5) -> np.ndarray:
        return self.rows(pids) @ self.sample_weights(rng, scale)

    def explain(self, pid: int, top: int = 3) -> tuple[list[dict], list[dict]]:
        """Largest positive and negative feature contributions to this item's predicted appeal."""
        x = self.space.X[self.space.row[pid]]
        contrib = x * self.mean
        sd = np.abs(x) * self.marginal_sd
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
        msd = self.marginal_sd
        trust_sd = float(msd[0])
        feats = []
        for j in range(1, self.space.img_slice.start):  # attribute features only
            sd = float(msd[j])
            group, label = self.space.feature_name(j)
            if abs(self.mean[j]) < 0.02 and sd > 0.95 * math.sqrt(
                PRIOR_VAR_COMBO if group == COMBO else PRIOR_VAR_ATTR
            ):
                continue  # nothing learned about this feature yet
            feats.append({"group": group, "label": label, "mean": round(float(self.mean[j]), 3), "sd": round(sd, 3)})
        feats.sort(key=lambda f: -abs(f["mean"]))
        return {
            "observations": self.n_obs,
            "prior_trust": {"mean": round(float(self.mean[0]), 3), "sd": round(trust_sd, 3)},
            "features": feats[:limit],
        }


def _orphaned_feedback(db: Session) -> list[Feedback]:
    """Decisions about products since deleted, which kept a snapshot of the product's attributes."""
    return [fb for fb in db.query(Feedback).filter(Feedback.product_id.is_(None)) if fb.snapshot]


def observations(db: Session, space: FeatureSpace) -> tuple[list[np.ndarray], list[int], list[float], int]:
    """Training data from feedback (latest decision per product), inspiration photos and the wardrobe.

    A decision about a product that has since been deleted still counts, learned from the attributes
    recorded with it (its photo is gone, so it teaches nothing visual). Also returns how many observations
    are real (decisions, inspiration, rated wardrobe pieces) rather than background."""
    rows, ys, ws, n_real, _ = observations_with_families(db, space)
    return rows, ys, ws, n_real


def family_key(family: tuple) -> str:
    """The garment-family key used by approval labels ("g<group>", "p<product>", "d<url>")."""
    kind, value = family
    if kind == "group":
        return f"g{value}"
    if isinstance(value, tuple):  # ("deleted", source url)
        return f"d{value[1]}"
    return f"p{value}"


def _latest_decisions(db: Session, held: HeldOut, before: datetime | None, version: str):
    """The latest decision about the look per listing (a deleted listing: per address), leaving out what ``held``
    covers, and a function giving each one's garment family."""
    group_of = dict(db.query(Product.id, Product.group_id).filter(Product.group_id.isnot(None)))
    query = db.query(Feedback)
    if before is not None:
        query = query.filter(Feedback.created_at < before)
    latest: dict[int | tuple, Feedback] = {}
    for fb in query.order_by(Feedback.created_at, Feedback.id):
        if fb.product_id is not None:
            key: int | tuple = fb.product_id
        elif fb.snapshot:
            key = ("deleted", fb.snapshot.get("source_url") or fb.id)
        else:
            continue
        if held and held.covers_feedback(fb):
            continue
        said = reading(fb.action, fb.reasons, version)  # the latest decision about the look counts
        if said == WITHDRAW:
            latest.pop(key, None)
        elif said is not None:
            latest[key] = fb
    # A decision about a deleted listing belongs to the garment now listed at its address, if any (a re-import).
    relinked = live_families_by_url(db, [k[1] for k in latest if isinstance(k, tuple) and isinstance(k[1], str)])

    def family(key: int | tuple) -> str:
        if isinstance(key, tuple):
            url = key[1]
            return relinked.get(norm_url(url) or "", f"d{url}") if isinstance(url, str) else f"d{url}"
        return f"g{group_of[key]}" if key in group_of else f"p{key}"

    return latest, family


def teaches(fb: Feedback, version: str = LABEL_VERSION) -> tuple[int, float, list[str] | None] | None:
    """What one decision teaches the taste model: (label, weight, the feature groups it may move, None for all), or
    None (nothing: a non-taste rejection, or an action the reading does not learn from)."""
    labels = LABEL if version != "approval-v1" else LABEL_V1
    if fb.action not in labels:
        return None
    reasons = set(fb.reasons or [])
    if fb.action == "dislike" and reasons and reasons <= NON_TASTE_REASONS:
        return None  # value/redundancy/construction/this-item-only judgements are not taste
    y, w = labels[fb.action]
    specific = reasons - NON_TASTE_REASONS - GENERAL_REASONS
    if fb.action == "dislike" and specific:
        return y, w, sorted(set().union(*(REASON_GROUPS.get(r, set()) for r in specific)))
    return y, w, None


def _digest(value) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def content_fingerprint(obj) -> str:
    """What a piece contributes as features: its attributes and its photo vector (with how it was computed). Being
    marked as seen, a price or a description change does not change it."""
    g = obj.get if isinstance(obj, dict) else (lambda k, _o=obj: getattr(_o, k, None))
    vec = g("image_embedding") if not isinstance(obj, dict) else None
    if vec is None and not isinstance(obj, dict):
        vec = g("embedding")
    return _digest([sorted(attr_keys(obj)), hashlib.sha1(vec).hexdigest() if vec else None,
                    g("embedding_backend")])  # fmt: skip


def training_fingerprints(db: Session, version: str = LABEL_VERSION) -> list[str]:
    """For every decision the taste model learns from now: "<decision id>:<fingerprint>:<listing>". The fingerprint
    covers what it contributes: its label and weight, the features it may move (a reason narrows them), its garment
    family (listings of one garment share a weight) and the content of the piece. Editing a reason, re-analysing the
    piece's photo, regrouping it or changing its attributes changes it; a newer decision about the listing replaces
    the entry; marking a piece as seen changes nothing."""
    from sqlalchemy.orm import load_only

    latest, family = _latest_decisions(db, HeldOut(), None, version)
    pids = [k for k in latest if isinstance(k, int)]
    content: dict[int, str] = {}
    cols = (Product.id, Product.colours, Product.materials, Product.fit_tags, Product.category, Product.brand,
            Product.logo_level, Product.image_embedding, Product.embedding_backend)  # fmt: skip
    for i in range(0, len(pids), 500):
        for p in db.query(Product).options(load_only(*cols)).filter(Product.id.in_(pids[i : i + 500])):
            content[p.id] = content_fingerprint(p)
    out = []
    for key, fb in latest.items():
        taught = teaches(fb, version)
        if taught is None:
            continue
        piece = content.get(key) if isinstance(key, int) else content_fingerprint(fb.snapshot or {})
        listing = f"p{key}" if isinstance(key, int) else f"d{key[1]}"
        out.append(f"{fb.id}:{_digest([*taught, family(key), piece])}:{listing}")
    return sorted(out)


def observations_with_families(
    db: Session,
    space: FeatureSpace,
    *,
    exclude: frozenset[str] | HeldOut = frozenset(),
    before: datetime | None = None,
    version: str = LABEL_VERSION,
) -> tuple[list, list, list, int, list]:
    """``observations`` plus, for each row, the garment family of the decision it came from (None for
    inspiration, wardrobe and background rows).

    ``exclude``: what is held out (a provenance.HeldOut, or garment family keys "g…", "p…", "d…", resolved by
    services/provenance.py), left out entirely, as if never decided: decisions about those garments (under any of
    their listings or addresses), wardrobe pieces linked to them and inspiration photos that are their photographs.
    Their listings become undecided, exactly like candidates being served. ``before``: only what was known before
    that time. Either way the background sample and its weight are built from what remains, so a left-out label
    cannot leak back in through them (out-of-fold calibration, rolling-origin evaluation)."""
    if isinstance(exclude, HeldOut):
        held = exclude
    else:
        held = Identity(db).resolve(families=exclude) if exclude else HeldOut()
    latest, family = _latest_decisions(db, held, before, version)
    group_of = dict(db.query(Product.id, Product.group_id).filter(Product.group_id.isnot(None)))
    decisions = []  # (family, x, y, w)
    for pid, fb in latest.items():
        if isinstance(pid, int) and pid not in space.row:
            continue
        taught = teaches(fb, version)
        if taught is None:
            continue
        y, w, groups = taught
        x = space.X[space.row[pid]].copy() if isinstance(pid, int) else space.vector(attr_keys(fb.snapshot))
        if groups is not None:
            x *= space.group_mask(set(groups))  # learn only about what the reason names
        decisions.append((family(pid), x, y, w))
    # One garment listed by several sellers is one piece of evidence: its listings share one decision's weight.
    family_size = Counter(f for f, *_ in decisions)
    rows, ys, ws, families = [], [], [], []
    for fam, x, y, w in decisions:
        rows.append(x)
        ys.append(y)
        ws.append(w / family_size[fam])
        families.append(fam)
    n_real = len(family_size)
    for img in db.query(InspirationImage).filter(InspirationImage.embedding.isnot(None)):
        if img.id in held.inspiration_ids:
            continue
        if img.embedding_backend == space.backend and (before is None or img.added_at < before):
            rows.append(space.vector([], image=emb.from_bytes(img.embedding)))
            ys.append(1 if img.polarity > 0 else 0)
            ws.append(1.0)
            n_real += 1
    for item in db.query(WardrobeItem):
        rating = item.rating or 3
        if rating == 3 or (before is not None and item.added_at >= before) or item.id in held.wardrobe_ids:
            continue
        image = emb.from_bytes(item.embedding) if item.embedding_backend == space.backend else None
        rows.append(space.vector(attr_keys(item), image=image))
        ys.append(1 if rating > 3 else 0)
        ws.append(0.5)
        n_real += 1
    decided = {family(k) for k in latest}
    rows, ys, ws, n_real = _with_background(space, decided, group_of, rows, ys, ws, n_real)
    return rows, ys, ws, n_real, families + [None] * (len(rows) - len(families))


def _stable_rank(pid: int) -> int:
    return int(hashlib.blake2b(str(pid).encode(), digest_size=8).hexdigest(), 16)


def _with_background(space, decided, group_of, rows, ys, ws, n_real):
    """Positive-unlabelled background: weak "typical, not chosen" observations from undecided items.

    It only makes sense against something you chose: with no positive decisions it would just teach that
    every common attribute is disliked (a single colour rejection moved trousers and cotton). So its total
    weight grows with your positive evidence, up to BACKGROUND_WEIGHT. The sample is a stable function of
    the undecided set (a hash order), so one more decision changes it by one item, not entirely. Listings of
    a garment you decided on (``decided``: family keys) are never background: they are the same piece."""
    positive = sum(w for w, y in zip(ws, ys, strict=True) if y == 1)
    if BACKGROUND_SCALES_WITH_POSITIVES:
        total = min(BACKGROUND_WEIGHT, positive)
    else:  # the earlier rule: full weight as soon as there is any observation
        total = BACKGROUND_WEIGHT if rows else 0.0
    undecided = [pid for pid in space.row if (f"g{group_of[pid]}" if pid in group_of else f"p{pid}") not in decided]
    pick = sorted(undecided, key=_stable_rank)[:BACKGROUND_SAMPLE]
    if total > 0 and pick:
        for pid in pick:
            rows.append(space.X[space.row[pid]])
            ys.append(0)
            ws.append(total / len(pick))
    return rows, ys, ws, n_real


_post_lock = threading.Lock()
_post_cache: OrderedDict[str, Posterior] = OrderedDict()  # a few recent fits, keyed by their exact inputs


def prior(space: FeatureSpace) -> tuple[np.ndarray, np.ndarray]:
    """Prior mean and (diagonal) prior precision of the weights."""
    m0 = np.zeros(space.d)
    m0[0] = PRIOR_MEAN_TRUST
    q0 = np.full(space.d, 1 / PRIOR_VAR_ATTR)
    q0[space.img_slice] = 1 / PRIOR_VAR_IMG
    for (group, _), i in space.vocab.items():
        if group == COMBO:
            q0[1 + i] = 1 / PRIOR_VAR_COMBO
    q0[0] = 1 / PRIOR_VAR_TRUST
    q0[space.bias] = 1 / PRIOR_VAR_BIAS
    return m0, q0


def fit(db: Session, space: FeatureSpace) -> Posterior:
    """Fit the taste model to your current evidence. A fit is reused while its inputs are unchanged."""
    return fit_rows(space, *observations(db, space))


def fit_rows(space: FeatureSpace, rows: list, ys: list, ws: list, n_real: int) -> Posterior:
    """Fit on given observation rows (also used for out-of-fold fits by the calibration)."""
    m0, q0 = prior(space)
    A = np.vstack(rows) if rows else np.zeros((0, space.d))
    y, c = np.array(ys, float), np.array(ws, float)
    key = hashlib.sha1(
        b"".join([space.key.encode(), A.tobytes(), y.tobytes(), c.tobytes(), m0.tobytes(), q0.tobytes()])
    ).hexdigest()
    with _post_lock:
        if key in _post_cache:
            _post_cache.move_to_end(key)
            hit = _post_cache[key]
            # The weights depend only on the inputs hashed above; bind them to the caller's space object.
            return hit if hit.space is space else dataclasses.replace(hit, space=space)
    post = _fit(space, m0, q0, A, y, c, n_real)
    with _post_lock:
        _post_cache[key] = post
        while len(_post_cache) > 8:
            _post_cache.popitem(last=False)
    return post


def _fit(space, m0, q0, A, y, c, n_real) -> Posterior:
    if not len(A):
        return Posterior(space, m0, q0, 0, chol=np.diag(np.sqrt(q0)))

    def loss(w):
        z = A @ w
        p = _sigmoid(z)
        value = 0.5 * np.sum(q0 * (w - m0) ** 2) + np.sum(c * (np.logaddexp(0, z) - y * z))
        return value, q0 * (w - m0) + A.T @ (c * (p - y))

    w = minimize(loss, m0.copy(), jac=True, method="L-BFGS-B").x
    p = _sigmoid(A @ w)
    H = np.diag(q0) + (A.T * (c * p * (1 - p))) @ A
    return Posterior(space, w, np.diag(H).copy(), n_real, chol=np.linalg.cholesky(H))
