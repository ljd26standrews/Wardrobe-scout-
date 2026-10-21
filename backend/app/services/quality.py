"""Evidence-based quality assessment.

Principles
----------
* Price never raises a quality score.
* Seller claims carry very little weight; AI visual assessments even less, and
  are never used for fibre content or durability.
* If the weighted evidence for a dimension is below a threshold the dimension
  is reported as **Unverified** (score ``None``), not as good or bad.
"""

from __future__ import annotations

import math
from statistics import median

from sqlalchemy.orm import Session

from ..models import Evidence, Product, Seller

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


def score_dimension(items: list[Evidence]) -> dict:
    total = sum(_weight(e) for e in items)
    signed = sum(_weight(e) * e.polarity for e in items)
    directional = sum(_weight(e) for e in items if e.polarity != 0)
    confidence = 1 - math.exp(-total / 1.2)
    by_kind: dict[str, int] = {}
    for e in items:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    independent = sum(1 for e in items if e.kind in ("verified", "buyer_report", "user_note"))
    if confidence < UNVERIFIED_BELOW or independent == 0:
        label = "Seller claim only" if by_kind and set(by_kind) <= {"seller_claim", "ai_visual"} else "Unverified"
        return {"score": None, "label": label, "confidence": round(confidence, 2), "evidence_counts": by_kind}
    score = 50 + 50 * signed / (directional + 0.8) if directional else 55.0
    score = max(0.0, min(100.0, score))
    label = "Strong" if score >= 75 else "Good" if score >= 60 else "Mixed" if score >= 40 else "Poor"
    return {"score": round(score), "label": label, "confidence": round(confidence, 2), "evidence_counts": by_kind}


def category_price_stats(db: Session) -> dict[str, float]:
    by_cat: dict[str, list[float]] = {}
    for cat, price in db.query(Product.category, Product.price).filter(
        Product.price.isnot(None), Product.status == "active", Product.currency.in_(["CNY", ""])
    ):
        by_cat.setdefault(cat or "other", []).append(price)
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

    # Value: price relative to category median, only meaningful with quality evidence
    value = {"score": None, "label": "Unknown", "confidence": 0.0, "notes": []}
    if p.price:
        stats = price_stats if price_stats is not None else category_price_stats(db)
        med = stats.get(p.category or "other")
        q_scores = [d["score"] for d in (construction, material) if d["score"] is not None]
        q = sum(q_scores) / len(q_scores) if q_scores else None
        notes = []
        if med:
            ratio = p.price / med
            notes.append(f"{ratio:.1f}× the median price for {p.category or 'this category'} in your catalogue")
            base = 60 - 25 * math.log2(max(ratio, 0.1))
            if q is not None:
                base += (q - 55) * 0.6
            value["score"] = round(max(0, min(100, base)))
        if cheaper_equivalent:
            notes.append(
                f"Same photos listed at {cheaper_equivalent['price']:g} by {cheaper_equivalent['seller']} "
                f"({cheaper_equivalent['saving_pct']:.0f}% cheaper)"
            )
            if value["score"] is not None:
                value["score"] = max(0, value["score"] - min(30, cheaper_equivalent["saving_pct"]))
        value["notes"] = notes
        value["confidence"] = round(0.25 + (0.5 if q is not None else 0) + (0.15 if med else 0), 2)
        if value["score"] is None:
            value["label"] = "Unknown"
        elif q is None:
            value["label"] = "Uncertain — quality not demonstrated"
        else:
            value["label"] = "Good" if value["score"] >= 60 else "Fair" if value["score"] >= 40 else "Weak"

    dims_conf = [construction["confidence"], material["confidence"], seller_dim["confidence"]]
    overall_conf = round(sum(dims_conf) / 3, 2)
    known = [d["score"] for d in (construction, material, seller_dim) if d["score"] is not None]
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
