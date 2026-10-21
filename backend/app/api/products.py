"""Discovery feed, product browsing, comparison and evidence endpoints."""

from __future__ import annotations

import datetime as dt
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..ingestion.parsing import materials_to_text, parse_materials
from ..models import Evidence, Product
from ..schemas import EvidenceIn, MatchDecision, ProductPatch
from ..services import dedup, jobs
from ..services import embeddings as emb
from ..services.presenter import Context, comparison, product_card
from ..services.recommender import build_feed, build_profile, feed_candidates, rank_products
from ..services.settings_store import get_setting
from ..services.wardrobe import complement_scores

router = APIRouter(prefix="/api", tags=["products"])


def _apply_filters(
    products: list[Product],
    *,
    q: str | None,
    brand: list[str] | None,
    seller_id: list[int] | None,
    category: list[str] | None,
    material: list[str] | None,
    min_price: float | None,
    max_price: float | None,
    season: str | None,
    size: str | None,
    quality: str | None,
    origin: str | None,
    ctx: Context,
) -> list[Product]:
    out = []
    ql = (q or "").lower().strip()
    for p in products:
        if ql and ql not in f"{p.title_en} {p.title_original} {p.brand or ''} {p.description_en or ''}".lower():
            continue
        if brand and p.brand not in brand:
            continue
        if seller_id and p.seller_id not in seller_id:
            continue
        if category and p.category not in category:
            continue
        if material and not {m["fibre"] for m in p.materials or []} & set(material):
            continue
        if min_price is not None and (p.price is None or p.price < min_price):
            continue
        if max_price is not None and (p.price is None or p.price > max_price):
            continue
        if season and p.seasons and season not in p.seasons:
            continue
        if size and size.upper() not in [s.upper() for s in p.sizes or []]:
            continue
        if origin and p.data_origin != origin:
            continue
        if quality:
            status = ctx.quality(p)["status"]
            if quality == "evidence" and status == "Unverified":
                continue
            if quality == "unverified" and status != "Unverified":
                continue
        out.append(p)
    return out


def _load(db: Session, status: str | None = "active"):
    q = db.query(Product).options(selectinload(Product.images), selectinload(Product.evidence))
    if status:
        q = q.filter(Product.status == status)
    return q


@router.get("/feed")
def feed(
    limit: int = Query(20, ge=1, le=100),
    q: str | None = None,
    brand: list[str] | None = Query(None),
    seller_id: list[int] | None = Query(None),
    category: list[str] | None = Query(None),
    material: list[str] | None = Query(None),
    min_price: float | None = None,
    max_price: float | None = None,
    season: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    include_seed: bool = False,
    db: Session = Depends(get_db),
):
    """Personalised discovery feed: undecided products ranked for you, with exploration slots."""
    prof = build_profile(db)
    rcfg = get_setting(db, "recommender")
    ctx = Context(db)
    ids = {p.id for p in feed_candidates(db, prof, include_skipped_after_days=rcfg.get("skip_resurface_days", 10))}
    candidates = [p for p in _load(db).filter(Product.id.in_(ids)).all()] if ids else []
    if not include_seed:
        candidates = [p for p in candidates if p.data_origin != "seed"]
    candidates = _apply_filters(
        candidates,
        q=q,
        brand=brand,
        seller_id=seller_id,
        category=category,
        material=material,
        min_price=min_price,
        max_price=max_price,
        season=season,
        size=size,
        quality=quality,
        origin=None,
        ctx=ctx,
    )
    quality_lookup = {p.id: ctx.quality(p) for p in candidates}
    ranked = rank_products(
        db, candidates, prof, quality_lookup=quality_lookup, wardrobe_lookup=complement_scores(db, candidates)
    )
    seed = int(dt.date.today().strftime("%Y%m%d"))
    chosen = build_feed(ranked, prof, limit, rcfg.get("exploration_rate", 0.18), seed=seed)
    return {
        "items": [product_card(ctx, r.product, r) for r in chosen],
        "total_candidates": len(candidates),
        "learning_strength": round(prof.strength, 3),
        "decisions": prof.n_informative,
    }


