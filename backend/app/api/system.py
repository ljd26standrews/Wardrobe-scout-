"""Settings, insights, images/files, backup and maintenance endpoints."""

from __future__ import annotations

import json
import shutil

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..ingestion.http import PoliteClient
from ..ingestion.pipeline import apply_analysis, fetch_and_analyse
from ..models import Feedback, Job, Product, ProductImage, SavedItem, Seller, WardrobeItem, utcnow
from ..services import ai, jobs
from ..services import embeddings as emb
from ..services.backup import export_all, import_all
from ..services.images import ImageError
from ..services.presenter import Context, product_card
from ..services.recommender import build_profile, describe_profile, rank_products
from ..services.settings_store import DEFAULTS, all_settings, get_setting, set_setting

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/bookmarklet-config")
def bookmarklet_config():
    """Token for the bookmarklet. Only reachable by an already-authorised client (the app's own proxy)."""
    from ..config import get_api_token

    return {"token": get_api_token()}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    counts = dict(db.query(Product.data_origin, func.count()).group_by(Product.data_origin).all())
    return {
        "products": {
            "total": sum(counts.values()),
            "by_origin": counts,
            "active": db.query(func.count(Product.id)).filter(Product.status == "active").scalar(),
            "new": db.query(func.count(Product.id)).filter(Product.is_new.is_(True)).scalar(),
        },
        "images": {
            "downloaded": db.query(func.count(ProductImage.id)).filter(ProductImage.local_path.isnot(None)).scalar(),
            "failed": db.query(func.count(ProductImage.id)).filter(ProductImage.fetch_status == "failed").scalar(),
        },
        "sellers": db.query(func.count(Seller.id)).scalar(),
        "feedback": db.query(func.count(Feedback.id)).scalar(),
        "saved": db.query(func.count(SavedItem.id)).scalar(),
        "wardrobe": db.query(func.count(WardrobeItem.id)).scalar(),
        "running_jobs": db.query(func.count(Job.id)).filter(Job.status.in_(["queued", "running"])).scalar(),
        "embedding": emb.backend_info(),
        "ai": ai.status(),
        "playwright_enabled": get_settings().enable_playwright,
    }


# ----------------------------------------------------------------- settings
@router.get("/settings")
def get_settings_all(db: Session = Depends(get_db)):
    return all_settings(db)


@router.put("/settings/{key}")
def put_setting(key: str, value: dict | list = Body(...), db: Session = Depends(get_db)):
    if key not in DEFAULTS:
        raise HTTPException(404, f"unknown setting {key}")
    if isinstance(DEFAULTS[key], dict) and not isinstance(value, dict):
        raise HTTPException(422, "expected an object")
    if key == "ranking_weights":
        for k, v in value.items():
            if not isinstance(v, int | float) or not 0 <= v <= 5:
                raise HTTPException(422, f"weight {k} must be between 0 and 5")
    if key == "recommender" and not 0 <= float(value.get("exploration_rate", 0.18)) <= 0.5:
        raise HTTPException(422, "exploration_rate must be between 0 and 0.5")
    return set_setting(db, key, value)


@router.post("/settings/{key}/reset")
def reset_setting(key: str, db: Session = Depends(get_db)):
    if key not in DEFAULTS:
        raise HTTPException(404, f"unknown setting {key}")
    return set_setting(db, key, DEFAULTS[key])


@router.post("/settings/currency/refresh")
def refresh_rate(db: Session = Depends(get_db)):
    """Fetch the CNY→GBP reference rate from the ECB via frankfurter.app (free, no key)."""
    try:
        r = httpx.get("https://api.frankfurter.app/latest?from=CNY&to=GBP", timeout=10)
        r.raise_for_status()
        rate = float(r.json()["rates"]["GBP"])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise HTTPException(502, f"could not fetch exchange rate: {e}") from e
    cur = get_setting(db, "currency")
    cur.update(
        {
            "cny_to_gbp": rate,
            "rate_source": "ECB reference rate via frankfurter.app",
            "rate_updated_at": utcnow().isoformat(),
        }
    )
    return set_setting(db, "currency", cur)


# ------------------------------------------------------------------ insights
@router.get("/insights")
def insights(db: Session = Depends(get_db)):
    prof = build_profile(db)
    out = describe_profile(db, prof)
    weights = get_setting(db, "ranking_weights")
    rcfg = get_setting(db, "recommender")
    engine = rcfg.get("engine", "bayes")
    out["engine"] = engine
    if engine == "bayes":
        from ..services import preference_model as pm

        post = pm.fit(db, pm.build_feature_space(db, get_setting(db, "taste_profile"), emb.get_image_backend().name))
        out["model"] = post.summary()
        k = rcfg.get("cold_start_k", 15)
        out["learning_strength"] = round(post.n_obs / (post.n_obs + k), 3) if post.n_obs else 0.0
        used = ("taste", "quality", "value", "wardrobe", "seller")
        out["effective_weights"] = {key: round(weights.get(key, 0), 3) for key in used}
    else:
        s = prof.strength
        out["effective_weights"] = {
            k: round(w * g, 3)
            for k, w in weights.items()
            if k != "taste"
            for g in [
                {
                    "rules": 1 - 0.6 * s,
                    "learned_attributes": s,
                    "visual": s if prof.pos_visual else 0,
                    "semantic": s if prof.pos_text else 0,
                }.get(k, 1.0)
            ]
        }
    out["weights"] = weights
    out["embedding"] = emb.backend_info()
    # Top recommendations with explanations (sample of the current catalogue)
    products = db.query(Product).filter(Product.status == "active", Product.data_origin != "seed").limit(2000).all()
    ranked = rank_products(db, products, prof)[:6]
    ctx = Context(db)
    out["top_explained"] = [
        {
            "id": r.product.id,
            "title": r.product.title_en,
            "score": round(r.score, 3),
            "contributions": {k: round(v, 4) for k, v in r.contributions.items()},
            "reasons": r.reasons,
            "cautions": r.cautions,
            "image": (product_card(ctx, r.product)["images"] or [{}])[0].get("src"),
        }
        for r in ranked
    ]
    return out


