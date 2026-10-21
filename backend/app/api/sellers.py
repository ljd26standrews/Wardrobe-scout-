"""Sellers, watched links, imports (URL / manual / CSV / JSON / bookmarklet), jobs and research leads."""

from __future__ import annotations

import csv
import io
import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion.base import RawListing
from ..ingestion.generic import url_id
from ..ingestion.parsing import parse_price, weidian_item_id
from ..ingestion.pipeline import process_images, upsert_listing
from ..ingestion.scan import get_or_create_seller_for_url, import_html, slug_from_url
from ..models import CommunityLead, Evidence, Job, Product, ProductImage, ScanRun, Seller, WatchedUrl
from ..schemas import (
    BookmarkletIn,
    CommunitySearchIn,
    EvidenceIn,
    LeadAccept,
    ScanIn,
    SellerIn,
    SellerPatch,
    UrlImportIn,
)
from ..services import dedup, jobs
from ..services.images import ImageError, phash_of, store_image_bytes
from ..services.quality import evidence_view, seller_reputation_status
from ..services.research import accept_lead

router = APIRouter(prefix="/api", tags=["sellers"])


def _seller_view(db: Session, s: Seller) -> dict:
    counts = dict(
        db.query(Product.status, func.count()).filter(Product.seller_id == s.id).group_by(Product.status).all()
    )
    origins = dict(
        db.query(Product.data_origin, func.count())
        .filter(Product.seller_id == s.id)
        .group_by(Product.data_origin)
        .all()
    )
    last_run = db.query(ScanRun).filter(ScanRun.seller_id == s.id).order_by(ScanRun.started_at.desc()).first()
    images_ok = (
        db.query(func.count(ProductImage.id))
        .join(Product)
        .filter(Product.seller_id == s.id, ProductImage.local_path.isnot(None))
        .scalar()
    )
    new_7d = db.query(func.count(Product.id)).filter(Product.seller_id == s.id, Product.is_new.is_(True)).scalar()
    return {
        "id": s.id,
        "name": s.name,
        "slug": s.slug,
        "platform": s.platform,
        "base_url": s.base_url,
        "status": s.status,
        "priority": s.priority,
        "notes": s.notes,
        "scan_interval_hours": s.scan_interval_hours,
        "reputation_status": s.reputation_status,
        "evidence_count": db.query(func.count(Evidence.id)).filter(Evidence.seller_id == s.id).scalar(),
        "products": {
            "active": counts.get("active", 0),
            "unavailable": counts.get("unavailable", 0),
            "scraped": origins.get("scraped", 0),
            "manual": sum(origins.get(k, 0) for k in ("manual", "csv", "json", "bookmarklet")),
            "seed": origins.get("seed", 0),
            "new": new_7d,
            "images_downloaded": images_ok,
        },
        "last_scan_at": s.last_scan_at.isoformat() if s.last_scan_at else None,
        "last_scan_status": s.last_scan_status,
        "last_scan_message": s.last_scan_message,
        "last_run": {
            "pages_fetched": last_run.pages_fetched,
            "products_seen": last_run.products_seen,
            "new": last_run.products_new,
            "updated": last_run.products_updated,
            "errors": (last_run.errors or [])[:5],
            "status": last_run.status,
        }
        if last_run
        else None,
    }


@router.get("/sellers")
def list_sellers(db: Session = Depends(get_db)):
    return [_seller_view(db, s) for s in db.query(Seller).order_by(Seller.priority, Seller.name)]


@router.post("/sellers")
def create_seller(body: SellerIn, db: Session = Depends(get_db)):
    base = str(body.base_url).rstrip("/") if body.base_url else None
    slug = slug_from_url(base) if base else body.name.lower().replace(" ", "-")
    if db.query(Seller).filter_by(slug=slug).first():
        raise HTTPException(409, "a seller with this catalogue already exists")
    platform = body.platform
    if base and "yupoo.com" in base:
        platform = "yupoo"
    s = Seller(
        name=body.name,
        slug=slug,
        base_url=base,
        platform=platform,
        priority=body.priority,
        notes=body.notes,
        scan_interval_hours=body.scan_interval_hours,
    )
    db.add(s)
    db.commit()
    return _seller_view(db, s)


def _seller(db: Session, seller_id: int) -> Seller:
    s = db.get(Seller, seller_id)
    if s is None:
        raise HTTPException(404, "seller not found")
    return s


