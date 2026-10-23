"""Selective recommendation: recommend a piece only when it is likely enough that you will approve of it.

See docs/selective-recommendation/02-design.md. In short:

* Every candidate has a raw compatibility m (the taste model's posterior mean logit) and an uncertainty s (the
  posterior s.d. of that logit). Neither is a probability.
* With an active calibration (calibration.py), m becomes an approval estimate p̂ and a conservative bound
  p_low(γ). A piece passes a mode (t, γ) iff p_low(γ) ≥ t: under the model you can be γ-sure that its approval
  probability is at least t. The 80/90/95% targets are aspirations until your own outcomes confirm them
  (``performance``); nothing here claims they are met.
* Eligibility runs first and every exclusion keeps its reason: near-duplicates of pieces you rejected and pieces
  with independent evidence of poor quality never qualify; the strict modes also require an analysed photo and
  leave out sellers with recorded concerns, unproven pieces above the highest price you judged too much for one of
  their kind, and pieces like one you said you already own (never a whole category: hiding a category is an
  explicit setting). Unknown quality is not poor quality.
* The gate is never relaxed to fill the feed. Without a calibration, Balanced shows the taste ranking labelled
  as not yet calibrated, and the strict modes show nothing and say what is needed. If the model or calibration
  fails, the same degraded behaviour applies: nothing is ever presented as high-confidence without a calibration.
* Learning picks are a separate, labelled feed of plausible but uncertain pieces, Poisson-sampled with logged
  inclusion probabilities. They never enter the main feed.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from sqlalchemy.orm import Session

from ..logging_setup import get_logger
from ..models import FeedRequest, Seller
from . import calibration
from . import embeddings as emb
from . import preference_model as pm
from .approval_data import rated_outcomes
from .recommender import Profile, Ranked, above_judged_price, owned_like
from .settings_store import get_setting

log = get_logger(__name__)

MODES = ("balanced", "high", "extreme")
MODE_LABEL = {"balanced": "Balanced", "high": "High confidence", "extreme": "Extremely selective"}
STRICT = frozenset({"high", "extreme"})
POOR_QUALITY_BELOW = 40  # independent quality evidence scoring below this is known poor quality
QUALITY_SUPPORTS_PRICE = 60  # quality evidence at or above this justifies a price above your ceiling
MAX_RUN = 2  # diversity: at most this many consecutive pieces from one category, or one seller
MAIN_LIMIT = 24  # the Discover page's main feed size; Learning picks never repeat its pieces
MIN_PERFORMANCE_N = 10  # rated picks needed before an observed approval rate is reported
# Learning picks judge plausibility optimistically, at the logit's mean plus this many s.d.: a piece the model knows
# little about can still be tried, which is how a taste far from the written profile gets found (the selective
# simulation's contrarian persona never did with the plain estimate).
LEARNING_OPTIMISM = 1.0
LEARNING_POOL_MIN = 3  # × the number of picks: the plausibility floor relaxes before learning would stall

EXCLUSIONS = {
    "near_duplicate": "Near-identical to something you rejected",
    "poor_quality": "Independent evidence shows poor quality",
    "no_quality_evidence": "No independent quality evidence (required in your settings)",
    "no_photo_analysis": "No analysed garment photo yet",
    "seller_concerns": "The seller has recorded concerns",
    "price_ceiling": "Above the price you judged too much for an unproven piece of this kind",
    "already_own": "Like a piece you said you already own",
}


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.asarray(x, float)))


def thresholds(cfg: dict, mode: str) -> tuple[float, float]:
    """(target t, confidence γ) for a mode."""
    return float(cfg[f"{mode}_target"]), float(cfg[f"{mode}_confidence"])


def trial_arm(cfg: dict, day: date) -> str | None:
    """The opt-in prospective trial (a daily switchback): on alternate days Discover serves the earlier ranked
    feed instead of the gate, so the two can be compared on what you then rate. None when the trial is off."""
    if not cfg.get("trial") or not cfg.get("enabled", True):
        return None
    return "ranked" if day.toordinal() % 2 else "selective"


def resolve_mode(cfg: dict, mode: str | None) -> str:
    return mode if mode in MODES else (cfg.get("mode") if cfg.get("mode") in MODES else "balanced")


# ----------------------------------------------------------------- data
@dataclass
class Candidate:
    """One piece as the gate sees it. ``estimate`` and ``bound`` exist only with an active calibration."""

    ranked: Ranked
    logit: float  # m: raw compatibility (not a probability)
    sd: float | None = None  # s: posterior s.d. of the logit (computed only where it can matter)
    estimate: float | None = None  # p̂: calibrated approval estimate
    bound: dict[str, float] = field(default_factory=dict)  # conservative bound per mode, at its confidence
    passes: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(MODES, False))
    excluded: dict[str, str | None] = field(default_factory=dict)  # per mode: the first eligibility failure
    propensity: float | None = None  # Learning picks: the probability this pick was included

    @property
    def product(self):
        return self.ranked.product


@dataclass
class Gate:
    mode: str
    # calibrated | stale (estimates in use; new evidence awaits a refit) | uncalibrated (too few decisions) |
    # pending (a fit is queued or running) | failed (the last fit failed; retried later) | unavailable | off
    state: str
    cfg: dict
    candidates: list[Candidate] = field(default_factory=list)
    calibration: calibration.ApprovalMap | None = None
    progress: dict | None = None  # labels so far and what is needed (without a calibration)
    reason: str | None = None  # why estimates are unavailable
    lifecycle: dict = field(default_factory=dict)  # new evidence since the fit, or why the last one was withdrawn

    @property
    def calibrated(self) -> bool:
        """Estimates are in use (a stale calibration is still valid: nothing it rests on was withdrawn)."""
        return self.state in ("calibrated", "stale")

    def qualified(self) -> dict[str, int]:
        return {m: int(sum(bool(c.passes[m]) for c in self.candidates)) for m in MODES}

    def exclusions(self, mode: str | None = None) -> dict[str, int]:
        mode = mode or self.mode
        return dict(Counter(c.excluded.get(mode) for c in self.candidates if c.excluded.get(mode)))

    def summary(self, shown: int, policy: str = "selective") -> dict:
        t, g = thresholds(self.cfg, self.mode) if self.state != "off" else (None, None)
        out = {
            "policy": policy, "mode": self.mode, "mode_label": MODE_LABEL[self.mode], "state": self.state,
            "target": t, "confidence": g, "targets_validated": False, "shown": shown,
            "candidates": len(self.candidates),
            "eligible": {m: sum(c.excluded.get(m) is None for c in self.candidates) for m in MODES},
            "qualified": self.qualified() if self.calibrated else None,
            "excluded": {k: {"count": n, "label": EXCLUSIONS[k]} for k, n in self.exclusions().items()},
            "calibration": None, "progress": self.progress, "message": None, **self.lifecycle,
        }  # fmt: skip
        if self.calibration is not None:
            d = self.calibration.diagnostics or {}
            out["calibration"] = {
                "id": self.calibration.id, "created_at": self.calibration.created_at.isoformat(),
                "labels": self.calibration.n_labels, "approvals": self.calibration.n_positive,
                "map": d.get("map"), "auc": d.get("auc_out_of_fold"), "brier": d.get("brier_chosen"),
                "ece": d.get("ece_chosen"), "base_rate": d.get("base_rate"),
            }  # fmt: skip
        out["message"] = message(self, shown, policy)
        return out


# ----------------------------------------------------------------- eligibility (stage B)
def _base_exclusion(r: Ranked, cfg: dict, quality: dict | None) -> str | None:
    """Rules for every mode, and for Learning picks."""
    if r.suppressed:
        return "near_duplicate"
    qs = (quality or {}).get("quality_score")
    if qs is not None and qs < POOR_QUALITY_BELOW:
        return "poor_quality"
    if cfg.get("require_quality_evidence") and (quality or {}).get("status", "Unverified") == "Unverified":
        return "no_quality_evidence"
    return None


def _strict_exclusion(
    r: Ranked, cfg: dict, prof: Profile, quality: dict | None, backend: str, seller: Seller | None
) -> str | None:
    """Additional rules for High confidence and Extremely selective."""
    p = r.product
    if p.image_embedding is None or p.embedding_backend != backend:
        return "no_photo_analysis"
    if cfg.get("exclude_seller_concerns", True) and seller is not None and seller.reputation_status == "concerns":
        return "seller_concerns"
    cat = p.category or "other"
    qs = (quality or {}).get("quality_score")
    if (
        cat in prof.price_ceiling  # (no judged price for this kind: nothing to convert)
        and above_judged_price(prof, cat, prof.fx.gbp(p.price, p.currency))
        and (qs is None or qs < QUALITY_SUPPORTS_PRICE)
    ):
        return "price_ceiling"
    if owned_like(prof, p, backend) is not None:
        return "already_own"
    return None


# ----------------------------------------------------------------- the gate (stages C-E)
def evaluate(
    db: Session,
    ranked: list[Ranked],
    prof: Profile,
    mode: str | None = None,
    *,
    quality_lookup: dict[int, dict] | None = None,
    learning: bool | str = False,
) -> Gate:
    """Score every candidate under the active calibration and decide which pass each mode.

    ``learning``: also compute the uncertainty of the Learning-picks pool (True), or only when Balanced has no
    calibration in use ("warmup": its feed then carries a few). Never raises: a failing model or calibration gives
    state "unavailable", which shows nothing as high-confidence."""
    cfg = get_setting(db, "selective")
    mode = resolve_mode(cfg, mode)
    if not cfg.get("enabled", True):
        return Gate(mode, "off", cfg)
    quality_lookup = quality_lookup or {}
    backend = emb.get_image_backend().name
    sellers = {s.id: s for s in db.query(Seller)}
    cands: list[Candidate] = []
    try:
        if get_setting(db, "recommender").get("engine", "bayes") != "bayes":
            raise _Unavailable("approval estimates need the Bayesian taste engine (Settings → Recommender)")
        space = pm.build_feature_space(db, get_setting(db, "taste_profile"), backend)
        post = pm.fit(db, space)
        known = [r for r in ranked if r.product.id in space.row]
        m = post.mean_logit([r.product.id for r in known])
        for r, mi in zip(known, m.tolist(), strict=True):
            p = r.product
            q = quality_lookup.get(p.id)
            base = _base_exclusion(r, cfg, q)
            seller = sellers.get(p.seller_id) if p.seller_id else None
            strict = base or _strict_exclusion(r, cfg, prof, q, backend, seller)
            cands.append(Candidate(r, mi, excluded={"balanced": base, "high": strict, "extreme": strict}))
        cal, state, ev, details = calibration.lifecycle(db)
        amap = None if cal is None else calibration.to_map(cal)
    except Exception as e:  # noqa: BLE001 - the feed must stay usable when the model or calibration fails
        if not isinstance(e, _Unavailable):
            log.exception("selective_gate_unavailable")
        gate = Gate(mode, "unavailable", cfg, reason=str(e) if isinstance(e, _Unavailable) else "internal error")
        gate.candidates = [_plain(r, cfg, quality_lookup.get(r.product.id)) for r in ranked]
        return gate

    if amap is None:
        gate = Gate(mode, state, cfg, cands, progress=ev.suff, lifecycle=details)
        if learning is True or (learning == "warmup" and mode == "balanced"):
            # the pool is chosen on the raw model; its numbers are never shown as probabilities
            _attach_sd(post, [c for c in cands if c.excluded["balanced"] is None])
        return gate

    gate = Gate(mode, state, cfg, cands, calibration=amap, lifecycle=details)
    m = np.array([c.logit for c in cands])
    est = _sigmoid(amap.a * m + amap.b).tolist()
    lowest = min(thresholds(cfg, md)[0] for md in MODES)
    # Uncertainty only where a bound could reach a target, or (Learning picks) for every eligible piece.
    pool = learning is True
    need = [c for c, e in zip(cands, est, strict=True) if e >= lowest or (pool and c.excluded["balanced"] is None)]
    _attach_sd(post, need)
    for c, e in zip(cands, est, strict=True):
        c.estimate = e
    for md in MODES:
        t, g = thresholds(cfg, md)
        if not need:
            break
        _, low = amap.estimate(np.array([c.logit for c in need]), np.array([c.sd for c in need]), confidence=g)
        for c, lo in zip(need, low, strict=True):
            c.bound[md] = float(lo)
            c.passes[md] = bool(c.excluded[md] is None and lo >= t)
    return gate


class _Unavailable(Exception):
    pass


def _plain(r: Ranked, cfg: dict, quality: dict | None) -> Candidate:
    base = _base_exclusion(r, cfg, quality)
    return Candidate(r, float("nan"), excluded={"balanced": base, "high": base, "extreme": base})


def _attach_sd(post: pm.Posterior, cands: list[Candidate]) -> None:
    if cands:
        sd = post.logit_sd(post.rows([c.product.id for c in cands]))
        for c, s in zip(cands, sd, strict=True):
            c.sd = float(s)


# ----------------------------------------------------------------- feeds (stage F)
def diversify(items: list[Candidate], max_run: int = MAX_RUN) -> list[Candidate]:
    """Reorder (never add or drop) so that no more than ``max_run`` consecutive pieces share a category or a
    seller, when the set allows it; otherwise keep the given order."""
    rest = list(items)
    out: list[Candidate] = []

    def run_breaks(c: Candidate) -> bool:
        tail = out[-max_run:]
        if len(tail) < max_run:
            return False
        p = c.product
        return all(t.product.category == p.category for t in tail) or all(
            t.product.seller_id == p.seller_id for t in tail
        )

    while rest:
        j = next((i for i, c in enumerate(rest) if not run_breaks(c)), 0)
        out.append(rest.pop(j))
    return out


def interleave(main: list[Candidate], picks: list[Candidate], rate: float) -> list[Candidate]:
    """Place labelled Learning picks among the ranking, about one in every 1/rate positions."""
    gap = max(3, round(1 / rate)) if rate > 0 else 10**9
    out, j = [], 0
    for c in main:
        out.append(c)
        if j < len(picks) and len(out) % gap == gap - 1:
            out.append(picks[j])
            j += 1
    return out + picks[j:]


def warmup_exploration(gate: Gate, limit: int, rate: float) -> int:
    """Learning picks in the main feed: only while Balanced has no calibration (the feed is labelled as a plain
    taste ranking then), so that a taste far from the written profile is still found. Never in a calibrated feed
    and never in the strict modes."""
    if gate.mode != "balanced" or gate.state not in ("uncalibrated", "pending", "failed"):
        return 0
    eligible = sum(1 for c in gate.candidates if c.excluded["balanced"] is None)
    return round(limit * rate) if eligible > limit else 0


def main_feed(gate: Gate, limit: int) -> list[Candidate]:
    """The pieces this mode recommends, best first. Never relaxed to fill the feed."""
    if gate.state == "off":
        return []
    if gate.calibrated:
        chosen = sorted(
            (c for c in gate.candidates if c.passes[gate.mode]),
            key=lambda c: (-c.bound[gate.mode], -(c.estimate or 0.0), -c.ranked.score),
        )[:limit]
        return diversify(chosen)
    if gate.mode != "balanced":
        return []  # nothing may be presented as high-confidence without a calibration
    # Balanced without a calibration: the taste ranking itself, labelled as such. The feed endpoint adds a few
    # labelled Learning picks to it (warmup_exploration); a calibrated feed is never mixed.
    return diversify([c for c in gate.candidates if c.excluded["balanced"] is None][:limit])


def inclusion_probabilities(w: np.ndarray, k: int) -> np.ndarray:
    """Poisson-sampling inclusion probabilities π_i = min(1, c·w_i), with c chosen so that Σπ = min(k, n)."""
    n = len(w)
    if n == 0 or k <= 0:
        return np.zeros(n)
    if k >= n:
        return np.ones(n)
    w = np.maximum(np.asarray(w, float), 1e-12)
    capped = np.zeros(n, dtype=bool)
    pi = np.zeros(n)
    for _ in range(n):
        free = ~capped
        pi[free] = (k - capped.sum()) * w[free] / w[free].sum()
        pi[capped] = 1.0
        over = free & (pi > 1.0)
        if not over.any():
            break
        capped |= over
    return np.minimum(pi, 1.0)


def permanent_random_number(salt: str, product_id: int) -> float:
    """A uniform number fixed per (salt, piece). The salt changes with every decision, so repeated requests in
    between return the same picks while each request's inclusion probability stays exactly π."""
    h = hashlib.sha256(f"{salt}:{product_id}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2.0**64


def learning_feed(db: Session, gate: Gate, limit: int, *, salt: str) -> tuple[list[Candidate], dict]:
    """Plausible but uncertain pieces that did not pass the current mode, sampled with probability proportional
    to q²·s, where q is the optimistic plausibility (the estimate at the logit's mean + LEARNING_OPTIMISM s.d.) and
    s the logit's uncertainty, more for categories you have rated little. Each pick carries its inclusion
    probability. The pool is the pieces with q at least ``learning_min_estimate``, widened to LEARNING_POOL_MIN
    times the picks wanted when fewer qualify. Learning picks are never presented as recommendations.

    Returns (picks, sampling details for the log). Without a calibration the pool is chosen on the raw model,
    whose numbers are used only to pick and are never shown as probabilities."""
    sampling = {"method": "poisson-prn", "salt": salt, "pool": 0, "expected": 0.0}
    if gate.state in ("off", "unavailable"):
        return [], sampling
    main = {c.product.id for c in main_feed(gate, MAIN_LIMIT)}
    floor = float(gate.cfg["learning_min_estimate"])
    amap = gate.calibration

    def plausibility(c: Candidate) -> float:  # optimistic: could you approve, given what the model doesn't know?
        z = c.logit + LEARNING_OPTIMISM * c.sd
        return float(_sigmoid(amap.a * z + amap.b if amap is not None else z))

    eligible = sorted(
        (c for c in gate.candidates if c.excluded["balanced"] is None and not c.passes[gate.mode]
         and c.product.id not in main and c.sd is not None),
        key=lambda c: -plausibility(c),
    )  # fmt: skip
    pool = [c for c in eligible if plausibility(c) >= floor]
    pool += eligible[len(pool) : LEARNING_POOL_MIN * limit]  # too few plausible pieces: widen rather than stall
    if not pool:
        return [], sampling
    rated = Counter((o.snapshot or {}).get("category") or "other" for o in rated_outcomes(db))
    w = np.array([plausibility(c) ** 2 * c.sd * (1 + 1 / (1 + rated[c.product.category or "other"])) for c in pool])
    pi = inclusion_probabilities(w, limit)
    picks = []
    for c, p in zip(pool, pi, strict=True):
        if permanent_random_number(salt, c.product.id) < p:
            c.propensity = float(p)
            picks.append(c)
    picks.sort(key=lambda c: -plausibility(c))
    sampling.update(pool=len(pool), expected=round(float(pi.sum()), 3))
    return picks, sampling


# ----------------------------------------------------------------- words
def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def message(gate: Gate, shown: int, policy: str = "selective") -> str | None:
    """What the person should be told about this feed (always, when it is empty)."""
    label = MODE_LABEL[gate.mode]
    if gate.state == "off":
        return None
    if gate.state == "unavailable":
        if policy == "learning":
            return "Learning picks are unavailable while approval estimates are unavailable."
        if gate.mode != "balanced":
            return (f"Approval estimates are unavailable right now ({gate.reason}), so nothing can be shown as "
                    f"{label.lower()}. Balanced mode shows your taste ranking meanwhile.")  # fmt: skip
        return f"Approval estimates are unavailable right now ({gate.reason}). This is your taste ranking."
    if gate.state in ("uncalibrated", "pending", "failed"):
        if policy == "learning":
            return None if shown else "No uncertain pieces to learn from right now."
        pr = gate.progress or {}
        withdrawn = gate.lifecycle.get("withdrawn")
        why = f"Earlier estimates were withdrawn: {withdrawn['label']}. " if withdrawn else ""
        if gate.state == "pending":
            need = why + "Your decisions are being calibrated now; estimates appear when that finishes."
        elif gate.state == "failed":
            need = why + "Calibrating your decisions failed; it will be retried in a few minutes."
        else:
            more = max(0, pr.get("needed", 0) - pr.get("labels", 0))
            ya = max(0, pr.get("needed_each", 0) - pr.get("approvals", 0))
            na = max(0, pr.get("needed_each", 0) - pr.get("non_approvals", 0))
            parts = [f"{more} more rated pieces"] if more else []
            parts += [f"{ya} more approvals"] if ya else []
            parts += [f"{na} more rejections"] if na else []
            need = why + "Calibrated estimates need " + " and ".join(parts or ["a few more decisions"]) + "."
        if gate.mode == "balanced":
            return f"Not yet calibrated: this is your taste ranking, not a confidence-filtered feed. {need}"
        return f"{label} needs calibrated approval estimates, so nothing is shown as {label.lower()} yet. {need}"
    if policy == "learning":
        return None if shown else "No uncertain pieces to learn from right now."
    stale = _stale_note(gate) if gate.state == "stale" else ""
    if shown:
        return stale or None
    q = gate.qualified()
    others = [f"{q[m]} under {MODE_LABEL[m]}" for m in MODES if m != gate.mode and q[m]]
    tail = (" " + "; ".join(others) + ".") if others else " Rating Learning picks helps the model become surer."
    return f"No sufficiently strong matches are currently available in {label} mode.{tail}" + (
        f" {stale}" if stale else ""
    )


def _stale_note(gate: Gate) -> str:
    d = gate.lifecycle.get("since") or {}
    n = int(d.get("new_decisions") or 0)
    if n:
        return f"These estimates do not yet include your {n} newest decisions; they are being updated."
    if d.get("catalogue_changed"):
        return "The catalogue has changed a lot since these estimates were made; they are being updated."
    return "Your inspiration or wardrobe ratings changed since these estimates were made; they are being updated."


def card_confidence(gate: Gate, c: Candidate) -> dict:
    """The per-piece confidence block for the client: numbers only with a calibration."""
    out = {"state": gate.state, "mode": gate.mode, "passes": [m for m in MODES if c.passes.get(m)]}
    excl = c.excluded.get(gate.mode)
    out["excluded"] = EXCLUSIONS[excl] if excl else None
    if gate.calibrated and c.estimate is not None:
        t, g = thresholds(gate.cfg, gate.mode)
        out.update(estimate=round(c.estimate, 3), target=t, level=g)
        out["bound"] = round(c.bound[gate.mode], 3) if gate.mode in c.bound else None
    if c.propensity is not None:
        out["learning"] = True
    return out


def log_items(gate: Gate, chosen: list[Candidate], offers: dict) -> list[dict]:
    """The served items for the feed log: position, scores, the gate's numbers and decisions."""
    out = []
    for i, c in enumerate(chosen):
        row = {
            "product_id": c.product.id, "position": i, "score": round(float(c.ranked.score), 4),
            "explore": c.propensity is not None, "family_size": 1 + len(offers.get(c.product.id, [])),
            "m": None if math.isnan(c.logit) else round(c.logit, 4),
            "s": None if c.sd is None else round(c.sd, 4),
            "estimate": None if c.estimate is None else round(c.estimate, 4),
            "bound": None if gate.mode not in c.bound else round(c.bound[gate.mode], 4),
            "passes": [m for m in MODES if c.passes.get(m)],
        }  # fmt: skip
        if c.propensity is not None:
            row["propensity"] = round(c.propensity, 6)
        out.append(row)
    return out


# ----------------------------------------------------------------- observed performance
def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    from scipy.stats import beta

    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
    return lo, hi


def _rate(k: int, n: int) -> dict:
    out = {"rated": n, "approved": k, "rate": None, "interval": None}
    if n >= MIN_PERFORMANCE_N:
        lo, hi = clopper_pearson(k, n)
        out.update(rate=round(k / n, 3), interval=[round(lo, 3), round(hi, 3)])
    return out


def performance(db: Session) -> dict:
    """Observed approval among pieces the feeds actually served and you then rated, by mode.

    "Passed" counts a rated piece under every mode whose gate it passed when served (the modes are nested), so a
    stricter mode's rate comes from fewer pieces. Rates are withheld below MIN_PERFORMANCE_N rated pieces, and
    come with 95% Clopper-Pearson intervals. Pieces shown but never rated are not counted as rejections."""
    outcomes = [o for o in rated_outcomes(db) if o.feed_id]
    feeds = {
        f.id: f for f in db.query(FeedRequest).filter(FeedRequest.id.in_({o.feed_id for o in outcomes}))
    } if outcomes else {}  # fmt: skip
    served = {m: [0, 0] for m in MODES}
    passed = {m: [0, 0] for m in MODES}
    learning = [0, 0]
    uncalibrated = [0, 0]
    trial = {"ranked": [0, 0], "selective": [0, 0]}
    for o in outcomes:
        f = feeds.get(o.feed_id)
        if f is None or getattr(f, "engine_version", None) != pm.ENGINE_VERSION:
            continue
        row = next((it for it in f.items or [] if it.get("product_id") == o.product_id), None)
        if row is None:
            continue
        arm = (f.gate or {}).get("trial")
        if arm in trial and not row.get("explore"):  # the trial compares the two main feeds, not exploration
            trial[arm][0] += o.y
            trial[arm][1] += 1
        policy = getattr(f, "policy", None)
        if policy == "learning" or (policy == "selective" and row.get("propensity") is not None):
            learning[0] += o.y  # Learning picks, in their own tab or labelled in the not-yet-calibrated feed
            learning[1] += 1
            continue
        if policy != "selective":
            continue
        if f.calibration_id is None:
            uncalibrated[0] += o.y
            uncalibrated[1] += 1
            continue
        if f.mode in served:
            served[f.mode][0] += o.y
            served[f.mode][1] += 1
        for md in row.get("passes") or []:
            if md in passed:
                passed[md][0] += o.y
                passed[md][1] += 1
    return {
        "served_in_mode": {m: _rate(*served[m]) for m in MODES},
        "passed_gate": {m: _rate(*passed[m]) for m in MODES},
        "learning_picks": _rate(*learning),
        "uncalibrated_ranking": _rate(*uncalibrated),
        "trial": {arm: _rate(*v) for arm, v in trial.items()},
        "min_rated": MIN_PERFORMANCE_N,
        "note": 'Approved = you liked the look (love or like, including "not now"); a dislike about the look is not. '
        "Purchases, maybes and rejections only for price, size, owning something like it or quality are left out "
        "(they are not about the look). Pieces shown but not rated are "
        "left out, so these rates describe the pieces you chose to rate.",
    }


def status(db: Session) -> dict:
    """Everything the Settings and Insights pages show about the gate."""
    from ..models import Calibration

    cfg = get_setting(db, "selective")
    out = {
        "enabled": bool(cfg.get("enabled", True)),
        "mode": resolve_mode(cfg, None),
        "modes": [
            {"mode": m, "label": MODE_LABEL[m], "target": thresholds(cfg, m)[0], "confidence": thresholds(cfg, m)[1]}
            for m in MODES
        ],
        "targets_validated": False,
        "calibration": calibration.status(db),
        "performance": performance(db),
    }
    cur, ev = calibration.serving(db)
    if cur is not None:
        d = cur.diagnostics or {}
        bins = d.get("reliability_chosen") or d.get("reliability") or []
        out["reliability"] = []
        for b in bins:  # out-of-fold estimate vs observed approval per bin, with how little a small bin says
            k, n = round(b["observed"] * b["n"]), int(b["n"])
            lo, hi = clopper_pearson(k, n) if n else (0.0, 1.0)
            out["reliability"].append({**b, "interval": [round(lo, 3), round(hi, 3)]})
    out["history"] = []
    for c in db.query(Calibration).order_by(Calibration.id.desc()).limit(10):
        unusable = None if c.active else calibration.invalid_reason(db, c, ev)  # can it be rolled back to?
        withdrawn = (c.params or {}).get("withdrawn")
        out["history"].append({
            "id": c.id, "created_at": c.created_at.isoformat(), "labels": c.n_labels, "approvals": c.n_positive,
            "active": c.active, "map": (c.diagnostics or {}).get("map"),
            "brier": (c.diagnostics or {}).get("brier_chosen"), "ece": (c.diagnostics or {}).get("ece_chosen"),
            "current_model": c.engine_version == pm.ENGINE_VERSION, "usable": unusable is None,
            "unusable_reason": calibration.WITHDRAWN[unusable] if unusable else None,
            "withdrawn": {**withdrawn, "label": calibration.WITHDRAWN[withdrawn["reason"]]} if withdrawn else None,
        })  # fmt: skip
    return out
