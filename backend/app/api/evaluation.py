"""Evaluation sessions: you grade uniformly sampled pieces so the recommender can be measured (see
``services/evaluation.py`` and ``eval/real.py``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Judgement, Product
from ..schemas import JudgementIn
from ..services import evaluation as ev
from ..services.presenter import Context, product_card
from ..services.recommender import snapshot

router = APIRouter(prefix="/api/eval", tags=["evaluation"])

# Blinded view: what the piece looks like and costs. No seller, ranking reasons or quality scores, which
# would turn a taste label into a judgement of the app's own suggestions.
BLIND_FIELDS = ("id", "title", "title_original", "category", "colours", "images", "price", "currency", "price_gbp")


@router.get("/status")
def eval_status(db: Session = Depends(get_db)):
    return ev.status(db)


@router.get("/next")
def eval_next(db: Session = Depends(get_db)):
    p = ev.next_product(db)
    if p is None:
        return {"product": None, "status": ev.status(db)}
    card = product_card(Context(db), p)
    return {"product": {k: card.get(k) for k in BLIND_FIELDS}, "status": ev.status(db)}


@router.post("/judgements")
def add_judgement(body: JudgementIn, db: Session = Depends(get_db)):
    p = db.get(Product, body.product_id)
    if p is None:
        raise HTTPException(404, "product not found")
    key = ev.family_key(p)
    if key not in ev.eligible_families(db):
        raise HTTPException(409, "this piece is not in the evaluation pool (already decided, or not readable)")
    if key in ev.judged_families(db):
        raise HTTPException(409, "this piece (or another listing of it) is already judged")
    j = Judgement(product_id=p.id, family_key=key, taste=body.taste, owned_similar=body.owned_similar,
                  too_expensive=body.too_expensive, quality_unclear=body.quality_unclear, note=body.note,
                  snapshot=snapshot(p))  # fmt: skip
    db.add(j)
    db.commit()
    return {"id": j.id, "status": ev.status(db)}


@router.delete("/judgements/{judgement_id}")
def undo_judgement(judgement_id: int, product_id: int | None = Query(None), db: Session = Depends(get_db)):
    """Undo a grade. With ``product_id`` (the graded piece) it is idempotent: a repeat, or an id since given to a
    newer grade of another piece, changes nothing."""
    j = db.get(Judgement, judgement_id)
    if j is None or (product_id is not None and j.product_id != product_id):
        if product_id is not None:
            return {"ok": True, "already_undone": True, "status": ev.status(db)}
        raise HTTPException(404, "judgement not found")
    db.delete(j)
    db.commit()
    return {"ok": True, "status": ev.status(db)}


@router.get("/judgements")
def list_judgements(limit: int = Query(200, ge=1, le=2000), db: Session = Depends(get_db)):
    rows = db.query(Judgement).order_by(Judgement.created_at.desc()).limit(limit).all()
    return [
        {"id": j.id, "product_id": j.product_id, "title": (j.snapshot or {}).get("title"), "taste": j.taste,
         "owned_similar": j.owned_similar, "too_expensive": j.too_expensive, "quality_unclear": j.quality_unclear,
         "created_at": j.created_at.isoformat()}
        for j in rows
    ]  # fmt: skip
