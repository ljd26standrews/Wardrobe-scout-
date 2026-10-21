"""Discovery feed, product browsing, comparison and evidence endpoints."""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..ingestion.parsing import materials_to_text, parse_materials
from ..models import Evidence, Feedback, FeedRequest, Impression, Product
from ..schemas import EvidenceIn, MatchDecision, ProductPatch, ViewedIn
from ..services import calibration, dedup, jobs, selective
from ..services import embeddings as emb
from ..services.presenter import Context, comparison, product_card
from ..services.recommender import build_feed, build_profile, collapse_families, feed_candidates, rank_products
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


def feed_filters(
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
) -> dict:
    return {
        "q": q, "brand": brand, "seller_id": seller_id, "category": category, "material": material,
        "min_price": min_price, "max_price": max_price, "season": season, "size": size, "quality": quality,
        "include_seed": include_seed,
    }  # fmt: skip


NO_FILTERS = {k: None for k in ("q", "brand", "seller_id", "category", "material", "min_price", "max_price", "season",
                                "size", "quality")} | {"include_seed": False}  # fmt: skip


def ranked_candidates(db: Session, prof, ctx: Context, flt: dict) -> tuple[list, dict, int, dict]:
    """Undecided pieces matching the filters, ranked, one per garment: (ranked, offers, n candidates, quality)."""
    rcfg = get_setting(db, "recommender")
    ids = {p.id for p in feed_candidates(db, prof, include_skipped_after_days=rcfg.get("skip_resurface_days", 10))}
    candidates = [p for p in _load(db).filter(Product.id.in_(ids)).all()] if ids else []
    if not flt.get("include_seed"):
        candidates = [p for p in candidates if p.data_origin != "seed"]
    filters = {k: v for k, v in flt.items() if k != "include_seed"}
    candidates = _apply_filters(candidates, **filters, origin=None, ctx=ctx)
    quality_lookup = {p.id: ctx.quality(p) for p in candidates}
    ranked = rank_products(
        db, candidates, prof, quality_lookup=quality_lookup, wardrobe_lookup=complement_scores(db, candidates)
    )
    ranked, offers = collapse_families(ranked)  # one slot per garment, its other listings as offers
    return ranked, offers, len(candidates), quality_lookup


def feed_card(ctx: Context, r, offers: dict, confidence: dict | None = None) -> dict:
    card = product_card(ctx, r.product, r)
    others = offers.get(r.product.id, [])
    card["family"] = {
        "size": 1 + len(others),
        "offers": [
            {
                "product_id": o.id,
                "seller": o.seller.name if o.seller else None,
                "price": o.price,
                "currency": o.currency,
            }
            for o in others
        ],
    }
    if confidence is not None:
        card["confidence"] = confidence
    return card


