"""My Wardrobe and inspiration-image endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion.parsing import parse_category, parse_colours, parse_materials
from ..models import InspirationImage, Product, WardrobeItem
from ..services import embeddings as emb
from ..services.images import ImageError, phash_of, store_image_bytes
from ..services.presenter import Context, product_card
from ..services.settings_store import get_setting, set_setting
from ..services.wardrobe import analyse, complement_scores
from .uploads import max_image_upload, read_capped

router = APIRouter(prefix="/api", tags=["wardrobe"])


def _item_view(w: WardrobeItem) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "category": w.category,
        "brand": w.brand,
        "colours": w.colours,
        "materials": w.materials,
        "seasons": w.seasons,
        "notes": w.notes,
        "rating": w.rating,
        "source_url": w.source_url,
        "product_id": w.product_id,
        "image": f"/api/files/{w.image_path}" if w.image_path else None,
        "added_at": w.added_at.isoformat(),
    }


def _csv(v: str | None) -> list[str]:
    return [x.strip().lower() for x in (v or "").split(",") if x.strip()]


@router.get("/wardrobe")
def wardrobe(season: str | None = None, db: Session = Depends(get_db)):
    items = db.query(WardrobeItem).order_by(WardrobeItem.category, WardrobeItem.name).all()
    budget = get_setting(db, "budget")
    return {"items": [_item_view(w) for w in items], "analysis": analyse(db, season), "budget": budget}


@router.post("/wardrobe")
async def add_wardrobe_item(
    name: str = Form(...),
    category: str | None = Form(None),
    brand: str | None = Form(None),
    colours: str | None = Form(None),
    materials: str | None = Form(None),
    seasons: str | None = Form(None),
    notes: str | None = Form(None),
    rating: int | None = Form(None),
    source_url: str | None = Form(None),
    product_id: int | None = Form(None),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    text = f"{name} {notes or ''}"
    item = WardrobeItem(
        name=name,
        category=(category or parse_category(text) or None),
        brand=brand,
        colours=_csv(colours) or parse_colours(text),
        materials=parse_materials(materials or text),
        seasons=_csv(seasons),
        notes=notes,
        rating=rating,
        source_url=source_url,
        product_id=product_id,
    )
    if product_id:
        p = db.get(Product, product_id)
        if p:
            item.category = item.category or p.category
            item.colours = item.colours or p.colours
            item.materials = item.materials or p.materials
            item.embedding, item.embedding_backend = p.image_embedding, p.embedding_backend
            img = next((i for i in p.images if i.local_path), None)
            item.image_path = img.local_path if img else None
    if image is not None:
        data = await read_capped(image, max_image_upload())
        if data:
            try:
                rel, pil = store_image_bytes(data, "uploads")
            except ImageError as e:
                raise HTTPException(422, str(e)) from e
            backend = emb.get_image_backend()
            item.image_path = rel
            item.embedding = emb.to_bytes(backend.embed(pil))
            item.embedding_backend = backend.name
    db.add(item)
    db.commit()
    return _item_view(item)


@router.patch("/wardrobe/{item_id}")
def patch_wardrobe_item(item_id: int, body: dict, db: Session = Depends(get_db)):
    w = db.get(WardrobeItem, item_id)
    if w is None:
        raise HTTPException(404, "item not found")
    allowed = {"name", "category", "brand", "colours", "seasons", "notes", "rating"}
    for k, v in body.items():
        if k in allowed:
            setattr(w, k, v)
    db.commit()
    return _item_view(w)


@router.delete("/wardrobe/{item_id}")
def delete_wardrobe_item(item_id: int, db: Session = Depends(get_db)):
    w = db.get(WardrobeItem, item_id)
    if w:
        db.delete(w)
        db.commit()
    return {"ok": True}


@router.get("/wardrobe/fit/{product_id}")
def wardrobe_fit(product_id: int, db: Session = Depends(get_db)):
    """Does a candidate purchase complement what you own?"""
    p = db.get(Product, product_id)
    if p is None:
        raise HTTPException(404, "product not found")
    score = complement_scores(db, [p]).get(p.id)
    owned_same = [_item_view(w) for w in db.query(WardrobeItem).filter(WardrobeItem.category == p.category)]
    return {"product": product_card(Context(db), p), "complement": score, "owned_same_category": owned_same}


@router.put("/budget")
def set_budget(body: dict, db: Session = Depends(get_db)):
    cur = get_setting(db, "budget")
    cur.update(
        {k: (float(v) if v not in (None, "") else None) for k, v in body.items() if k in ("monthly_gbp", "season_gbp")}
    )
    return set_setting(db, "budget", cur)


# ------------------------------------------------------------------ inspiration
@router.get("/inspiration")
def inspiration(db: Session = Depends(get_db)):
    return [
        {
            "id": i.id,
            "image": f"/api/files/{i.path}",
            "note": i.note,
            "polarity": i.polarity,
            "tags": i.tags,
            "added_at": i.added_at.isoformat(),
        }
        for i in db.query(InspirationImage).order_by(InspirationImage.added_at.desc())
    ]


@router.post("/inspiration")
async def add_inspiration(
    image: UploadFile = File(...), note: str | None = Form(None), polarity: int = Form(1), db: Session = Depends(get_db)
):
    data = await read_capped(image, max_image_upload())
    try:
        rel, pil = store_image_bytes(data, "uploads")
    except ImageError as e:
        raise HTTPException(422, str(e)) from e
    backend = emb.get_image_backend()
    item = InspirationImage(
        path=rel,
        note=note,
        polarity=1 if polarity >= 0 else -1,
        phash=phash_of(pil),
        embedding=emb.to_bytes(backend.embed(pil)),
        embedding_backend=backend.name,
    )
    db.add(item)
    db.commit()
    return {"id": item.id, "image": f"/api/files/{rel}"}


@router.delete("/inspiration/{item_id}")
def delete_inspiration(item_id: int, db: Session = Depends(get_db)):
    i = db.get(InspirationImage, item_id)
    if i:
        db.delete(i)
        db.commit()
    return {"ok": True}
