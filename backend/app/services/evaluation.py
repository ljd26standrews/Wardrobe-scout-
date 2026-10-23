"""Collecting your own judgements to measure the recommender honestly (see ``eval/real.py``).

Principles (audit, Oct 2026):

* **Unbiased test items.** Products are drawn uniformly at random from undecided, readable listings, not
  chosen by the model, so a ranking cannot look good merely because it picked what you were shown.
* **One product per family.** Listings of the same garment (``Product.group_id``) are one test item, so a
  re-posted piece cannot count several times or sit on both sides of a train/test split.
* **Taste on its own.** The grade is about the look only; owning something similar, the price and unclear
  quality are separate flags, so purchase objections never become "dislikes".
* **Measuring, not training.** Judgements never feed the taste model; the evaluator removes any feedback
  about a judged family before fitting.

The order of the sample is a stable hash of the family under a per-installation salt, so refreshing the page
shows the same next item and the sequence cannot be steered.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy.orm import Session

from ..models import Feedback, Judgement, Product
from .provenance import live_families_by_url
from .settings_store import get_setting, set_setting

MIN_TEST_JUDGEMENTS = 40  # below this, the evaluator reports nothing
MIN_TEST_SELLERS = 3


def family_key(p: Product) -> str:
    return f"g{p.group_id}" if p.group_id is not None else f"p{p.id}"


def _salt(db: Session) -> str:
    cfg = get_setting(db, "evaluation") or {}
    if not cfg.get("salt"):
        cfg = {**cfg, "salt": secrets.token_hex(8)}
        set_setting(db, "evaluation", cfg)
    return cfg["salt"]


def _order(salt: str, key: str) -> str:
    return hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()


def eligible_families(db: Session) -> dict[str, Product]:
    """Readable, undecided listings, one per family: the pool test items are drawn from.

    Readable means a title, a stated price and at least one analysed photo. Undecided means no Discover
    decision on any listing of the family, nor on a deleted listing at the address of one (skips don't count: they
    teach nothing)."""
    decided = {
        pid
        for (pid,) in db.query(Feedback.product_id).filter(Feedback.product_id.isnot(None), Feedback.action != "skip")
    }
    orphans = db.query(Feedback.snapshot).filter(Feedback.product_id.is_(None), Feedback.action != "skip")
    relinked = set(live_families_by_url(db, [(snap or {}).get("source_url") for (snap,) in orphans]).values())
    products = (
        db.query(Product)
        .filter(
            Product.status == "active",
            Product.data_origin != "seed",
            Product.price.isnot(None),
            Product.image_embedding.isnot(None),
        )
        .all()
    )
    groups = db.query(Product.group_id).filter(Product.id.in_(decided), Product.group_id.isnot(None)) if decided else []
    decided_families = {f"p{pid}" for pid in decided} | {f"g{g}" for (g,) in groups} | relinked
    pool: dict[str, Product] = {}
    for p in sorted(products, key=lambda p: p.id):
        k = family_key(p)
        if p.title_original and k not in decided_families and k not in pool:
            pool[k] = p
    return pool


def judged_families(db: Session) -> set[str]:
    """Families already judged: as recorded, as they are now (a later match can merge two families), and the family
    of a listing now at a judged listing's address (a re-import)."""
    out: set[str] = set()
    urls = []
    for j, p in db.query(Judgement, Product).outerjoin(Product, Judgement.product_id == Product.id):
        out.add(j.family_key)
        if p is not None:
            out.add(family_key(p))
        urls.append((j.snapshot or {}).get("source_url"))
    return out | set(live_families_by_url(db, urls).values())


def next_product(db: Session) -> Product | None:
    pool = eligible_families(db)
    todo = set(pool) - judged_families(db)
    if not todo:
        return None
    salt = _salt(db)
    return pool[min(todo, key=lambda k: _order(salt, k))]


def status(db: Session) -> dict:
    rows = db.query(Judgement).all()
    sellers = {(j.snapshot or {}).get("seller_id") for j in rows} - {None}
    categories = {(j.snapshot or {}).get("category") for j in rows} - {None}
    remaining = len(set(eligible_families(db)) - judged_families(db))
    return {
        "judgements": len(rows),
        "liked": sum(j.taste >= 2 for j in rows),
        "sellers": len(sellers),
        "categories": len(categories),
        "remaining": remaining,
        "needed": MIN_TEST_JUDGEMENTS,
        "ready": len(rows) >= MIN_TEST_JUDGEMENTS and len(sellers) >= MIN_TEST_SELLERS,
    }
