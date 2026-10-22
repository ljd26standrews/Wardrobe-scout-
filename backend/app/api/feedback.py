"""Feedback (love / like / maybe / dislike / skip / purchased) and saved lists.

What each decision means (approval_data has the contract): love and like say you like the look ("not now" adds
that you would not buy it now, and saves it to Later); a dislike's reasons say whether it was about the look, buying
or quality; maybe is undecided; purchased moves the piece to your wardrobe. Saving a piece to a list always means
you like the look, wherever it is done.
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Feedback, Product, SavedItem, WardrobeItem
from ..schemas import FeedbackIn, ReasonsIn, SavedPatch
from ..services import calibration, jobs
from ..services.approval_data import LABEL_VERSION, POSITIVE_REASONS, REJECTION_REASONS, latest_readings
from ..services.presenter import Context, product_card
from ..services.recommender import REASONS, snapshot

router = APIRouter(prefix="/api", tags=["feedback"])

LIST_FOR_ACTION = {"love": "loved", "like": "liked", "maybe": "maybe"}


def list_for(action: str, reasons: list[str] | None) -> str | None:
    """The saved list a decision puts a piece in ("not now" goes to Later), or None."""
    if action in ("love", "like") and "not_now" in (reasons or []):
        return "later"
    return LIST_FOR_ACTION.get(action)


# Context keys only the server writes: what Undo needs to put things back exactly and to recognise its own decision.
SERVER_CONTEXT = frozenset({"undo_token", "saved_before", "saved_after", "removed"})
SAVED_FIELDS = ("list_name", "priority", "note", "target_size")


def saved_state(db: Session, product_id: int) -> dict | None:
    """A piece's saved details (list, priority, note, target size), or None when it is in no list."""
    s = db.query(SavedItem).filter_by(product_id=product_id).one_or_none()
    if s is None:
        return None
    return {**{k: getattr(s, k) for k in SAVED_FIELDS}, "added_at": s.added_at.isoformat() if s.added_at else None}


def _same(a: dict | None, b: dict | None) -> bool:
    """Equal saved details (when it was added aside)."""
    if a is None or b is None:
        return a is b
    return all(a.get(k) == b.get(k) for k in SAVED_FIELDS)


def _restore_saved(db: Session, product_id: int, state: dict | None) -> None:
    s = db.query(SavedItem).filter_by(product_id=product_id).one_or_none()
    if state is None:
        if s is not None:
            db.delete(s)
        return
    if s is None:
        s = SavedItem(product_id=product_id, list_name=state["list_name"])
        db.add(s)
    for k in SAVED_FIELDS:
        setattr(s, k, state.get(k))
    if state.get("added_at"):
        s.added_at = datetime.fromisoformat(state["added_at"])


def _undo_record(db: Session, fb: Feedback, before: dict | None) -> None:
    """Record, with the decision, the saved details before and after it and the token its Undo must present."""
    db.flush()
    fb.context = {**(fb.context or {}), "undo_token": secrets.token_hex(8), "saved_before": before,
                  "saved_after": saved_state(db, fb.product_id) if fb.product_id is not None else None}  # fmt: skip


def _check_reasons(action: str, reasons: list[str]) -> None:
    allowed = POSITIVE_REASONS if action in ("love", "like") else REJECTION_REASONS if action == "dislike" else set()
    if wrong := [r for r in reasons if r not in allowed]:
        raise HTTPException(422, f"reasons {wrong} do not go with {action}")


@router.get("/feedback/reasons")
def reasons():
    return [{"value": k, "label": v} for k, v in REASONS.items()]