@router.patch("/sellers/{seller_id}")
def patch_seller(seller_id: int, body: SellerPatch, db: Session = Depends(get_db)):
    s = _seller(db, seller_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, str(v).rstrip("/") if k == "base_url" and v else v)
    db.commit()
    return _seller_view(db, s)


@router.delete("/sellers/{seller_id}")
def delete_seller(seller_id: int, delete_products: bool = False, db: Session = Depends(get_db)):
    s = _seller(db, seller_id)
    if delete_products:
        for p in db.query(Product).filter(Product.seller_id == s.id):
            db.delete(p)
    db.delete(s)
    db.commit()
    return {"ok": True}


@router.post("/sellers/{seller_id}/scan")
def scan(seller_id: int, body: ScanIn, db: Session = Depends(get_db)):
    s = _seller(db, seller_id)
    if not s.base_url:
        raise HTTPException(400, "seller has no catalogue URL")
    return {"job_id": jobs.enqueue("scan_seller", {"seller_id": s.id, "mode": body.mode})}


@router.post("/sellers/scan-all")
def scan_all(body: ScanIn):
    return {"job_id": jobs.enqueue("scan_all", {"mode": body.mode})}


@router.get("/sellers/{seller_id}/runs")
def seller_runs(seller_id: int, db: Session = Depends(get_db)):
    runs = db.query(ScanRun).filter(ScanRun.seller_id == seller_id).order_by(ScanRun.started_at.desc()).limit(20)
    return [
        {
            "id": r.id,
            "status": r.status,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "pages_fetched": r.pages_fetched,
            "products_seen": r.products_seen,
            "new": r.products_new,
            "updated": r.products_updated,
            "errors": r.errors,
            "message": r.message,
        }
        for r in runs
    ]


@router.get("/sellers/{seller_id}/evidence")
def seller_evidence(seller_id: int, db: Session = Depends(get_db)):
    return [evidence_view(e) for e in db.query(Evidence).filter(Evidence.seller_id == seller_id)]


@router.post("/sellers/{seller_id}/evidence")
def add_seller_evidence(seller_id: int, body: EvidenceIn, db: Session = Depends(get_db)):
    s = _seller(db, seller_id)
    db.add(Evidence(seller_id=s.id, created_by="user", **body.model_dump()))
    db.commit()
    s.reputation_status = seller_reputation_status(db, s)
    db.commit()
    return _seller_view(db, s)


# ------------------------------------------------------------------ watched URLs
@router.get("/watched")
def watched(db: Session = Depends(get_db)):
    return [
        {
            "id": w.id,
            "url": w.url,
            "seller_id": w.seller_id,
            "active": w.active,
            "product_id": w.product_id,
            "last_checked_at": w.last_checked_at.isoformat() if w.last_checked_at else None,
            "last_status": w.last_status,
            "last_message": w.last_message,
        }
        for w in db.query(WatchedUrl)
    ]


@router.delete("/watched/{watched_id}")
def delete_watched(watched_id: int, db: Session = Depends(get_db)):
    w = db.get(WatchedUrl, watched_id)
    if w:
        db.delete(w)
        db.commit()
    return {"ok": True}


@router.post("/watched/check")
def check_watched():
    return {"job_id": jobs.enqueue("check_watched", {})}


# ---------------------------------------------------------------------- imports
@router.post("/import/url")
def import_url(body: UrlImportIn, db: Session = Depends(get_db)):
    url = str(body.url)
    if body.watch and not db.query(WatchedUrl).filter_by(url=url).first():
        db.add(WatchedUrl(url=url, seller_id=body.seller_id))
        db.commit()
    return {"job_id": jobs.enqueue("import_url", {"url": url, "seller_id": body.seller_id})}


def _manual_seller(db: Session, seller_id: int | None, seller_name: str | None, url: str | None) -> Seller | None:
    if seller_id:
        return db.get(Seller, seller_id)
    if seller_name:
        slug = seller_name.strip().lower().replace(" ", "-")
        s = (
            db.query(Seller)
            .filter((Seller.slug == slug) | (func.lower(Seller.name) == seller_name.strip().lower()))
            .first()
        )
        if s is None:
            s = Seller(name=seller_name.strip(), slug=slug, platform="manual", status="paused")
            db.add(s)
            db.flush()
        return s
    if url and url.startswith("http"):
        return get_or_create_seller_for_url(db, url)
    return None


