"""Evidence-based quality assessment.

Principles
----------
* Price never raises a quality score.
* Seller claims and AI visual assessments are shown but never move a score;
  AI is never used for fibre content or durability.
* Each source counts once: a review reposted, imported twice or linked in another form (tracking parameters, a
  short link, a mobile page; see sources.py) is one source.
* A source cannot outvote itself: copies of one excerpt count once, and a source whose excerpts point both ways
  is mixed (it adds confidence, not direction).
* Garment quality (construction, material) and seller reliability (service, sizing, track record) are
  separate: good service never makes a garment look verified.
* If the weighted evidence for a dimension is below a threshold the dimension
  is reported as **Unverified** (score ``None``), not as good or bad.
"""

from __future__ import annotations

import math
import threading
from statistics import median

from sqlalchemy.orm import Session

from ..models import Evidence, Product, Seller
from . import revisions
from .money import Rates, rates
from .sources import canonical_source

KIND_WEIGHT = {
    "verified": 1.0,
    "user_note": 0.8,
    "buyer_report": 0.6,
    "seller_claim": 0.12,
    "ai_visual": 0.08,
}
AFFILIATE_FACTOR = 0.35
UNVERIFIED_BELOW = 0.3  # confidence below this => "Unverified"

DIMENSIONS = {
    "construction": {"construction", "stitching", "hardware", "fit", "accuracy", "defect"},
    "material": {"material", "gsm"},
    "seller": {"reputation", "defect", "sizing", "service", "consistency", "batch"},
}

KIND_LABEL = {
    "verified": "Verified evidence",
    "seller_claim": "Seller claim",
    "buyer_report": "Independent buyer report",
    "ai_visual": "AI visual assessment",
    "user_note": "Your own note",
}


def _weight(e: Evidence) -> float:
    w = KIND_WEIGHT.get(e.kind, 0.1) * max(0.0, min(1.0, e.strength or 0.0))
    if e.is_affiliate:
        w *= AFFILIATE_FACTOR
    return w


INDEPENDENT = ("verified", "buyer_report", "user_note")
MIN_INDEPENDENT_WEIGHT = 0.3  # e.g. one moderately specific buyer report; below this a dimension stays unverified


def _source_key(e: Evidence) -> tuple:
    """Which source an item came from: the same review or post, however its link was copied, counts once."""
    if key := canonical_source(e.source_url):
        return ("url", key)
    return ("text", e.kind, e.aspect, " ".join((e.claim or "").lower().split()), e.created_by)


def _sources(items: list[Evidence]) -> list[tuple[str, float, int, bool]]:
    """(kind, weight, direction, mixed) per distinct source: its strongest item's weight, and the direction its
    excerpts agree on. Copies never add up, and a source whose excerpts point both ways is mixed (direction 0):
    four copies of a favourable excerpt cannot outvote a contrary excerpt from the same post."""
    groups: dict[tuple, list[Evidence]] = {}
    for e in items:
        groups.setdefault(_source_key(e), []).append(e)
    out = []
    for group in groups.values():
        strongest = max(group, key=_weight)
        directions = {(e.polarity > 0) - (e.polarity < 0) for e in group if _weight(e) > 0} - {0}
        out.append((strongest.kind, _weight(strongest), directions.pop() if len(directions) == 1 else 0,
                    len(directions) > 1))  # fmt: skip
    return out


def score_dimension(items: list[Evidence]) -> dict:
    """Score one quality dimension from distinct independent sources. Seller claims and AI impressions are
    listed (``evidence_counts``) but never move the score, so marketing alone cannot make a garment look good."""
    by_kind: dict[str, int] = {}
    for e in items:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    sources = [s for s in _sources(items) if s[0] in INDEPENDENT]
    counts = {"sources": len(sources), "mixed_sources": sum(1 for s in sources if s[3])}
    total = sum(w for _, w, _, _ in sources)
    signed = sum(w * pol for _, w, pol, _ in sources)
    directional = sum(w for _, w, pol, _ in sources if pol != 0)
    confidence = 1 - math.exp(-total / 1.2)
    if confidence < UNVERIFIED_BELOW or total < MIN_INDEPENDENT_WEIGHT:
        label = "Seller claim only" if by_kind and set(by_kind) <= {"seller_claim", "ai_visual"} else "Unverified"
        return {"score": None, "label": label, "confidence": round(confidence, 2), "evidence_counts": by_kind,
                **counts}  # fmt: skip
    score = 50 + 50 * signed / (directional + 0.8) if directional else 55.0
    score = max(0.0, min(100.0, score))
    label = "Strong" if score >= 75 else "Good" if score >= 60 else "Mixed" if score >= 40 else "Poor"
    return {"score": round(score), "label": label, "confidence": round(confidence, 2), "evidence_counts": by_kind,
            **counts}  # fmt: skip


# Category medians shared across requests until the listings or the conversion rates change (revisions.py).
_price_stats_lock = threading.Lock()
_price_stats_cache: dict = {"key": None, "value": None}


def category_price_stats(db: Session, fx: Rates | None = None) -> dict[str, float]:
    """Median price in pounds per category, over listings for sale whose currency converts (others are left out:
    an unstated currency is not assumed to be yuan). Seed examples from your notes are not listings."""
    fx = fx or rates(db)
    key = (revisions.current(db, "products"), tuple(sorted(fx.to_gbp.items())))
    with _price_stats_lock:
        if _price_stats_cache["key"] == key:
            return dict(_price_stats_cache["value"])
    stats = _category_price_stats(db, fx)
    with _price_stats_lock:
        _price_stats_cache.update(key=key, value=stats)
    return dict(stats)