@router.get("/feed")
def feed(
    limit: int = Query(20, ge=1, le=100),
    mode: str | None = Query(None, pattern="^(balanced|high|extreme)$"),
    flt: dict = Depends(feed_filters),
    db: Session = Depends(get_db),
):
    """Discover's main feed. With selective recommendation on (the default), only pieces that pass the chosen
    mode's gate, best first, and never padded with weaker ones; ``gate`` says what qualified and why the feed
    is what it is. With it off, the earlier ranked feed with exploration slots."""
    prof = build_profile(db)
    rcfg = get_setting(db, "recommender")
    ctx = Context(db)
    ranked, offers, n_candidates, quality_lookup = ranked_candidates(db, prof, ctx, flt)
    scfg = get_setting(db, "selective")
    warmup = selective.resolve_mode(scfg, mode) == "balanced" and not calibration.active(db)
    gate = selective.evaluate(db, ranked, prof, mode, quality_lookup=quality_lookup, learning=warmup)
    arm = selective.trial_arm(gate.cfg, dt.date.today())
    base = {"total_candidates": n_candidates, "learning_strength": round(prof.strength, 3),
            "decisions": prof.n_informative}  # fmt: skip
    if gate.state == "off" or arm == "ranked":
        seed = int(dt.date.today().strftime("%Y%m%d"))
        exploration = rcfg.get("exploration_rate", 0.18)
        chosen = build_feed(ranked, prof, limit, exploration, seed=seed)
        rows = [
            {"product_id": r.product.id, "position": i, "score": round(float(r.score), 4), "explore": bool(r.explore),
             "family_size": 1 + len(offers.get(r.product.id, []))}
            for i, r in enumerate(chosen)
        ]  # fmt: skip
        feed_id = _log_feed(db, rcfg, prof, n_candidates, flt, rows, exploration=exploration, trial=arm)
        summary = gate.summary(len(chosen), policy="ranked")
        if arm:
            summary.update(trial=arm, message="Trial day: Discover shows the earlier ranking today (with its "
                           "exploration slots), so the two can be compared. You can end the trial in Settings.")  # fmt: skip
        return {"feed_id": feed_id, "items": [feed_card(ctx, r, offers) for r in chosen], **base, "gate": summary}
    rate = rcfg.get("exploration_rate", 0.18)
    n_explore = selective.warmup_exploration(gate, limit, rate)
    chosen = selective.main_feed(gate, limit - n_explore)
    sampling = None
    if n_explore:
        picks, sampling = selective.learning_feed(db, gate, n_explore, salt=_learning_salt(db, gate.mode))
        for c in picks:
            _label_learning_pick(c)
        chosen = selective.interleave(chosen, picks, rate)
    summary = gate.summary(len(chosen))
    if gate.calibrated:  # how this mode's claim has held up so far, said where the claim is made
        obs = selective.performance(db)["passed_gate"][gate.mode]
        summary["observed"] = {
            **obs,
            "shortfall": obs["interval"] is not None and obs["interval"][1] < summary["target"],
        }
    if sampling:
        summary["sampling"] = sampling
    if arm:
        summary["trial"] = arm
    feed_id = _log_feed(db, rcfg, prof, n_candidates, flt, selective.log_items(gate, chosen, offers),
                        policy="selective", mode=gate.mode, gate=gate, trial=arm, sampling=sampling,
                        exploration=rate if n_explore else 0.0)  # fmt: skip
    items = [feed_card(ctx, c.ranked, offers, selective.card_confidence(gate, c)) for c in chosen]
    return {"feed_id": feed_id, "items": items, **base, "gate": summary}


@router.get("/feed/learning")
def learning_feed(
    limit: int = Query(24, ge=1, le=100),
    mode: str | None = Query(None, pattern="^(balanced|high|extreme)$"),
    flt: dict = Depends(feed_filters),
    db: Session = Depends(get_db),
):
    """Learning picks: plausible pieces the model is unsure about, kept apart from the main feed. Sampled with
    known inclusion probabilities (logged), so what you think of them can be evaluated without bias."""
    prof = build_profile(db)
    rcfg = get_setting(db, "recommender")
    ctx = Context(db)
    ranked, offers, n_candidates, quality_lookup = ranked_candidates(db, prof, ctx, flt)
    gate = selective.evaluate(db, ranked, prof, mode, quality_lookup=quality_lookup, learning=True)
    base = {"total_candidates": n_candidates, "learning_strength": round(prof.strength, 3),
            "decisions": prof.n_informative}  # fmt: skip
    if gate.state == "off":
        summary = gate.summary(0, policy="learning")
        summary["message"] = "Learning picks are part of selective recommendation, which is switched off in Settings."
        return {"feed_id": None, "items": [], **base, "gate": summary}
    picks, sampling = selective.learning_feed(db, gate, limit, salt=_learning_salt(db, gate.mode))
    for c in picks:
        _label_learning_pick(c)
    summary = gate.summary(len(picks), policy="learning")
    summary["sampling"] = sampling
    feed_id = _log_feed(db, rcfg, prof, n_candidates, flt, selective.log_items(gate, picks, offers),
                        policy="learning", mode=gate.mode, gate=gate, sampling=sampling)  # fmt: skip
    items = [feed_card(ctx, c.ranked, offers, selective.card_confidence(gate, c)) for c in picks]
    return {"feed_id": feed_id, "items": items, **base, "gate": summary}


def _learning_salt(db: Session, mode: str) -> str:
    """Fresh draws after every decision (Undo returns to the earlier draw): a refresh does not reshuffle the picks,
    and rating them brings new ones. Each request's inclusion probabilities stay exact and are logged."""
    decisions = db.query(func.count(Feedback.id)).scalar() or 0
    return f"{dt.date.today().isoformat()}:{mode}:{decisions}"


