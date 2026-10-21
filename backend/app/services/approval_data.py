"""Explicit rated outcomes: the labels the approval estimate is calibrated and evaluated on.

The quantity being estimated is P(you approve | the piece was shown to you and you rated it).

* Y = 1, approved: love, like (save) or purchased.
* Y = 0, not approved: a dislike for any reason, or "maybe" (a maybe is not an approval).
* No label: a skip; a piece displayed but not rated; a piece never shown (the taste model's "typical items"
  background sample is a modelling device, never a rejection); a decision you undid (Undo deletes the
  row) or withdrew ("unsave"); seed examples from your notes (not rated recommendations).
* One label per garment family: the latest decision on any listing of it. Photos or listings of one
  garment are never independent evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Feedback, Product

# How decisions are read as labels. A calibration fitted under one reading is never applied under another.
LABEL_VERSION = "approval-v1"
APPROVE = frozenset({"love", "like", "purchased"})
NOT_APPROVED = frozenset({"dislike", "maybe"})


@dataclass
class RatedOutcome:
    family: str  # "g<group id>", "p<product id>", or "d<source url>" for a product since deleted
    product_id: int | None
    feedback_id: int
    y: int
    action: str
    reasons: list[str]
    created_at: datetime
    feed_id: str | None = None
    position: int | None = None
    surface: str | None = None
    explore: bool = False
    snapshot: dict = field(default_factory=dict)


def family_of(product: Product | None, snapshot: dict | None, product_id: int | None) -> str:
    if product is not None:
        return f"g{product.group_id}" if product.group_id is not None else f"p{product.id}"
    if product_id is not None:
        return f"p{product_id}"
    return f"d{(snapshot or {}).get('source_url') or ''}"


def rated_outcomes(db: Session, before: datetime | None = None) -> list[RatedOutcome]:
    """The latest explicit decision per garment family, oldest first (``before``: as known at that time)."""
    latest: dict[int | str, Feedback] = {}
    query = db.query(Feedback)
    if before is not None:
        query = query.filter(Feedback.created_at < before)
    for fb in query.order_by(Feedback.created_at, Feedback.id):
        if (fb.context or {}).get("seed"):
            continue
        key: int | str = fb.product_id if fb.product_id is not None else f"d{(fb.snapshot or {}).get('source_url')}"
        if fb.action == "unsave":
            latest.pop(key, None)
        elif fb.action in APPROVE or fb.action in NOT_APPROVED:
            latest[key] = fb
    ids = [k for k in latest if isinstance(k, int)]
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(ids))} if ids else {}
    per_family: dict[str, Feedback] = {}
    for fb in latest.values():
        fam = family_of(products.get(fb.product_id) if fb.product_id else None, fb.snapshot, fb.product_id)
        cur = per_family.get(fam)
        if cur is None or (fb.created_at, fb.id) > (cur.created_at, cur.id):
            per_family[fam] = fb
    out = []
    for fam, fb in per_family.items():
        ctx = fb.context or {}
        out.append(
            RatedOutcome(
                family=fam, product_id=fb.product_id, feedback_id=fb.id, y=int(fb.action in APPROVE),
                action=fb.action, reasons=list(fb.reasons or []), created_at=fb.created_at,
                feed_id=ctx.get("feed_id"), position=ctx.get("position"), surface=ctx.get("surface"),
                explore=bool(ctx.get("explore")), snapshot=fb.snapshot or {},
            )
        )  # fmt: skip
    out.sort(key=lambda o: (o.created_at, o.feedback_id))
    return out