@router.post("/feedback")
def give_feedback(body: FeedbackIn, db: Session = Depends(get_db)):
    p = db.get(Product, body.product_id)
    if p is None:
        raise HTTPException(404, "product not found")
    _check_reasons(body.action, body.reasons)
    before = saved_state(db, p.id)
    fb = Feedback(
        product_id=p.id,
        action=body.action,
        reasons=body.reasons,
        note=body.note,
        context={k: v for k, v in body.context.items() if k not in SERVER_CONTEXT},
        snapshot=snapshot(p),
    )
    db.add(fb)
    p.is_new = False
    saved = db.query(SavedItem).filter_by(product_id=p.id).one_or_none()
    wardrobe_id = None
    if lst := list_for(body.action, body.reasons):
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
    _undo_record(db, fb, before)
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
        "undo_token": fb.context["undo_token"],
        "saved": saved.list_name if saved and list_for(body.action, body.reasons) else None,
        "wardrobe_item_id": wardrobe_id,
    }


@router.post("/feedback/{feedback_id}/reasons")
def add_reasons(feedback_id: int, body: ReasonsIn, db: Session = Depends(get_db)):
    """Reasons for a dislike, given after it was recorded (Discover never makes you choose before deciding). They
    complete that same decision, so only your latest decision about the piece can take them."""
    fb = db.get(Feedback, feedback_id)
    if fb is None:
        raise HTTPException(404, "feedback not found")
    if fb.action != "dislike":
        raise HTTPException(422, "reasons can only be added to a dislike")
    _check_reasons("dislike", body.reasons)
    later = (
        db.query(Feedback.id)
        .filter(Feedback.product_id == fb.product_id, Feedback.action != "skip", Feedback.id > fb.id)
        .first()
    )
    if fb.product_id is not None and later is not None:
        raise HTTPException(409, "you have decided about this piece again since; add reasons to that decision")
    fb.reasons = list(body.reasons)
    if body.note:
        fb.note = body.note
    db.commit()
    calibration.maybe_schedule(db)  # a reason can change what the decision says about the look
    return {"ok": True, "feedback_id": fb.id, "reasons": fb.reasons}


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
            "undo_token": (f.context or {}).get("undo_token"),
        }
        for f in rows
    ]


@router.delete("/feedback/{feedback_id}")
def undo_feedback(feedback_id: int, token: str | None = Query(None, max_length=64), db: Session = Depends(get_db)):
    """Undo a decision (e.g. a mis-swipe): it is removed and the piece's saved details are put back exactly as they
    were before it (list, priority, note, target size).

    Newer intent always wins: a decision you have made about the piece since, or a change to it in Saved since, is
    never overwritten; the Undo is refused (409) and nothing changes. With the ``token`` returned with the decision,
    Undo is idempotent: repeating it, or retrying after a lost response, changes nothing further, even if the
    decision's id has since been given to a newer decision."""
    fb = db.get(Feedback, feedback_id)
    ctx = (fb.context or {}) if fb is not None else {}
    if fb is None or (token is not None and ctx.get("undo_token") != token):
        if token is not None:
            return {"ok": True, "already_undone": True}
        raise HTTPException(404, "feedback not found")
    pid = fb.product_id
    if pid is not None and fb.action != "skip":
        newer = (
            db.query(Feedback.id)
            .filter(Feedback.product_id == pid, Feedback.action != "skip", Feedback.id != fb.id,
                    or_(Feedback.created_at > fb.created_at, and_(Feedback.created_at == fb.created_at, Feedback.id > fb.id)))
            .first()
        )  # fmt: skip
        if newer is not None:
            raise HTTPException(409, "You have decided about this piece again since. Undo that decision first; "
                                     "nothing was changed.")  # fmt: skip
        if "saved_after" in ctx and not _same(saved_state(db, pid), ctx["saved_after"]):
            raise HTTPException(409, "This piece was changed in Saved since. Undo would discard that change, so "
                                     "nothing was changed.")  # fmt: skip
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
    if pid and undone_action != "skip":
        if "saved_before" in ctx:
            _restore_saved(db, pid, ctx["saved_before"])
        else:  # a decision recorded before saved details were kept with it
            _legacy_restore(db, pid, ctx.get("removed") if undone_action == "unsave" else None)
    db.commit()
    calibration.maybe_schedule(db)  # an undone decision is no longer evidence
    return {"ok": True, "saved": saved_state(db, pid) if pid else None}


