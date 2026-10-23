"""Explicit rated outcomes: the labels the approval estimate is calibrated and evaluated on, and the reading of
your decisions the taste model learns from.

Three things are kept apart (the feedback contract):

* **Your taste: do you like the look?** This is what the labels are, what the taste model learns and what the
  gate's approval estimate is calibrated to: P(you like the look | the piece was shown to you and you said so).
* **Buying: would you buy it now?** Price, size, already owning something like it, or simply "not now". These
  shape value, eligibility and lists. They are never evidence that you dislike the look.
* **Quality: is it well made?** Evidence, kept in quality.py. "Poor apparent quality" and "not enough quality
  evidence" are about that, not about the look.

The reading of decisions is versioned (LABEL_VERSION). The original events are never rewritten; an earlier reading
stays available for comparison.

"aesthetic-v2" (current):
* Y = 1: love, or like (with or without "not now": "I like the look, but not now" is a clear yes to the look).
* Y = 0: a dislike with no reason, or with at least one reason about the look (silhouette, colour, logo, fabric,
  this specific item, not my style).
* No label, and the earlier label stands: a dislike only for buying, quality or listing reasons (too expensive,
  already own, wrong size, not enough quality evidence, poor apparent quality, wrong category, too repetitive); a
  purchase (buying is not proof of liking the look, nor of disliking it); a skip; a piece shown but not rated.
* No label, and an earlier label is withdrawn: maybe (ambiguous), unsave, Undo (the row is deleted).

"approval-v1" (earlier): Y = 1 for love, like or purchased; Y = 0 for any dislike or maybe; unsave withdraws.

Either way: one label per garment family, from its latest labelled decision (listings of one garment are never
independent evidence; a decision about a deleted listing counts with the listing now at its address, see
services/provenance.py), and seed examples from your notes are not rated recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Feedback, Product
from .provenance import live_families_by_url, norm_url

LABEL_VERSION = "aesthetic-v2"
VERSIONS = ("approval-v1", "aesthetic-v2")

# What a reason is about. Every reason a decision can carry is in exactly one group.
AESTHETIC_REASONS = frozenset({"silhouette", "colour", "logo", "fabric", "specific_item", "not_my_style"})
PURCHASE_REASONS = frozenset({"too_expensive", "already_own", "wrong_size", "not_now"})
QUALITY_REASONS = frozenset({"construction", "quality_unclear"})
LISTING_REASONS = frozenset({"wrong_category", "too_repetitive"})
POSITIVE_REASONS = frozenset({"not_now"})  # may accompany love or like; every other reason accompanies a dislike
REJECTION_REASONS = (AESTHETIC_REASONS | PURCHASE_REASONS | QUALITY_REASONS | LISTING_REASONS) - POSITIVE_REASONS

APPROVE = frozenset({"love", "like", "purchased"})  # approval-v1
NOT_APPROVED = frozenset({"dislike", "maybe"})  # approval-v1
WITHDRAW = "withdraw"


def reading(action: str, reasons: list[str] | None, version: str = LABEL_VERSION) -> int | str | None:
    """What one decision says about the look: 1, 0, WITHDRAW (an earlier label no longer stands) or None (nothing:
    an earlier label stands)."""
    if action == "unsave":
        return WITHDRAW
    if version == "approval-v1":
        return 1 if action in APPROVE else 0 if action in NOT_APPROVED else None
    if action in ("love", "like"):
        return 1
    if action == "dislike":
        given = set(reasons or [])
        return 0 if not given or given & AESTHETIC_REASONS else None
    if action == "maybe":
        return WITHDRAW
    return None  # purchased, skip


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


def latest_readings(events, version: str = LABEL_VERSION) -> dict[int | str, tuple[Feedback, int]]:
    """Per product (or a deleted product's URL), the latest decision that says something about the look and what
    it says. ``events`` in time order."""
    latest: dict[int | str, tuple[Feedback, int]] = {}
    for fb in events:
        if (fb.context or {}).get("seed"):
            continue
        key: int | str = fb.product_id if fb.product_id is not None else f"d{(fb.snapshot or {}).get('source_url')}"
        r = reading(fb.action, fb.reasons, version)
        if r == WITHDRAW:
            latest.pop(key, None)
        elif r is not None:
            latest[key] = (fb, r)
    return latest


def rated_outcomes(db: Session, before: datetime | None = None, version: str = LABEL_VERSION) -> list[RatedOutcome]:
    """The latest labelled decision per garment family, oldest first (``before``: as known at that time)."""
    query = db.query(Feedback)
    if before is not None:
        query = query.filter(Feedback.created_at < before)
    latest = latest_readings(query.order_by(Feedback.created_at, Feedback.id), version)
    ids = [k for k in latest if isinstance(k, int)]
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(ids))} if ids else {}
    # A decision about a deleted listing belongs to the garment now listed at its address, if any (a re-import).
    relinked = live_families_by_url(db, [(fb.snapshot or {}).get("source_url") for fb, _ in latest.values()
                                         if fb.product_id is None])  # fmt: skip
    per_family: dict[str, tuple[Feedback, int]] = {}
    for fb, y in latest.values():
        fam = family_of(products.get(fb.product_id) if fb.product_id else None, fb.snapshot, fb.product_id)
        if fb.product_id is None:
            fam = relinked.get(norm_url((fb.snapshot or {}).get("source_url")) or "", fam)
        cur = per_family.get(fam)
        if cur is None or (fb.created_at, fb.id) > (cur[0].created_at, cur[0].id):
            per_family[fam] = (fb, y)
    out = []
    for fam, (fb, y) in per_family.items():
        ctx = fb.context or {}
        out.append(
            RatedOutcome(
                family=fam, product_id=fb.product_id, feedback_id=fb.id, y=y,
                action=fb.action, reasons=list(fb.reasons or []), created_at=fb.created_at,
                feed_id=ctx.get("feed_id"), position=ctx.get("position"), surface=ctx.get("surface"),
                explore=bool(ctx.get("explore")), snapshot=fb.snapshot or {},
            )
        )  # fmt: skip
    out.sort(key=lambda o: (o.created_at, o.feedback_id))
    return out