@router.get("/products")
def list_products(
    sort: str = Query("score", pattern="^(score|newest|price_asc|price_desc|updated)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=200),
    status: str | None = Query("active"),
    q: str | None = None,
    brand: list[str] | None = Query(None),
    seller_id: list[int] | None = Query(None),
    category: list[str] | None = Query(None),
    material: list[str] | None = Query(None),
    min_price: float | None = None,
    max_price: float | None = None,
    season: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    origin: str | None = None,
    new_only: bool = False,
    db: Session = Depends(get_db),
):
    ctx = Context(db)
    products = _load(db, status if status != "all" else None).all()
    if new_only:
        products = [p for p in products if p.is_new]
    products = _apply_filters(
        products,
        q=q,
        brand=brand,
        seller_id=seller_id,
        category=category,
        material=material,
        min_price=min_price,
        max_price=max_price,
        season=season,
        size=size,
        quality=quality,
        origin=origin,
        ctx=ctx,
    )
    total = len(products)
    start = (page - 1) * page_size
    if sort == "score":
        ranked = rank_products(
            db,
            products,
            quality_lookup={p.id: ctx.quality(p) for p in products},
            wardrobe_lookup=complement_scores(db, products),
        )
        items = [product_card(ctx, r.product, r) for r in ranked[start : start + page_size]]
    else:
        key = {
            "newest": lambda p: -(p.first_seen_at.timestamp() if p.first_seen_at else 0),
            "updated": lambda p: -(p.updated_at.timestamp() if p.updated_at else 0),
            "price_asc": lambda p: (p.price is None, p.price or 0),
            "price_desc": lambda p: (p.price is None, -(p.price or 0)),
        }[sort]
        products.sort(key=key)
        items = [product_card(ctx, p) for p in products[start : start + page_size]]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    products = db.query(Product).filter(Product.status == "active").all()
    c = lambda xs: [{"value": k, "count": v} for k, v in Counter(x for x in xs if x).most_common()]  # noqa: E731
    from ..models import Seller

    sellers = {s.id: s.name for s in db.query(Seller)}
    return {
        "brands": c(p.brand for p in products),
        "categories": c(p.category for p in products),
        "materials": c(m["fibre"] for p in products for m in p.materials or []),
        "sizes": c(s for p in products for s in p.sizes or []),
        "seasons": c(s for p in products for s in p.seasons or []),
        "sellers": [
            {"value": sid, "label": sellers.get(sid, "?"), "count": n}
            for sid, n in Counter(p.seller_id for p in products if p.seller_id).most_common()
        ],
        "price_range": [
            min((p.price for p in products if p.price), default=None),
            max((p.price for p in products if p.price), default=None),
        ],
        "total": len(products),
    }


def _get(db: Session, product_id: int) -> Product:
    p = db.get(Product, product_id)
    if p is None:
        raise HTTPException(404, "product not found")
    return p


@router.get("/products/{product_id}")
def get_product(product_id: int, db: Session = Depends(get_db)):
    p = _get(db, product_id)
    ctx = Context(db)
    ranked = rank_products(db, [p], wardrobe_lookup=complement_scores(db, [p]))[0]
    if p.is_new:
        p.is_new = False
        db.commit()
    return product_card(ctx, p, ranked, detail=True)


@router.patch("/products/{product_id}")
def patch_product(product_id: int, body: ProductPatch, db: Session = Depends(get_db)):
    p = _get(db, product_id)
    data = body.model_dump(exclude_unset=True)
    if "material_text" in data:
        mats = parse_materials(data.pop("material_text") or "")
        p.materials = mats
        p.material_text = materials_to_text(mats) or None
    for k, v in data.items():
        setattr(p, k, v)
    p.text_embedding = emb.to_bytes(emb.embed_text(emb.product_text(p)))
    db.commit()
    return get_product(product_id, db)


@router.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    db.delete(_get(db, product_id))
    db.commit()
    return {"ok": True}


@router.get("/products/{product_id}/compare")
def compare(product_id: int, db: Session = Depends(get_db)):
    return comparison(Context(db), _get(db, product_id))


@router.post("/products/{product_id}/match")
def decide_match(product_id: int, body: MatchDecision, db: Session = Depends(get_db)):
    _get(db, product_id)
    _get(db, body.other_id)
    rel = dedup.set_user_decision(db, product_id, body.other_id, body.same)
    return {"relation": rel.relation, "user_confirmed": rel.user_confirmed}


@router.post("/products/{product_id}/evidence")
def add_evidence(product_id: int, body: EvidenceIn, db: Session = Depends(get_db)):
    p = _get(db, product_id)
    ev = Evidence(product_id=p.id, created_by="user", **body.model_dump())
    db.add(ev)
    db.commit()
    return {"id": ev.id}


@router.delete("/evidence/{evidence_id}")
def delete_evidence(evidence_id: int, db: Session = Depends(get_db)):
    ev = db.get(Evidence, evidence_id)
    if ev is None:
        raise HTTPException(404, "evidence not found")
    if ev.created_by == "system":
        raise HTTPException(400, "seller-claim evidence is generated from the listing; edit the product instead")
    db.delete(ev)
    db.commit()
    return {"ok": True}


@router.post("/products/{product_id}/refresh")
def refresh_product(product_id: int, db: Session = Depends(get_db)):
    p = _get(db, product_id)
    url = p.source_url if p.source_url.startswith("http") else (p.links or {}).get("weidian")
    if not url:
        raise HTTPException(400, "this record has no web link to refresh")
    return {"job_id": jobs.enqueue("import_url", {"url": url, "seller_id": p.seller_id})}


@router.post("/products/{product_id}/ai-assess")
def ai_assess(product_id: int, db: Session = Depends(get_db)):
    from ..services import ai

    _get(db, product_id)
    if not ai.is_enabled("visual"):
        raise HTTPException(400, "AI visual assessment is disabled (enable it in Settings and set ANTHROPIC_API_KEY)")
    return {"job_id": jobs.enqueue("ai_assess", {"product_id": product_id})}
