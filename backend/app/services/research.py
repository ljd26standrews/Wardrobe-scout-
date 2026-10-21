"""Research helpers: AI visual assessment -> evidence, lead acceptance."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import CommunityLead, Evidence, Product
from . import ai


def ai_visual_assess(db: Session, product_id: int) -> dict:
    p = db.get(Product, product_id)
    if p is None:
        return {"error": "product not found"}
    paths = [i.local_path for i in p.images if i.local_path][:4]
    if not paths:
        return {"error": "no downloaded images to assess"}
    try:
        data = ai.visual_assessment(paths, p.title_en or p.title_original)
    except ai.AIUnavailable as e:
        return {"error": str(e)}
    # Replace previous AI visual evidence for this product
    p.evidence = [e for e in p.evidence if e.kind != "ai_visual"]
    added = 0
    for obs in data.get("observations", [])[:10]:
        aspect = obs.get("aspect", "construction")
        if aspect in ("material", "gsm"):
            continue  # never accept AI claims about fibre content / weight
        p.evidence.append(
            Evidence(
                kind="ai_visual",
                aspect=aspect,
                claim=f"AI photo observation: {obs.get('observation', '')}",
                polarity=int(obs.get("polarity", 0)),
                strength=float(obs.get("confidence", 0.3)) * 0.6,
                source_title="AI visual assessment (photos only)",
                source_url=p.source_url,
                created_by="ai",
            )
        )
        added += 1
    if data.get("logo_visibility") in ("none", "subtle", "visible") and p.logo_level == "unknown":
        p.logo_level = data["logo_visibility"]
    db.commit()
    return {"added": added, "caveats": data.get("caveats"), "silhouette": data.get("silhouette")}


def accept_lead(
    db: Session,
    lead: CommunityLead,
    *,
    aspect: str,
    polarity: int,
    claim: str,
    product_id: int | None,
    seller_id: int | None,
    is_affiliate: bool | None = None,
    strength: float = 0.7,
) -> Evidence:
    ev = Evidence(
        kind="buyer_report",
        aspect=aspect,
        claim=claim,
        polarity=polarity,
        strength=strength,
        source_url=lead.url,
        source_title=lead.title[:300],
        captured_at=lead.posted_at or lead.fetched_at,
        is_affiliate=lead.possible_affiliate if is_affiliate is None else is_affiliate,
        product_id=product_id or lead.product_id,
        seller_id=seller_id or lead.seller_id,
        created_by="user",
    )
    db.add(ev)
    lead.status = "accepted"
    db.commit()
    return ev