def _legacy_restore(db: Session, pid: int, kept: dict | None) -> None:
    """Saved state after undoing a decision recorded without its saved details: the previous decision's list."""
    prev = (
        db.query(Feedback)
        .filter(Feedback.product_id == pid, Feedback.action != "skip")
        .order_by(Feedback.created_at.desc())
        .first()
    )
    saved = db.query(SavedItem).filter_by(product_id=pid).one_or_none()
    if prev is not None and (lst := list_for(prev.action, prev.reasons)):
        if saved is None:
            db.add(SavedItem(product_id=pid, list_name=lst))
        else:
            saved.list_name = lst
    elif saved is not None and saved.list_name != "wishlist":
        db.delete(saved)
    if kept:
        # Undoing a removal puts the piece back exactly as it was: its list, priority and purchase notes.
        db.flush()
        saved = db.query(SavedItem).filter_by(product_id=pid).one_or_none()
        if saved is None:
            saved = SavedItem(product_id=pid, list_name=kept.get("list_name") or "wishlist")
            db.add(saved)
        for k in SAVED_FIELDS:
            if k in kept:
                setattr(saved, k, kept[k])


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
    """Save a piece to a list, move it between lists, or note what to check before buying. Saving a piece that is in
    no list records that you like the look, as the Like button does; moving it or adding a note records nothing.
    With ``expect`` (an Undo of a move), the change applies only if the piece is still saved exactly as expected:
    otherwise it was changed since, and the newer change stands (409)."""
    if (body.expect is not None or body.expect_set) and not _same(
        saved_state(db, product_id), body.expect.model_dump() if body.expect else None
    ):
        raise HTTPException(409, "This piece was changed in Saved since. Undo would discard that change, so nothing "
                                 "was changed.")  # fmt: skip
    s = db.query(SavedItem).filter_by(product_id=product_id).one_or_none()
    fb = None
    if s is None:
        p = db.get(Product, product_id)
        if p is None:
            raise HTTPException(404, "product not found")
        s = SavedItem(product_id=product_id, list_name=body.list_name or "wishlist")
        db.add(s)
        events = db.query(Feedback).filter(Feedback.product_id == product_id).order_by(Feedback.created_at, Feedback.id)
        said = latest_readings(events, LABEL_VERSION).get(product_id)
        if said is None or said[1] != 1:  # not already a like: saving says you like the look
            fb = Feedback(product_id=product_id, action="like", context={"surface": "save"}, snapshot=snapshot(p))
            db.add(fb)
            p.is_new = False
    for k, v in body.model_dump(exclude_unset=True, exclude={"expect", "expect_set"}).items():
        setattr(s, k, v)
    if fb is not None:
        _undo_record(db, fb, None)
    db.commit()
    calibration.maybe_schedule(db)
    out = {"ok": True, "list": s.list_name, "priority": s.priority, "saved": saved_state(db, product_id)}
    if fb is not None:
        out |= {"feedback_id": fb.id, "undo_token": fb.context["undo_token"]}
    return out


@router.delete("/saved/{product_id}")
def delete_saved(product_id: int, db: Session = Depends(get_db)):
    s = db.query(SavedItem).filter_by(product_id=product_id).one_or_none()
    if s is None:
        return {"ok": True, "feedback_id": None}
    p = db.get(Product, product_id)
    before = saved_state(db, product_id)
    fb = Feedback(product_id=product_id, action="unsave", context={}, snapshot=snapshot(p) if p else {})
    db.add(fb)
    db.delete(s)
    _undo_record(db, fb, before)
    db.commit()
    calibration.maybe_schedule(db)  # an unsaved approval is no longer evidence
    # Undo is DELETE /api/feedback/{feedback_id}?token=...: it restores the list, priority and notes kept above.
    return {"ok": True, "feedback_id": fb.id, "undo_token": fb.context["undo_token"]}
