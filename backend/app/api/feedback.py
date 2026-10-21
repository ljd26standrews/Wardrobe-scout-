"""Feedback (love / like / maybe / dislike / skip / purchased) and saved lists."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Feedback, Product, SavedItem, WardrobeItem
from ..schemas import FeedbackIn, SavedPatch
from ..services import calibration, jobs
from ..services.presenter import Context, product_card
from ..services.recommender import REASONS, snapshot

router = APIRouter(prefix="/api", tags=["feedback"])

LIST_FOR_ACTION = {"love": "loved", "like": "liked", "maybe": "maybe"}


@router.get("/feedback/reasons")
def reasons():
    return [{"value": k, "label": v} for k, v in REASONS.items()]


@router.post("/feedback")
def give_feedback(body: FeedbackIn, db: Session = Depends(get_db)):
    p = db.get(Product, body.product_id)
    if p is None:
        raise HTTPException(404, "product not found")
    fb = Feedback(
        product_id=p.id,
        action=body.action,
        reasons=body.reasons,
        note=body.note,
        context=body.context,
        snapshot=snapshot(p),
    )
    db.add(fb)
    p.is_new = False
    saved = db.query(SavedItem).filter_by(product_id=p.id).one_or_none()
    wardrobe_id = None
    if body.action in LIST_FOR_ACTION:
        lst = LIST_FOR_ACTION[body.action]
        if saved is None:
            saved = SavedItem(product_id=p.id, list_name=lst, priority=1 if lst == "loved" else 2)
            db.add(saved)
        elif saved.list_name != "wishlist":
            saved.list_name = lst
    elif body.action in ("dislike", "unsave") and saved is not None:
        db.delete(saved)
    elif body.action == "purchased":
        if saved is not None:
            db.delete(saved)
        existing = db.query(WardrobeItem).filter_by(product_id=p.id).first()
        if existing is None:
            img = next((i for i in p.images if i.local_path), None)
            item = WardrobeItem(
                name=p.title_en or p.title_original,
                category=p.category,
                brand=p.brand,
                colours=p.colours or [],
                materials=p.materials or [],
                seasons=p.seasons or [],
                image_path=img.local_path if img else None,
                source_url=p.source_url if p.source_url.startswith("http") else None,
                product_id=p.id,
                embedding=p.image_embedding,
                embedding_backend=p.embedding_backend,
                notes=f"Purchased from {p.seller.name if p.seller else 'seller'}"
                + (f" for ¥{p.price:g}" if p.price else ""),
            )
            db.add(item)
            db.flush()
            wardrobe_id = item.id
    db.commit()
    if (
        body.action in (*LIST_FOR_ACTION, "purchased")
        and p.data_origin == "scraped"
        and p.analysis_state != "deep"
        and get_settings().deep_on_feedback
    ):
        # You are interested: compare it properly (full gallery, cross-seller photo matching).
        jobs.enqueue("deep_product", {"product_id": p.id})
    calibration.maybe_schedule(db)  # refit approval estimates once enough new decisions have arrived
    return {
        "ok": True,
        "feedback_id": fb.id,
        "saved": saved.list_name if saved and body.action in LIST_FOR_ACTION else None,
        "wardrobe_item_id": wardrobe_id,
    }


@router.get("/feedback")
def feedback_history(limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    rows = db.query(Feedback).order_by(Feedback.created_at.desc()).limit(limit).all()
    return [
        {
            "id": f.id,
            "product_id": f.product_id,
            "action": f.action,
            "reasons": [REASONS.get(r, r) for r in f.reasons or []],
            "note": f.note,
            "context": f.context,
            "title": (f.snapshot or {}).get("title"),
            "seller": (f.snapshot or {}).get("seller"),
            "created_at": f.created_at.isoformat(),
        }
        for f in rows
    ]


@router.delete("/feedback/{feedback_id}")
def undo_feedback(feedback_id: int, db: Session = Depends(get_db)):
    """Undo a decision (e.g. a mis-swipe). Saved-list state reverts to the previous decision."""
    fb = db.get(Feedback, feedback_id)
    if fb is None:
        raise HTTPException(404, "feedback not found")
    pid = fb.product_id
    undone_action = fb.action
    db.delete(fb)
    db.flush()
    if pid and undone_action == "purchased":
        still_purchased = db.query(Feedback).filter(Feedback.product_id == pid, Feedback.action == "purchased").first()
        if still_purchased is None:
            # Remove only the entry "purchased" created automatically, never one you added yourself.
            for item in db.query(WardrobeItem).filter(WardrobeItem.product_id == pid):
                if (item.notes or "").startswith("Purchased from"):
                    db.delete(item)
    if pid:
        prev = (
            db.query(Feedback)
            .filter(Feedback.product_id == pid, Feedback.action != "skip")
            .order_by(Feedback.created_at.desc())
            .first()
        )
        saved = db.query(SavedItem).filter_by(product_id=pid).one_or_none()
        if prev is not None and prev.action in LIST_FOR_ACTION:
            if saved is None:
                db.add(SavedItem(product_id=pid, list_name=LIST_FOR_ACTION[prev.action]))
            else:
                saved.list_name = LIST_FOR_ACTION[prev.action]
        elif saved is not None and saved.list_name != "wishlist":
            db.delete(saved)
    db.commit()
    calibration.maybe_schedule(db)  # an undone decision is no longer evidence
    return {"ok": True}


@router.get("/saved")
def saved_items(list_name: str | None = Query(None, alias="list"), db: Session = Depends(get_db)):
    q = db.query(SavedItem)
    if list_name:
        q = q.filter(SavedItem.list_name == list_name)
    rows = q.order_by(SavedItem.priority, SavedItem.updated_at.desc()).all()
    ctx = Context(db)
    out = []
    for s in rows:
        p = db.get(Product, s.product_id)
        if p is None:
            continue
        card = product_card(ctx, p)
        card["saved"] = {
            "list": s.list_name,
            "priority": s.priority,
            "note": s.note,
            "target_size": s.target_size,
            "added_at": s.added_at.isoformat(),
        }
        out.append(card)
    totals = {}
    for c in out:
        lst = c["saved"]["list"]
        t = totals.setdefault(lst, {"count": 0, "landed_gbp": 0.0, "with_estimate": 0})
        t["count"] += 1
        if c["landed_cost"]:
            t["landed_gbp"] = round(t["landed_gbp"] + c["landed_cost"]["total_gbp"], 2)
            t["with_estimate"] += 1
    return {"items": out, "totals": totals}


@router.patch("/saved/{product_id}")
def patch_saved(product_id: int, body: SavedPatch, db: Session = Depends(get_db)):
    s = db.query(SavedItem).filter_by(product_id=product_id).one_or_none()
    if s is None:
        if db.get(Product, product_id) is None:
            raise HTTPException(404, "product not found")
        s = SavedItem(product_id=product_id, list_name=body.list_name or "wishlist")
        db.add(s)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    db.commit()
    return {"ok": True, "list": s.list_name, "priority": s.priority}


@router.delete("/saved/{product_id}")
def delete_saved(product_id: int, db: Session = Depends(get_db)):
    s = db.query(SavedItem).filter_by(product_id=product_id).one_or_none()
    if s:
        p = db.get(Product, product_id)
        db.add(Feedback(product_id=product_id, action="unsave", snapshot=snapshot(p) if p else {}))
        db.delete(s)
        db.commit()
    return {"ok": True}