def _label_learning_pick(c) -> None:
    c.ranked.explore = True
    c.ranked.reasons.insert(0, "Learning pick: the model is unsure about pieces like this, so your reaction teaches "
                               "it the most")  # fmt: skip


MAX_FEED_LOG = 10_000  # feeds kept in the impression log (oldest pruned)
MAX_IMPRESSIONS = 200_000


def _log_feed(db, rcfg, prof, n_candidates, flt, rows, *, exploration=0.0, policy="ranked", mode=None, gate=None,
              sampling=None, trial=None) -> str:  # fmt: skip
    """Record what this feed offered and under which model and policy (see models.FeedRequest)."""
    from ..services import preference_model as pm

    engine = rcfg.get("engine", "bayes")
    details = None
    if gate is not None:
        t, g = selective.thresholds(gate.cfg, gate.mode)
        details = {"state": gate.state, "target": t, "confidence": g, "eligible": len(gate.candidates),
                   "qualified": gate.qualified() if gate.calibrated else None}  # fmt: skip
        if sampling is not None:
            details["sampling"] = sampling
    if trial:
        details = {**(details or {}), "trial": trial}
    fr = FeedRequest(
        id=uuid.uuid4().hex,
        engine=engine,
        engine_version=pm.ENGINE_VERSION if engine == "bayes" else "legacy",
        decisions=prof.n_informative,
        candidates=n_candidates,
        exploration_rate=exploration,
        filters={k: v for k, v in flt.items() if v not in (None, [], "", False)},
        items=rows,
        policy=policy,
        mode=mode,
        calibration_id=gate.calibration.id if gate is not None and gate.calibration is not None else None,
        gate=details,
    )
    db.add(fr)
    db.flush()
    _prune(db, FeedRequest, FeedRequest.created_at, MAX_FEED_LOG)
    db.commit()
    return fr.id


def _prune(db, model, order_col, keep: int) -> None:
    n = db.query(func.count()).select_from(model).scalar() or 0
    if n > keep + keep // 20:  # prune in batches, not on every request
        cutoff = db.query(order_col).order_by(order_col.desc()).offset(keep).limit(1).scalar()
        if cutoff is not None:
            db.query(model).filter(order_col <= cutoff).delete(synchronize_session=False)


@router.post("/feed/{feed_id}/viewed")
def feed_viewed(feed_id: str, body: ViewedIn, db: Session = Depends(get_db)):
    """Record which served products were actually displayed. Only products that feed served are accepted, at
    the position the server served them (each once per feed and surface)."""
    fr = db.get(FeedRequest, feed_id)
    if fr is None:
        raise HTTPException(404, "feed not found")
    served = {it["product_id"]: it for it in fr.items or []}
    already = {
        pid
        for (pid,) in db.query(Impression.product_id).filter(
            Impression.feed_id == feed_id, Impression.surface == body.surface
        )
    }
    logged = 0
    for it in body.items:
        s = served.get(it.product_id)
        if s is None or it.product_id in already:
            continue
        db.add(Impression(feed_id=feed_id, product_id=it.product_id, position=s["position"], surface=body.surface,
                          explore=bool(s.get("explore"))))  # fmt: skip
        already.add(it.product_id)
        logged += 1
    db.flush()
    _prune(db, Impression, Impression.id, MAX_IMPRESSIONS)
    db.commit()
    return {"logged": logged}


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
    prof = build_profile(db)
    quality_lookup = {p.id: ctx.quality(p)}
    ranked = rank_products(db, [p], prof, quality_lookup=quality_lookup, wardrobe_lookup=complement_scores(db, [p]))[0]
    if p.is_new:
        p.is_new = False
        db.commit()
    card = product_card(ctx, p, ranked, detail=True)
    gate = selective.evaluate(db, [ranked], prof, None, quality_lookup=quality_lookup)
    if gate.state != "off" and gate.candidates:  # the same gate as the feeds, so the two always agree
        card["confidence"] = selective.card_confidence(gate, gate.candidates[0])
    return card


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