def _manual_raw(d: dict) -> RawListing:
    url = (d.get("url") or "").strip()
    title = (d.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    ext = weidian_item_id(url) if "weidian.com" in url else None
    ext = ext or (url_id(url) if url else f"manual-{uuid.uuid4().hex[:12]}")
    desc_parts = [d.get("description") or ""]
    if d.get("material_text"):
        desc_parts.append(f"Material: {d['material_text']}")
    if d.get("brand"):
        desc_parts.append(f"Brand: {d['brand']}")
    if d.get("category"):
        desc_parts.append(f"Category: {d['category']}")
    if d.get("colours"):
        desc_parts.append(f"Colour: {d['colours']}")
    price = d.get("price")
    try:
        price = float(price) if price not in (None, "") else parse_price(title)
    except (TypeError, ValueError):
        price = parse_price(str(price))
    images = d.get("image_urls") or []
    if isinstance(images, str):
        images = [u for u in images.replace("|", "\n").split() if u.startswith("http")]
    sizes = d.get("sizes") or []
    if isinstance(sizes, str):
        sizes = [x.strip() for x in sizes.replace("/", ",").split(",") if x.strip()]
    links = {}
    if "weidian.com" in url:
        links["weidian"] = url
    elif "yupoo.com" in url:
        links["yupoo"] = url
    elif url:
        links["seller"] = url
    return RawListing(
        external_id=ext,
        source_url=url or f"manual://{ext}",
        title=title,
        description="\n".join(x for x in desc_parts if x) or None,
        image_urls=images,
        image_referer=url or None,
        price=price,
        currency=(d.get("currency") or "CNY").upper(),
        sizes=sizes,
        links=links,
        category_hint=d.get("category"),
    )


def _post_import(db: Session, p: Product, d: dict) -> None:
    if d.get("brand"):
        p.brand = d["brand"]
    if d.get("category"):
        p.category = d["category"].lower()


@router.post("/import/manual")
async def import_manual(
    title: str = Form(...),
    url: str | None = Form(None),
    seller_id: int | None = Form(None),
    seller_name: str | None = Form(None),
    price: str | None = Form(None),
    currency: str = Form("CNY"),
    description: str | None = Form(None),
    material_text: str | None = Form(None),
    brand: str | None = Form(None),
    category: str | None = Form(None),
    colours: str | None = Form(None),
    sizes: str | None = Form(None),
    image_urls: str | None = Form(None),
    watch: bool = Form(False),
    images: list[UploadFile] | None = File(None),
    db: Session = Depends(get_db),
):
    d = dict(
        title=title,
        url=url,
        price=price,
        currency=currency,
        description=description,
        material_text=material_text,
        brand=brand,
        category=category,
        colours=colours,
        sizes=sizes,
        image_urls=image_urls,
    )
    try:
        raw = _manual_raw(d)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    seller = _manual_seller(db, seller_id, seller_name, url)
    res = upsert_listing(db, seller, raw, origin="manual")
    p = res.product
    _post_import(db, p, d)
    for f in images or []:
        data = await f.read()
        if not data:
            continue
        try:
            rel, pil = store_image_bytes(data, "uploads")
        except ImageError as e:
            raise HTTPException(422, f"{f.filename}: {e}") from e
        p.images.append(
            ProductImage(
                url=f"upload:{rel}",
                local_path=rel,
                fetch_status="uploaded",
                position=len(p.images),
                phash=phash_of(pil),
            )
        )
    db.flush()
    process_images(db, p, None)
    dedup.match_product(db, p)
    if url and watch and url.startswith("http") and not db.query(WatchedUrl).filter_by(url=url).first():
        db.add(WatchedUrl(url=url, seller_id=seller.id if seller else None, product_id=p.id))
    db.commit()
    job_id = jobs.enqueue("fetch_images", {"product_ids": [p.id]}) if raw.image_urls else None
    return {"ok": True, "product_id": p.id, "created": res.created, "image_job_id": job_id}


def _bulk(db: Session, rows: list[dict], origin: str) -> dict:
    results, ids = [], []
    for i, row in enumerate(rows):
        try:
            raw = _manual_raw(row)
            seller = _manual_seller(db, row.get("seller_id"), row.get("seller"), row.get("url"))
            res = upsert_listing(db, seller, raw, origin=origin)
            _post_import(db, res.product, row)
            db.commit()
            ids.append(res.product.id)
            results.append({"row": i + 1, "ok": True, "product_id": res.product.id, "created": res.created})
        except (ValueError, KeyError) as e:
            db.rollback()
            results.append({"row": i + 1, "ok": False, "error": str(e)})
    job_id = jobs.enqueue("fetch_images", {"product_ids": ids}) if ids else None
    return {
        "imported": sum(r["ok"] for r in results),
        "failed": sum(not r["ok"] for r in results),
        "results": results,
        "image_job_id": job_id,
    }


@router.post("/import/csv")
async def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    text = (await file.read()).decode("utf-8-sig")
    rows = [
        {k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(io.StringIO(text))
    ]
    return _bulk(db, rows, "csv")


@router.post("/import/json")
async def import_json(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        data = json.loads(await file.read())
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"invalid JSON: {e}") from e
    rows = data.get("products", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise HTTPException(422, 'expected a list of products or {"products": [...]}')
    return _bulk(db, rows, "json")


@router.get("/import/template.csv", response_class=PlainTextResponse)
def csv_template():
    return (
        "seller,title,url,price,currency,description,material_text,brand,category,colours,sizes,image_urls\n"
        "ExampleSeller,Example wool trousers,https://example.com/item/1,399,CNY,,80% wool 20% cashmere,,trousers,"
        "charcoal,S M L,https://example.com/a.jpg|https://example.com/b.jpg\n"
    )


@router.post("/import/bookmarklet")
def bookmarklet_import(body: BookmarkletIn, db: Session = Depends(get_db)):
    """Receives the HTML of a page *you* opened in your own browser."""
    return import_html(db, str(body.url), body.html, seller_id=body.seller_id)


# ------------------------------------------------------------------------ jobs
def _job_view(j: Job) -> dict:
    return {
        "id": j.id,
        "kind": j.kind,
        "status": j.status,
        "params": j.params,
        "progress": j.progress,
        "message": j.message,
        "result": j.result,
        "log": (j.log or [])[-30:],
        "created_at": j.created_at.isoformat(),
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


@router.get("/jobs")
def list_jobs(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)):
    return [_job_view(j) for j in db.query(Job).order_by(Job.created_at.desc()).limit(limit)]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    return _job_view(j)


# ------------------------------------------------------------------ research
@router.post("/research/community-search")
def community_search(body: CommunitySearchIn):
    return {"job_id": jobs.enqueue("community_search", body.model_dump())}


@router.get("/research/leads")
def leads(
    status: str = "new", seller_id: int | None = None, product_id: int | None = None, db: Session = Depends(get_db)
):
    q = db.query(CommunityLead).filter(CommunityLead.status == status)
    if seller_id:
        q = q.filter(CommunityLead.seller_id == seller_id)
    if product_id:
        q = q.filter(CommunityLead.product_id == product_id)
    return [
        {
            "id": lead.id,
            "source": lead.source,
            "url": lead.url,
            "title": lead.title,
            "excerpt": lead.excerpt,
            "author": lead.author,
            "posted_at": lead.posted_at.isoformat() if lead.posted_at else None,
            "possible_affiliate": lead.possible_affiliate,
            "seller_id": lead.seller_id,
            "product_id": lead.product_id,
            "fetched_at": lead.fetched_at.isoformat(),
        }
        for lead in q.order_by(CommunityLead.posted_at.desc()).limit(200)
    ]


@router.post("/research/leads/{lead_id}/accept")
def accept(lead_id: int, body: LeadAccept, db: Session = Depends(get_db)):
    lead = db.get(CommunityLead, lead_id)
    if lead is None:
        raise HTTPException(404, "lead not found")
    ev = accept_lead(
        db,
        lead,
        aspect=body.aspect,
        polarity=body.polarity,
        claim=body.claim,
        product_id=body.product_id,
        seller_id=body.seller_id,
        is_affiliate=body.is_affiliate,
        strength=body.strength,
    )
    if ev.seller_id:
        s = db.get(Seller, ev.seller_id)
        s.reputation_status = seller_reputation_status(db, s)
        db.commit()
    return evidence_view(ev)


@router.post("/research/leads/{lead_id}/dismiss")
def dismiss(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(CommunityLead, lead_id)
    if lead:
        lead.status = "dismissed"
        db.commit()
    return {"ok": True}
