"""Transparent hybrid recommender.

score = Σ_c  w_c · gate_c · (component_c − 0.5)

Components (each in [0, 1], 0.5 = neutral):

* rules              your written preferences (cold-start prior; fades as feedback grows)
* learned_attributes affinities for colours, categories, fibres, brands, sellers, fits and
                     logo levels learned from reason-aware feedback
* visual             similarity to images you approved / uploaded vs. rejected (CLIP or lite)
* semantic           similarity of descriptions + attributes to approved vs. rejected items
* quality            evidence-based quality (unverified ≈ neutral, never "excellent")
* value              price vs. category median, cheaper identical listings, learned price ceilings
* wardrobe           fills a gap (+) or adds redundancy (−)
* seller             seller reputation evidence and your seller priority

`gate` for learned components is the learning strength s = n / (n + k), where n
is the number of informative decisions; the rules gate is 1 − 0.6·s. Nothing is
claimed as "learned" until feedback exists.

Rejections are routed by reason: e.g. "too expensive for the quality" adjusts
a category price ceiling only - it never teaches a dislike of the colour or
garment type.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session, selectinload

from ..ingestion.vocab import CATEGORY_GROUP, NEUTRAL_COLOURS, PREMIUM_FIBRES, QUIET_LUXURY_BRANDS
from ..models import Feedback, InspirationImage, Product, Seller, WardrobeItem, utcnow
from . import embeddings as emb
from .settings_store import get_setting

ACTION_WEIGHT = {"love": 2.0, "like": 1.0, "purchased": 1.5, "maybe": 0.35, "dislike": -1.0}

REASONS = {
    "silhouette": "Dislike the silhouette",
    "colour": "Dislike the colour",
    "logo": "Dislike the logo",
    "fabric": "Fabric doesn't appeal to me",
    "construction": "Poor apparent quality",
    "too_expensive": "Too expensive for the quality",
    "already_own": "Already own something similar",
    "specific_item": "Don't like this specific item",
    "not_my_style": "Not my style",
    "wrong_category": "Wrong category (listing error)",
    "too_repetitive": "Too repetitive",
}

ATTR_TYPE_WEIGHT = {"colour": 1.0, "category": 1.0, "fibre": 1.0, "brand": 0.8, "seller": 0.5, "fit": 1.0, "logo": 1.0}


@dataclass
class Routing:
    attrs: dict[str, float] = field(default_factory=dict)  # attr type -> multiplier
    visual: float = 0.0
    text: float = 0.0
    suppress: bool = False
    price_ceiling: bool = False
    redundancy: bool = False
    construction_concern: bool = False


def route_feedback(action: str, reasons: list[str] | None) -> Routing:
    """Map a decision (+ optional reasons) to which preference signals it updates."""
    reasons = [r for r in (reasons or []) if r in REASONS and r != "not_my_style"]
    if action in ("love", "like", "purchased", "maybe"):
        return Routing(attrs={k: 1.0 for k in ATTR_TYPE_WEIGHT}, visual=1.0, text=1.0)
    if action != "dislike":
        return Routing()  # skip / unsave teach nothing
    if reasons and set(reasons) <= {"wrong_category", "too_repetitive"}:
        return Routing()  # a listing error or a repetitive feed says nothing about taste
    if not reasons:
        return Routing(attrs={k: 0.5 for k in ATTR_TYPE_WEIGHT}, visual=0.7, text=0.5, suppress=True)
    r = Routing()

    def bump(kind: str, v: float) -> None:
        r.attrs[kind] = max(r.attrs.get(kind, 0.0), v)

    for reason in reasons:
        if reason == "silhouette":
            bump("fit", 1.0)
            bump("category", 0.15)
            r.visual = max(r.visual, 0.6)
            r.text = max(r.text, 0.2)
        elif reason == "colour":
            bump("colour", 1.0)
        elif reason == "logo":
            bump("logo", 1.0)  # not the brand: a loud logo is not a verdict on the label
            r.visual = max(r.visual, 0.2)
        elif reason == "fabric":
            bump("fibre", 1.0)
            r.text = max(r.text, 0.2)
        elif reason == "construction":
            bump("seller", 0.6)
            r.construction_concern = True
        elif reason == "too_expensive":
            r.price_ceiling = True
        elif reason == "already_own":
            r.redundancy = True
        elif reason == "specific_item":
            r.suppress = True
            r.visual = max(r.visual, 0.25)
    return r


def product_attrs(p: Product | dict) -> list[str]:
    g = (lambda k: p.get(k)) if isinstance(p, dict) else (lambda k: getattr(p, k))
    keys = [f"colour:{c}" for c in (g("colours") or [])]
    if g("category"):
        keys.append(f"category:{g('category')}")
    keys += [f"fibre:{m['fibre']}" for m in (g("materials") or [])]
    if g("brand"):
        keys.append(f"brand:{g('brand')}")
    if g("seller_id"):
        keys.append(f"seller:{g('seller_id')}")
    keys += [f"fit:{f}" for f in (g("fit_tags") or [])]
    if g("logo_level") and g("logo_level") != "unknown":
        keys.append(f"logo:{g('logo_level')}")
    return keys


def snapshot(p: Product) -> dict:
    return {
        "title": p.title_en or p.title_original,
        "brand": p.brand,
        "category": p.category,
        "colours": p.colours,
        "materials": p.materials,
        "fit_tags": p.fit_tags,
        "logo_level": p.logo_level,
        "seller_id": p.seller_id,
        "seller": p.seller.name if p.seller else None,
        "price": p.price,
        "currency": p.currency,
        "source_url": p.source_url,
    }


@dataclass
class Proto:
    vec: np.ndarray
    weight: float
    label: str
    product_id: int | None
    kind: str  # love/like/purchased/inspiration/wardrobe/dislike


@dataclass
class Profile:
    attr_sum: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    attr_n: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    attr_examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    pos_visual: list[Proto] = field(default_factory=list)
    neg_visual: list[Proto] = field(default_factory=list)
    pos_text: list[Proto] = field(default_factory=list)
    neg_text: list[Proto] = field(default_factory=list)
    suppressed_ids: set[int] = field(default_factory=set)
    suppressed_vecs: list[np.ndarray] = field(default_factory=list)
    price_ceiling: dict[str, float] = field(default_factory=dict)
    price_tolerance: dict[str, float] = field(default_factory=dict)
    redundant: Counter = field(default_factory=Counter)
    construction_concerns: int = 0
    decided_ids: set[int] = field(default_factory=set)
    # Product families (Product.group_id) you have decided on: their other listings are the same garment.
    decided_groups: set[int] = field(default_factory=set)
    skipped_at: dict[int, datetime] = field(default_factory=dict)
    exposure: Counter = field(default_factory=Counter)
    n_informative: int = 0
    n_positive: int = 0
    n_negative: int = 0
    n_seed: int = 0
    reason_counts: Counter = field(default_factory=Counter)
    strength: float = 0.0

    def affinity(self, key: str) -> float:
        n = self.attr_n.get(key, 0.0)
        if n <= 0:
            return 0.0
        return self.attr_sum[key] / (n + 2.0)  # Bayesian shrinkage towards 0


def build_profile(db: Session, backend_name: str | None = None) -> Profile:
    prof = Profile()
    cfg = get_setting(db, "recommender")
    backend_name = backend_name or emb.get_image_backend().name

    events = db.query(Feedback).order_by(Feedback.created_at).all()
    latest: dict[int, Feedback] = {}
    for fb in events:
        if fb.product_id is None:
            continue
        snap = fb.snapshot or {}
        for k in ("category", "brand"):
            if snap.get(k):
                prof.exposure[f"{k}:{snap[k]}"] += 1
        if snap.get("seller_id"):
            prof.exposure[f"seller:{snap['seller_id']}"] += 1
        if fb.action == "skip":
            prof.skipped_at[fb.product_id] = fb.created_at
            continue
        if fb.action == "unsave":
            latest.pop(fb.product_id, None)
            prof.decided_ids.add(fb.product_id)
            continue
        latest[fb.product_id] = fb

    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(list(latest))).all()} if latest else {}
    for pid, fb in latest.items():
        prof.decided_ids.add(pid)
        p = products.get(pid)
        source = p if p is not None else (fb.snapshot or {})
        title = (fb.snapshot or {}).get("title") or (p.title_en if p else "item")
        w = ACTION_WEIGHT.get(fb.action, 0.0)
        if (fb.context or {}).get("seed"):
            prof.n_seed += 1
        routing = route_feedback(fb.action, fb.reasons)
        for r in fb.reasons or []:
            prof.reason_counts[r] += 1
        informative = bool(routing.attrs or routing.visual or routing.text)
        if informative:
            prof.n_informative += 1
            if w > 0:
                prof.n_positive += 1
            elif w < 0:
                prof.n_negative += 1
        is_seed = bool((fb.context or {}).get("seed"))
        for key in product_attrs(source):
            if is_seed and key.startswith("seller:"):
                continue  # seed examples describe style, not endorsement of the seller
            mult = routing.attrs.get(key.split(":", 1)[0], 0.0)
            if mult:
                prof.attr_sum[key] += w * mult
                prof.attr_n[key] += abs(mult)
                if len(prof.attr_examples[key]) < 5:
                    prof.attr_examples[key].append(title)
        if p is not None:
            if p.image_embedding is not None and p.embedding_backend == backend_name and routing.visual:
                proto = Proto(emb.from_bytes(p.image_embedding), abs(w) * routing.visual, title, p.id, fb.action)
                (prof.pos_visual if w > 0 else prof.neg_visual).append(proto)
            if p.text_embedding is not None and routing.text:
                proto = Proto(emb.from_bytes(p.text_embedding), abs(w) * routing.text, title, p.id, fb.action)
                (prof.pos_text if w > 0 else prof.neg_text).append(proto)
            if routing.suppress:
                prof.suppressed_ids.add(p.id)
                if p.image_embedding is not None and p.embedding_backend == backend_name:
                    prof.suppressed_vecs.append(emb.from_bytes(p.image_embedding))
        cat = (source.category if p is not None else source.get("category")) or "other"
        price = p.price if p is not None else source.get("price")
        if routing.price_ceiling and price:
            prof.price_ceiling[cat] = min(prof.price_ceiling.get(cat, float("inf")), price)
        if w > 0 and price:
            prof.price_tolerance[cat] = max(prof.price_tolerance.get(cat, 0.0), price)
        if routing.redundancy:
            prof.redundant[cat] += 1
        if routing.construction_concern:
            prof.construction_concerns += 1

    for img in db.query(InspirationImage).filter(InspirationImage.embedding.isnot(None)):
        if img.embedding_backend == backend_name:
            proto = Proto(emb.from_bytes(img.embedding), 1.5, img.note or "inspiration photo", None, "inspiration")
            (prof.pos_visual if img.polarity > 0 else prof.neg_visual).append(proto)
            prof.n_informative += 1 if img.polarity else 0
    for item in db.query(WardrobeItem).filter(WardrobeItem.embedding.isnot(None)):
        if item.embedding_backend == backend_name:
            wgt = 0.4 + 0.2 * ((item.rating or 3) - 3)
            if wgt > 0:
                prof.pos_visual.append(Proto(emb.from_bytes(item.embedding), wgt, item.name, None, "wardrobe"))

    # A decision about a garment covers its other listings, except a dislike only for the price: a cheaper
    # offer of the same piece is exactly what you would want to see.
    covering = [
        pid
        for pid, fb in latest.items()
        if not (fb.action == "dislike" and fb.reasons and set(fb.reasons) <= {"too_expensive"})
    ]
    if covering:
        prof.decided_groups = {
            g for (g,) in db.query(Product.group_id).filter(Product.id.in_(covering), Product.group_id.isnot(None))
        }
    k = cfg.get("cold_start_k", 15)
    prof.strength = prof.n_informative / (prof.n_informative + k) if prof.n_informative else 0.0
    return prof


# ------------------------------------------------------------------ components
def rules_component(p: Product, taste: dict) -> tuple[float, list[str]]:
    pts = 0.0
    why: list[str] = []
    colours = set(p.colours or [])
    pref_c = set(taste.get("preferred_colours", []))
    if colours & pref_c:
        pts += 1.0
        why.append(f"{', '.join(sorted(colours & pref_c))} is in your core palette")
    elif colours:
        pts -= 0.4
    cats = set(taste.get("preferred_categories", []))
    if p.category in cats:
        pts += 0.8
    elif p.category in ("footwear", "accessories", "bag"):
        pts -= 0.3
    fibres = {m["fibre"]: m.get("pct") for m in (p.materials or [])}
    pref_f = set(taste.get("preferred_fibres", []))
    good = [f for f in fibres if f in pref_f]
    if good:
        pts += 1.2 if "cashmere" in good else 0.9
        why.append(f"claimed {', '.join(good)} content")
    avoid = [f for f in fibres if f in set(taste.get("avoid_fibres", [])) and (fibres[f] is None or fibres[f] >= 40)]
    if avoid:
        pts -= 0.8
    logo = p.logo_level
    if logo == "none":
        pts += 0.8
        why.append("no visible branding")
    elif logo == "subtle":
        pts += 0.4
    elif logo == "visible":
        pts -= 1.0
    fits = set(p.fit_tags or [])
    if fits & set(taste.get("preferred_fits", [])):
        pts += 0.6
        why.append(f"{'/'.join(sorted(fits & set(taste.get('preferred_fits', []))))} cut")
    if fits & set(taste.get("avoid_fits", [])):
        pts -= 0.6
    if p.brand in set(taste.get("favourite_brands", [])):
        pts += 0.6
        why.append(f"{p.brand} design language")
    elif p.brand in QUIET_LUXURY_BRANDS:
        pts += 0.4
    focus = set(taste.get("season_focus", []))
    if focus and set(p.seasons or []) & focus:
        pts += 0.4
    elif focus and p.seasons and not set(p.seasons) & focus:
        pts -= 0.3
    if p.gsm and p.category == "t-shirt" and p.gsm >= 220:
        pts += 0.5
        why.append(f"heavyweight {p.gsm} g/m² claimed")
    return 0.5 + 0.5 * math.tanh(pts / 2.5), why


def learned_attr_component(p: Product, prof: Profile) -> tuple[float, list[tuple[str, float]]]:
    contribs = []
    total = 0.0
    weight = 0.0
    for key in product_attrs(p):
        aff = prof.affinity(key)
        if prof.attr_n.get(key):
            w = ATTR_TYPE_WEIGHT[key.split(":", 1)[0]]
            total += w * aff
            weight += w
            contribs.append((key, aff))
    if not weight:
        return 0.5, []
    return 0.5 + 0.5 * math.tanh(1.6 * total / weight), sorted(contribs, key=lambda x: -abs(x[1]))


def _proto_sims(mat: np.ndarray, protos: list[Proto]) -> tuple[np.ndarray, np.ndarray]:
    """Weighted top-3 similarity of each row of `mat` to the prototype set; also argmax index."""
    if not protos or mat.shape[0] == 0:
        return np.zeros(mat.shape[0]), np.full(mat.shape[0], -1)
    P = np.vstack([pr.vec for pr in protos])
    W = np.array([pr.weight for pr in protos])
    S = mat @ P.T  # (n, m)
    best = S.argmax(1)
    k = min(3, len(protos))
    idx = np.argsort(-S, axis=1)[:, :k]
    top = np.take_along_axis(S, idx, 1)
    tw = W[idx]
    return (top * tw).sum(1) / tw.sum(1), best


def _zscore_component(pos: np.ndarray, neg: np.ndarray, has_pos: bool, has_neg: bool) -> np.ndarray:
    raw = (pos if has_pos else 0) - (0.8 * neg if has_neg else 0)
    raw = np.asarray(raw, dtype=np.float64)
    if raw.size == 0 or not (has_pos or has_neg):
        return np.full(raw.shape if raw.ndim else 0, 0.5)
    sd = raw.std() or 1.0
    z = (raw - raw.mean()) / sd
    return 0.5 + 0.5 * np.tanh(z / 1.5)


# ------------------------------------------------------------------- ranking
@dataclass
class Ranked:
    product: Product
    score: float
    components: dict[str, float]
    contributions: dict[str, float]
    design_score: float
    _reasons: list[str]
    _cautions: list[str]
    nearest_positive: dict | None
    explore: bool = False
    suppressed: bool = False
    strength: float = 0.0
    sample_score: float | None = None  # score under a posterior sample (Thompson exploration)
    taste_sd: float | None = None  # uncertainty of the learned taste estimate (logit s.d.)
    # Builds (reasons, cautions) on first use: a feed ranks thousands of pieces but shows a few dozen.
    explainer: Callable[[], tuple[list[str], list[str]]] | None = field(default=None, repr=False)

    def _explain(self) -> None:
        if self.explainer is not None:
            self._reasons, self._cautions = self.explainer()
            self.explainer = None

    @property
    def reasons(self) -> list[str]:
        self._explain()
        return self._reasons

    @property
    def cautions(self) -> list[str]:
        self._explain()
        return self._cautions


UNCERTAINTY_TOP = 1000


def rank_products(
    db: Session,
    products: list[Product],
    prof: Profile | None = None,
    quality_lookup: dict[int, dict] | None = None,
    wardrobe_lookup: dict[int, dict] | None = None,
) -> list[Ranked]:
    from .quality import assess, category_price_stats

    if not products:
        return []
    backend = emb.get_image_backend().name
    prof = prof or build_profile(db, backend)
    taste = get_setting(db, "taste_profile")
    weights = get_setting(db, "ranking_weights")
    rcfg = get_setting(db, "recommender")
    s = prof.strength
    price_stats = category_price_stats(db)
    sellers = {sl.id: sl for sl in db.query(Seller).all()}

    # Vectorised visual / semantic components
    vis_idx = [i for i, p in enumerate(products) if p.image_embedding is not None and p.embedding_backend == backend]
    visual = np.full(len(products), 0.5)
    vis_best = np.full(len(products), -1)
    near_dup = np.zeros(len(products), dtype=bool)
    if vis_idx:
        M = np.vstack([emb.from_bytes(products[i].image_embedding) for i in vis_idx])
        ps, best = _proto_sims(M, prof.pos_visual)
        ns, _ = _proto_sims(M, prof.neg_visual)
        comp = _zscore_component(ps, ns, bool(prof.pos_visual), bool(prof.neg_visual))
        for j, i in enumerate(vis_idx):
            visual[i] = comp[j] if (prof.pos_visual or prof.neg_visual) else 0.5
            vis_best[i] = best[j]
        if prof.suppressed_vecs:
            sup = M @ np.vstack(prof.suppressed_vecs).T
            thr = rcfg.get("near_duplicate_threshold", 0.96)
            for j, i in enumerate(vis_idx):
                near_dup[i] = bool((sup[j] >= thr).any())
    txt_idx = [i for i, p in enumerate(products) if p.text_embedding is not None]
    semantic = np.full(len(products), 0.5)
    if txt_idx and (prof.pos_text or prof.neg_text):
        T = np.vstack([emb.from_bytes(products[i].text_embedding) for i in txt_idx])
        ps, _ = _proto_sims(T, prof.pos_text)
        ns, _ = _proto_sims(T, prof.neg_text)
        comp = _zscore_component(ps, ns, bool(prof.pos_text), bool(prof.neg_text))
        for j, i in enumerate(txt_idx):
            semantic[i] = comp[j]

    bayes = rcfg.get("engine", "bayes") == "bayes"
    if bayes:
        from . import preference_model as pm

        space = pm.build_feature_space(db, taste, backend)
        post = pm.fit(db, space)
        known = [p.id for p in products if p.id in space.row]
        # Mean logits for all; per-item uncertainty is computed by the selective gate for its shortlist only.
        zmean = post.mean_logit(known)
        rng = np.random.default_rng(int(utcnow().strftime("%Y%m%d")) * 1000 + post.n_obs)
        zsamp = post.sample_z(known, rng) if known else np.zeros(0)
        model_by_id = {pid: (float(zmean[j]), None, float(zsamp[j])) for j, pid in enumerate(known)}
        s = post.n_obs / (post.n_obs + rcfg.get("cold_start_k", 15)) if post.n_obs else 0.0

    gates = {
        "rules": 1 - 0.6 * s,
        "learned_attributes": s,
        "visual": s if (prof.pos_visual or prof.neg_visual) else 0.0,
        "semantic": s if (prof.pos_text or prof.neg_text) else 0.0,
        "quality": 1.0 + 0.1 * min(prof.construction_concerns, 5),
        "value": 1.0,
        "wardrobe": 1.0,
        "seller": 1.0,
    }
    if bayes:
        gates = {"taste": 1.0, "quality": gates["quality"], "value": 1.0, "wardrobe": 1.0, "seller": 1.0}
        weights = {**weights, "taste": weights.get("taste", 3.0)}
    denom = sum(abs(weights.get(c, 0)) * g for c, g in gates.items()) or 1.0

    out: list[Ranked] = []
    for i, p in enumerate(products):
        reasons: list[str] = []
        cautions: list[str] = []
        rules, rules_why = rules_component(p, taste)
        learned, contribs = learned_attr_component(p, prof)
        q = quality_lookup.get(p.id) if quality_lookup else None
        if q is None:
            q = assess(db, p, price_stats=price_stats)
        qs = q["quality_score"]
        quality_c = 0.45 if qs is None else 0.5 + (qs / 100 - 0.5) * (0.4 + 0.6 * q["overall_confidence"])
        vs = q["dimensions"]["value"]["score"]
        value_c = 0.5 if vs is None else 0.3 + 0.4 * vs / 100
        cat = p.category or "other"
        ceiling = prof.price_ceiling.get(cat)
        if ceiling and p.price and p.price >= 0.9 * ceiling and qs is None:
            value_c = min(value_c, 0.2)
            cautions.append(
                f"You previously judged ¥{ceiling:g} too expensive for an unproven {cat}; this is ¥{p.price:g}"
            )
        tol = prof.price_tolerance.get(cat)
        if tol and p.price and p.price > 1.6 * tol:
            value_c -= 0.1
        wl = (wardrobe_lookup or {}).get(p.id)
        wardrobe_c = wl["score"] if wl else 0.5
        if wl and wl.get("note"):
            (reasons if wardrobe_c > 0.5 else cautions).append(wl["note"])
        if prof.redundant.get(cat):
            wardrobe_c = min(wardrobe_c, 0.35)
        sl = sellers.get(p.seller_id) if p.seller_id else None
        rep = {"trusted": 0.8, "mixed": 0.5, "concerns": 0.15}.get(sl.reputation_status if sl else "", 0.5)
        prio = 0.5 + (3 - (sl.priority if sl else 3)) * 0.06
        seller_c = 0.5 * rep + 0.5 * prio

        if bayes:
            out.append(
                _bayes_ranked(
                    p,
                    post,
                    model_by_id.get(p.id),
                    rules_why,
                    quality_c,
                    value_c,
                    wardrobe_c,
                    seller_c,
                    weights,
                    gates,
                    denom,
                    reasons,
                    cautions,
                    prof,
                    vis_best[i],
                    p.id in prof.suppressed_ids or bool(near_dup[i]),
                    s,
                )
            )
            continue
        comps = {
            "rules": rules,
            "learned_attributes": learned,
            "visual": float(visual[i]),
            "semantic": float(semantic[i]),
            "quality": quality_c,
            "value": value_c,
            "wardrobe": wardrobe_c,
            "seller": seller_c,
        }
        contrib = {c: weights.get(c, 0) * gates[c] * (v - 0.5) for c, v in comps.items()}
        score = 0.5 + sum(contrib.values()) / (2 * denom) * 2
        suppressed = p.id in prof.suppressed_ids or bool(near_dup[i])
        if suppressed:
            score -= 0.5

        # Design compatibility: taste-only components
        tw = {
            "rules": weights["rules"] * gates["rules"],
            "learned_attributes": weights["learned_attributes"] * s,
            "visual": weights["visual"] * gates["visual"],
            "semantic": weights["semantic"] * gates["semantic"],
        }
        tws = sum(tw.values()) or 1.0
        design = sum(comps[c] * w for c, w in tw.items()) / tws

        if s < 0.05:
            reasons.append("Cold start: ranked mainly on your written preferences")
        reasons += [r[0].upper() + r[1:] for r in rules_why[:3]]
        for key, aff in contribs[:3]:
            label = key.split(":", 1)[1]
            if key.startswith("seller:"):
                label = sellers[int(label)].name if int(label) in sellers else label
            ex = prof.attr_examples.get(key, [])
            if aff > 0.15:
                reasons.append(
                    f"You've responded well to {key.split(':')[0]} “{label}”" + (f" (e.g. {ex[0]})" if ex else "")
                )
            elif aff < -0.15:
                cautions.append(f"You've rejected items with {key.split(':')[0]} “{label}” for that reason before")
        nearest = None
        if vis_best[i] >= 0 and prof.pos_visual and comps["visual"] > 0.6:
            pr = prof.pos_visual[int(vis_best[i])]
            nearest = {"label": pr.label, "product_id": pr.product_id, "kind": pr.kind}
            verb = {
                "love": "loved",
                "like": "liked",
                "purchased": "bought",
                "maybe": "marked maybe",
                "inspiration": "uploaded as inspiration",
                "wardrobe": "own",
            }.get(pr.kind, "liked")
            reasons.insert(0, f"Visually close to “{pr.label}”, which you {verb}")
        if comps["semantic"] > 0.7 and s > 0:
            reasons.append("Description and attributes resemble items you approved")
        if p.logo_level == "visible":
            cautions.append("Listing mentions visible logo/print")
        if suppressed:
            cautions.append("Near-identical to something you rejected")
        out.append(
            Ranked(
                p,
                float(score),
                comps,
                contrib,
                float(design),
                reasons[:5],
                cautions[:4],
                nearest,
                suppressed=suppressed,
                strength=s,
            )
        )
    out.sort(key=lambda r: -r.score)
    if bayes:  # uncertainty for the part of the ranking anyone will look at (cheap; the gate computes its own)
        top = [r for r in out[:UNCERTAINTY_TOP] if r.product.id in space.row]
        if top:
            for r, sd in zip(top, post.logit_sd(post.rows([r.product.id for r in top])), strict=True):
                r.taste_sd = float(sd)
    return out


GROUP_WORD = {"colour": "colour", "category": "", "fibre": "", "fit": "fit", "logo": "branding", "brand": ""}


def _feature_phrase(group: str, label: str) -> str:
    if group == "combo":  # "black|hoodie"
        return label.replace("|", " ")
    if group == "logo":
        return {"none": "no visible branding", "subtle": "subtle branding", "visible": "visible logos"}.get(
            label, label
        )
    word = GROUP_WORD.get(group, "")
    return f"{label} {word}".strip()


def _certainty(effect: float, sd: float) -> str:
    ratio = abs(effect) / (sd + 1e-9)
    return "confident" if ratio > 3 else "fairly sure" if ratio > 1.5 else "tentative"


def _bayes_ranked(
    p,
    post,
    model,
    rules_why,
    quality_c,
    value_c,
    wardrobe_c,
    seller_c,
    weights,
    gates,
    denom,
    reasons,
    cautions,
    prof,
    vis_best_i,
    suppressed,
    strength,
) -> Ranked:
    """Score one product with the Bayesian taste model (see preference_model). Its explanation is built only if
    it is read (the feed shows a few dozen of the pieces it ranks)."""
    zm, zsd, zs = model if model else (0.0, None, 0.0)
    taste_c = float(1 / (1 + math.exp(-zm)))
    comps = {"taste": taste_c, "quality": quality_c, "value": value_c, "wardrobe": wardrobe_c, "seller": seller_c}
    contrib = {c: weights.get(c, 0) * gates[c] * (v - 0.5) for c, v in comps.items()}
    score = 0.5 + sum(contrib.values()) / denom
    sampled_taste = float(1 / (1 + math.exp(-zs)))
    sample_score = score + weights.get("taste", 0) * (sampled_taste - taste_c) / denom
    if suppressed:
        score -= 0.5
        sample_score -= 0.5
    nearest = None
    if vis_best_i >= 0 and prof.pos_visual:
        pr = prof.pos_visual[int(vis_best_i)]
        nearest = {"label": pr.label, "product_id": pr.product_id, "kind": pr.kind}

    def explain() -> tuple[list[str], list[str]]:
        model_reasons: list[str] = []
        warn = list(cautions)
        if post.n_obs == 0:
            model_reasons.append("Cold start: ranked on your written preferences until you make some decisions")
        if model:
            pos, neg = post.explain(p.id)
            for f in pos:
                if f["group"] == "prior":
                    model_reasons.extend(r[0].upper() + r[1:] for r in rules_why[:2])
                elif f["group"] == "img":
                    continue  # covered by the nearest-example sentence below
                else:
                    model_reasons.append(
                        f"You tend to like {_feature_phrase(f['group'], f['label'])} ({_certainty(f['effect'], f['sd'])})"
                    )
            for f in neg:
                if f["group"] == "prior":
                    warn.append("Doesn't match your written preferences")
                elif f["group"] != "img":
                    warn.append(
                        f"You tend to avoid {_feature_phrase(f['group'], f['label'])} ({_certainty(f['effect'], f['sd'])})"
                    )
            if any(f["group"] == "img" for f in pos) and vis_best_i >= 0 and prof.pos_visual:
                pr = prof.pos_visual[int(vis_best_i)]
                model_reasons.insert(0, f"Looks like pieces you liked, e.g. “{pr.label}”")
        if not model_reasons and rules_why:
            model_reasons.extend(r[0].upper() + r[1:] for r in rules_why[:2])
        if p.logo_level == "visible":
            warn.append("Listing mentions visible logo/print")
        if suppressed:
            warn.append("Near-identical to something you rejected")
        return list(dict.fromkeys(model_reasons + reasons))[:5], list(dict.fromkeys(warn))[:4]

    return Ranked(
        p,
        float(score),
        comps,
        contrib,
        taste_c,
        [],
        [],
        nearest,
        suppressed=suppressed,
        strength=strength,
        sample_score=float(sample_score),
        taste_sd=zsd,
        explainer=explain,
    )


def explore_novelty(p: Product, prof: Profile) -> float:
    keys = []
    if p.category:
        keys.append(f"category:{p.category}")
    if p.brand:
        keys.append(f"brand:{p.brand}")
    if p.seller_id:
        keys.append(f"seller:{p.seller_id}")
    if not keys:
        return 0.5
    return sum(1.0 / (1 + prof.exposure.get(k, 0)) for k in keys) / len(keys)


def build_feed(
    ranked: list[Ranked], prof: Profile, limit: int, exploration_rate: float, seed: int | None = None
) -> list[Ranked]:
    """Exploit the top of the ranking, reserving ~exploration_rate of slots for novel items."""
    rng = random.Random(seed)
    pool = [r for r in ranked if not r.suppressed]
    n_explore = round(limit * exploration_rate) if len(pool) > limit else 0
    exploit = pool[: limit - n_explore]
    chosen = {r.product.id for r in exploit}
    rest = [r for r in pool if r.product.id not in chosen and r.components["quality"] >= 0.3]
    explore: list[Ranked] = []
    if n_explore and rest and all(r.sample_score is not None for r in rest):
        # Bayesian engine: explore where the model is uncertain but plausibly right (Thompson sampling)
        for r in sorted(rest, key=lambda r: -r.sample_score)[:n_explore]:
            r.explore = True
            r.reasons.insert(
                0, "Exploration pick: the model is still unsure about pieces like this, so your reaction helps it learn"
            )
            explore.append(r)
    elif n_explore and rest:
        weights = [explore_novelty(r.product, prof) ** 2 + 0.05 for r in rest]
        while rest and len(explore) < n_explore:
            j = rng.choices(range(len(rest)), weights=weights)[0]
            r = rest.pop(j)
            weights.pop(j)
            r.explore = True
            novel = [x for x in (r.product.category, r.product.brand) if x]
            r.reasons.insert(
                0,
                "Exploration pick: testing "
                + (" / ".join(novel) or "something different")
                + " to broaden what the system knows about you",
            )
            explore.append(r)
    feed: list[Ranked] = []
    gap = max(3, round(1 / exploration_rate)) if exploration_rate > 0 else 10**9
    ei = 0
    for r in exploit:
        feed.append(r)
        if ei < len(explore) and len(feed) % gap == gap - 1:
            feed.append(explore[ei])
            ei += 1
    feed.extend(explore[ei:])
    return feed[:limit]


def feed_candidates(db: Session, prof: Profile, *, include_skipped_after_days: int = 10) -> list[Product]:
    """Undecided active listings. Listings of a garment you decided on, or recently skipped, are left out:
    they are the same piece from another seller (the product and compare pages show those offers)."""
    cutoff = utcnow() - timedelta(days=include_skipped_after_days)
    skipped = {pid for pid, at in prof.skipped_at.items() if at > cutoff}
    blocked = set(prof.decided_groups)
    if skipped:
        blocked |= {
            g for (g,) in db.query(Product.group_id).filter(Product.id.in_(skipped), Product.group_id.isnot(None))
        }
    # Evidence is read for every candidate's quality; photos are loaded only for the cards actually shown.
    q = db.query(Product).options(selectinload(Product.evidence)).filter(Product.status == "active")
    out = []
    for p in q:
        if p.id in prof.decided_ids or p.id in skipped:
            continue
        if p.group_id is not None and p.group_id in blocked:
            continue
        out.append(p)
    return out


def collapse_families(ranked: list[Ranked]) -> tuple[list[Ranked], dict[int, list[Product]]]:
    """One entry per garment: the best-ranked listing of each product family stands for it, and the family's
    other listings are returned as its offers (so the same piece never fills several feed slots)."""
    kept: list[Ranked] = []
    lead: dict[int, int] = {}
    offers: dict[int, list[Product]] = {}
    for r in ranked:
        g = r.product.group_id
        if g is not None and g in lead:
            offers[lead[g]].append(r.product)
            continue
        if g is not None:
            lead[g] = r.product.id
        offers[r.product.id] = []
        kept.append(r)
    return kept, offers


def describe_profile(db: Session, prof: Profile | None = None) -> dict:
    prof = prof or build_profile(db)
    groups: dict[str, list[dict]] = defaultdict(list)
    sellers = {s.id: s.name for s in db.query(Seller).all()}
    for key, n in prof.attr_n.items():
        kind, label = key.split(":", 1)
        if kind == "seller":
            label = sellers.get(int(label), label)
        groups[kind].append(
            {
                "key": key,
                "label": label,
                "affinity": round(prof.affinity(key), 3),
                "evidence": round(n, 2),
                "examples": prof.attr_examples.get(key, [])[:3],
            }
        )
    for g in groups.values():
        g.sort(key=lambda x: -x["affinity"])
    s = prof.strength
    if prof.n_informative == 0:
        stage = "Cold start — no decisions yet. Rankings use your written preferences only."
    elif s < 0.3:
        stage = (
            f"Early learning — {prof.n_informative} informative decisions. Learned signals are weak "
            "and only nudge the ranking; written preferences still dominate."
        )
    elif s < 0.6:
        stage = f"Learning — {prof.n_informative} decisions; learned preferences now carry substantial weight."
    else:
        stage = f"Established — {prof.n_informative} decisions; rankings are driven mainly by your behaviour."
    return {
        "learning_strength": round(s, 3),
        "stage": stage,
        "decisions": {
            "informative": prof.n_informative,
            "positive": prof.n_positive,
            "negative": prof.n_negative,
            "seed_examples": prof.n_seed,
        },
        "visual_examples": {"positive": len(prof.pos_visual), "negative": len(prof.neg_visual)},
        "attributes": dict(groups),
        "price_ceilings": prof.price_ceiling,
        "price_tolerance": prof.price_tolerance,
        "redundant_categories": dict(prof.redundant),
        "rejection_reasons": {REASONS.get(k, k): v for k, v in prof.reason_counts.items()},
        "construction_concerns": prof.construction_concerns,
        "palette_neutral_share": _neutral_share(prof),
        "category_groups": CATEGORY_GROUP,
    }


def _neutral_share(prof: Profile) -> float | None:
    pos = [(k, v) for k, v in prof.attr_sum.items() if k.startswith("colour:") and v > 0]
    if not pos:
        return None
    tot = sum(v for _, v in pos)
    return round(sum(v for k, v in pos if k.split(":")[1] in NEUTRAL_COLOURS) / tot, 2)


__all__ = [
    "REASONS",
    "build_profile",
    "rank_products",
    "build_feed",
    "feed_candidates",
    "describe_profile",
    "route_feedback",
    "snapshot",
    "PREMIUM_FIBRES",
]