# ------------------------------------------------------------- images / files
@router.get("/images/{image_id}")
def image(image_id: int, db: Session = Depends(get_db)):
    img = db.get(ProductImage, image_id)
    if img is None:
        raise HTTPException(404, "image not found")
    if img.local_path is None and img.fetch_status != "uploaded" and get_settings().download_images:
        # On-demand fetch of a photo the scan did not need (display size), analysed on arrival so a size
        # chart moves to the sizing section.
        try:
            with PoliteClient(delay=0.5) as client:
                a = fetch_and_analyse(client, img.thumb_url or img.url, img.referer, fallback=img.url)
            apply_analysis(img, a)
            img.selected = img.selected or False
            db.commit()
        except ImageError as e:
            img.fetch_status, img.fetch_error = "failed", str(e)[:300]
            db.commit()
            raise HTTPException(404, "image unavailable") from e
    if img.local_path is None:
        raise HTTPException(404, "image unavailable")
    path = get_settings().data_dir / img.local_path
    if not path.exists():
        raise HTTPException(404, "image file missing")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


@router.get("/files/{rel_path:path}")
def files(rel_path: str):
    base = get_settings().data_dir.resolve()
    path = (base / rel_path).resolve()
    if (
        not path.is_relative_to(base)
        or not path.is_file()
        or path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")
    ):
        raise HTTPException(404, "file not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


# --------------------------------------------------------------------- backup
@router.get("/backup/export")
def export_backup(include_images: bool = True, db: Session = Depends(get_db)):
    data = export_all(db, include_images=include_images)
    fname = f"wardrobe-scout-backup-{utcnow().strftime('%Y%m%d-%H%M')}.json"
    return Response(
        json.dumps(data, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/backup/import")
async def import_backup(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        data = json.loads(await file.read())
        return import_all(db, data)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(422, str(e)) from e


@router.get("/backup/database")
def download_database():
    s = get_settings()
    if not s.database_url.startswith("sqlite:///"):
        raise HTTPException(400, "database download is only available for SQLite")
    src = s.database_url.removeprefix("sqlite:///")
    dst = s.backups_dir / f"wardrobe_scout-{utcnow().strftime('%Y%m%d-%H%M%S')}.db"
    import sqlite3

    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)  # consistent snapshot even while the app is running
    return FileResponse(dst, filename=dst.name, media_type="application/octet-stream")


# ---------------------------------------------------------------- maintenance
@router.post("/maintenance/recompute")
def recompute():
    emb.reset_backend()
    return {"job_id": jobs.enqueue("recompute", {})}


@router.post("/maintenance/dedup")
def run_dedup():
    return {"job_id": jobs.enqueue("dedup", {})}


@router.post("/research/summarise")
def summarise(body: dict = Body(...), db: Session = Depends(get_db)):
    from ..models import CommunityLead, Evidence

    seller_id, product_id = body.get("seller_id"), body.get("product_id")
    if not ai.is_enabled("research"):
        raise HTTPException(400, "AI research summaries are disabled")
    q = db.query(CommunityLead).filter(CommunityLead.status != "dismissed")
    q = (
        q.filter(CommunityLead.seller_id == seller_id)
        if seller_id
        else q.filter(CommunityLead.product_id == product_id)
    )
    leads = [
        {
            "title": x.title,
            "url": x.url,
            "excerpt": (x.excerpt or "") + (" [possible affiliate]" if x.possible_affiliate else ""),
        }
        for x in q.limit(30)
    ]
    ev = (
        db.query(Evidence)
        .filter((Evidence.seller_id == seller_id) if seller_id else (Evidence.product_id == product_id))
        .all()
    )
    leads += [{"title": f"{e.kind}: {e.aspect}", "url": e.source_url or "", "excerpt": e.claim} for e in ev]
    if not leads:
        raise HTTPException(400, "nothing to summarise yet")
    subject = (
        db.get(Seller, seller_id).name if seller_id else (db.get(Product, product_id).title_en if product_id else "")
    )
    try:
        return {
            "summary": ai.summarise_research(subject, leads),
            "sources": len(leads),
            "label": "AI summary of collected sources — verify before relying on it",
        }
    except ai.AIUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/maintenance/clear-images-cache")
def clear_cache(db: Session = Depends(get_db)):
    """Delete downloaded catalogue images (uploads are kept). They re-download on demand."""
    s = get_settings()
    shutil.rmtree(s.images_dir, ignore_errors=True)
    s.images_dir.mkdir(parents=True, exist_ok=True)
    db.query(ProductImage).filter(ProductImage.fetch_status == "ok").update(
        {ProductImage.local_path: None, ProductImage.fetch_status: "pending"}
    )
    db.commit()
    return {"ok": True}