def _category_price_stats(db: Session, fx: Rates) -> dict[str, float]:
    by_cat: dict[str, list[float]] = {}
    for cat, price, currency in db.query(Product.category, Product.price, Product.currency).filter(
        Product.price.isnot(None), Product.status == "active", Product.data_origin != "seed"
    ):
        if (gbp := fx.gbp(price, currency)) is not None:
            by_cat.setdefault(cat or "other", []).append(gbp)
    return {k: median(v) for k, v in by_cat.items() if len(v) >= 3}


def assess(
    db: Session,
    p: Product,
    *,
    seller_evidence: list[Evidence] | None = None,
    price_stats: dict[str, float] | None = None,
    cheaper_equivalent: dict | None = None,
    design_score: float | None = None,
    design_confidence: float | None = None,
    fx: Rates | None = None,
) -> dict:
    ev = list(p.evidence)
    if seller_evidence is None and p.seller_id:
        seller_evidence = (
            db.query(Evidence).filter(Evidence.seller_id == p.seller_id, Evidence.product_id.is_(None)).all()
        )
    seller_evidence = seller_evidence or []

    construction = score_dimension([e for e in ev if e.aspect in DIMENSIONS["construction"]])
    material = score_dimension([e for e in ev if e.aspect in DIMENSIONS["material"]])
    seller_dim = score_dimension(seller_evidence + [e for e in ev if e.aspect in {"sizing", "service"}])

    # Value = what you get for the price. Price alone says nothing about value: a cheap unverified item is
    # not "good value", so without quality evidence the score stays unknown. The one exception is a
    # like-for-like comparison: the very same item listed cheaper elsewhere makes this listing worse value.
    value = {
        "score": None,
        "label": "Unknown — quality not yet demonstrated",
        "confidence": 0.0,
        "notes": [],
        "price_position": None,
    }
    if p.price:
        fx = fx or rates(db)
        stats = price_stats if price_stats is not None else category_price_stats(db, fx)
        med = stats.get(p.category or "other")
        gbp = fx.gbp(p.price, p.currency)
        q_scores = [d["score"] for d in (construction, material) if d["score"] is not None]
        q = sum(q_scores) / len(q_scores) if q_scores else None
        notes = []
        ratio = None
        if med and gbp is not None:  # both in pounds: never a pound price against a yuan median
            ratio = gbp / med
            value["price_position"] = round(ratio, 2)
            notes.append(f"{ratio:.1f}× the median price for {p.category or 'this category'} in your catalogue")
        elif gbp is None:
            notes.append("Price in a currency without a rate (Settings → Currency): not compared")
        score = None
        if q is not None:
            # Quality dominates; price matters, but less (you will pay more when quality justifies it).
            score = 50 + (q - 55) * 0.8 - (15 * math.log2(max(ratio, 0.1)) if ratio else 0)
        if cheaper_equivalent:
            notes.append(
                f"Very likely the same item listed at {cheaper_equivalent['price']:g} by "
                f"{cheaper_equivalent['seller']} ({cheaper_equivalent['saving_pct']:.0f}% cheaper)"
            )
            score = (score if score is not None else 50) - min(40, cheaper_equivalent["saving_pct"])
        value["notes"] = notes
        if score is not None:
            value["score"] = round(max(0, min(100, score)))
            value["confidence"] = round((0.3 if cheaper_equivalent else 0) + (0.5 if q is not None else 0) + 0.1, 2)
            if q is None:
                value["label"] = "Same item cheaper elsewhere"
            else:
                value["label"] = "Good" if value["score"] >= 60 else "Fair" if value["score"] >= 40 else "Weak"

    # Garment quality is what is known about this garment: construction and material. The seller's service
    # record is reported beside it (and ranked separately), never averaged into it.
    dims_conf = [construction["confidence"], material["confidence"]]
    overall_conf = round(sum(dims_conf) / len(dims_conf), 2)
    known = [d["score"] for d in (construction, material) if d["score"] is not None]
    if not known:
        status = "Unverified"
    elif overall_conf < 0.5:
        status = "Limited evidence"
    else:
        status = "Evidence-backed"

    return {
        "status": status,
        "overall_confidence": overall_conf,
        "quality_score": round(sum(known) / len(known)) if known else None,
        "dimensions": {
            "design_compatibility": {
                "score": None if design_score is None else round(design_score * 100),
                "label": "Personal fit with your taste",
                "confidence": round(design_confidence or 0.0, 2),
            },
            "construction": construction,
            "material": material,
            "seller_reliability": seller_dim,
            "value": value,
        },
    }


def evidence_view(e: Evidence) -> dict:
    return {
        "id": e.id,
        "kind": e.kind,
        "kind_label": KIND_LABEL.get(e.kind, e.kind),
        "aspect": e.aspect,
        "claim": e.claim,
        "polarity": e.polarity,
        "strength": e.strength,
        "source_url": e.source_url,
        "source_key": canonical_source(e.source_url),  # excerpts from one source share it (they count once)
        "source_title": e.source_title,
        "is_affiliate": e.is_affiliate,
        "captured_at": e.captured_at.isoformat() if e.captured_at else None,
        "created_by": e.created_by,
        "product_id": e.product_id,
        "seller_id": e.seller_id,
    }


def seller_reputation_status(db: Session, seller: Seller) -> str:
    ev = db.query(Evidence).filter(Evidence.seller_id == seller.id).all()
    ev += (
        db.query(Evidence)
        .join(Product)
        .filter(Product.seller_id == seller.id, Evidence.kind.in_(["buyer_report", "verified", "user_note"]))
        .all()
    )
    if not ev:
        return "unassessed"
    d = score_dimension(ev)
    if d["score"] is None:
        return "insufficient evidence"
    return "trusted" if d["score"] >= 70 else "mixed" if d["score"] >= 45 else "concerns"
