"""Export / import of everything personal: settings, sellers, feedback, saved
items, wardrobe, inspiration, user-entered evidence and the products they
reference (as snapshots)."""

from __future__ import annotations

import base64
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (
    AppSetting,
    Evidence,
    Feedback,
    InspirationImage,
    Product,
    SavedItem,
    Seller,
    WardrobeItem,
    WatchedUrl,
    utcnow,
)
from .images import resolve_path

EXPORT_VERSION = 1


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _file_b64(rel: str | None) -> str | None:
    if not rel:
        return None
    path = resolve_path(rel)
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def export_all(db: Session, include_images: bool = True) -> dict:
    sellers = db.query(Seller).all()
    seller_slug = {s.id: s.slug for s in sellers}
    product_ids = {f.product_id for f in db.query(Feedback) if f.product_id} | {
        s.product_id for s in db.query(SavedItem)
    }
    products = db.query(Product).filter(Product.id.in_(product_ids)).all() if product_ids else []
    pkey = {p.id: {"seller": seller_slug.get(p.seller_id), "external_id": p.external_id} for p in products}
    return {
        "format": "wardrobe-scout-backup",
        "version": EXPORT_VERSION,
        "exported_at": utcnow().isoformat(),
        "settings": {r.key: r.value for r in db.query(AppSetting)},
        "sellers": [
            {
                "name": s.name,
                "slug": s.slug,
                "platform": s.platform,
                "base_url": s.base_url,
                "status": s.status,
                "priority": s.priority,
                "notes": s.notes,
                "scan_interval_hours": s.scan_interval_hours,
            }
            for s in sellers
        ],
        "watched_urls": [
            {"url": w.url, "seller": seller_slug.get(w.seller_id), "active": w.active} for w in db.query(WatchedUrl)
        ],
        "products": [
            {
                "seller": seller_slug.get(p.seller_id),
                "external_id": p.external_id,
                "title_original": p.title_original,
                "title_en": p.title_en,
                "brand": p.brand,
                "category": p.category,
                "colours": p.colours,
                "materials": p.materials,
                "price": p.price,
                "currency": p.currency,
                "source_url": p.source_url,
                "links": p.links,
                "data_origin": p.data_origin,
                "completeness": p.completeness,
                "seasons": p.seasons,
                "image_urls": [i.url for i in p.images if not i.url.startswith("upload:")],
            }
            for p in products
        ],
        "feedback": [
            {
                "product": pkey.get(f.product_id),
                "action": f.action,
                "reasons": f.reasons,
                "note": f.note,
                "context": f.context,
                "snapshot": f.snapshot,
                "created_at": _iso(f.created_at),
            }
            for f in db.query(Feedback).order_by(Feedback.created_at)
        ],
        "saved": [
            {
                "product": pkey.get(s.product_id),
                "list_name": s.list_name,
                "priority": s.priority,
                "note": s.note,
                "target_size": s.target_size,
            }
            for s in db.query(SavedItem)
        ],
        "evidence": [
            {
                "product": pkey.get(e.product_id) if e.product_id else None,
                "seller": seller_slug.get(e.seller_id),
                "kind": e.kind,
                "aspect": e.aspect,
                "claim": e.claim,
                "polarity": e.polarity,
                "strength": e.strength,
                "source_url": e.source_url,
                "source_title": e.source_title,
                "is_affiliate": e.is_affiliate,
                "captured_at": _iso(e.captured_at),
            }
            for e in db.query(Evidence).filter(Evidence.created_by == "user")
        ],
        "wardrobe": [
            {
                "name": w.name,
                "category": w.category,
                "brand": w.brand,
                "colours": w.colours,
                "materials": w.materials,
                "seasons": w.seasons,
                "source_url": w.source_url,
                "notes": w.notes,
                "rating": w.rating,
                "image_b64": _file_b64(w.image_path) if include_images else None,
            }
            for w in db.query(WardrobeItem)
        ],
        "inspiration": [
            {
                "note": i.note,
                "polarity": i.polarity,
                "tags": i.tags,
                "image_b64": _file_b64(i.path) if include_images else None,
            }
            for i in db.query(InspirationImage)
        ],
    }


def import_all(db: Session, data: dict) -> dict:
    """Merge a backup into the current database (idempotent for sellers/products/settings)."""
    from .images import store_image_bytes

    if data.get("format") != "wardrobe-scout-backup":
        raise ValueError("not a Wardrobe Scout backup file")
    counts = {
        k: 0
        for k in (
            "settings",
            "sellers",
            "products",
            "feedback",
            "saved",
            "evidence",
            "wardrobe",
            "inspiration",
            "watched_urls",
        )
    }
    for k, v in (data.get("settings") or {}).items():
        row = db.get(AppSetting, k)
        if row:
            row.value = v
        else:
            db.add(AppSetting(key=k, value=v))
        counts["settings"] += 1
    sellers = {s.slug: s for s in db.query(Seller)}
    for s in data.get("sellers", []):
        if s["slug"] not in sellers:
            obj = Seller(
                **{
                    k: s.get(k)
                    for k in (
                        "name",
                        "slug",
                        "platform",
                        "base_url",
                        "status",
                        "priority",
                        "notes",
                        "scan_interval_hours",
                    )
                    if s.get(k) is not None
                }
            )
            db.add(obj)
            sellers[s["slug"]] = obj
            counts["sellers"] += 1
    db.flush()
    for w in data.get("watched_urls", []):
        if not db.query(WatchedUrl).filter_by(url=w["url"]).first():
            db.add(
                WatchedUrl(
                    url=w["url"],
                    seller_id=sellers[w["seller"]].id if w.get("seller") in sellers else None,
                    active=w.get("active", True),
                )
            )
            counts["watched_urls"] += 1

    def find_product(key: dict | None) -> Product | None:
        if not key:
            return None
        sid = sellers[key["seller"]].id if key.get("seller") in sellers else None
        return db.query(Product).filter_by(seller_id=sid, external_id=key["external_id"]).one_or_none()

    for p in data.get("products", []):
        key = {"seller": p.get("seller"), "external_id": p["external_id"]}
        if find_product(key) is None:
            from ..models import ProductImage

            obj = Product(
                seller_id=sellers[p["seller"]].id if p.get("seller") in sellers else None,
                external_id=p["external_id"],
                title_original=p.get("title_original") or "",
                title_en=p.get("title_en") or "",
                brand=p.get("brand"),
                category=p.get("category"),
                colours=p.get("colours") or [],
                materials=p.get("materials") or [],
                price=p.get("price"),
                currency=p.get("currency") or "CNY",
                source_url=p["source_url"],
                links=p.get("links") or {},
                data_origin=p.get("data_origin") or "manual",
                completeness=p.get("completeness") or "partial",
                seasons=p.get("seasons") or [],
                is_new=False,
            )
            obj.images = [ProductImage(url=u, position=i) for i, u in enumerate(p.get("image_urls") or [])]
            db.add(obj)
            counts["products"] += 1
    db.flush()
    existing_fb = {(f.product_id, f.action, _iso(f.created_at)) for f in db.query(Feedback)}
    for f in data.get("feedback", []):
        p = find_product(f.get("product"))
        created = datetime.fromisoformat(f["created_at"]) if f.get("created_at") else utcnow()
        if (p.id if p else None, f["action"], _iso(created)) in existing_fb:
            continue
        db.add(
            Feedback(
                product_id=p.id if p else None,
                action=f["action"],
                reasons=f.get("reasons") or [],
                note=f.get("note"),
                context=f.get("context") or {},
                snapshot=f.get("snapshot") or {},
                created_at=created,
            )
        )
        counts["feedback"] += 1
    for s in data.get("saved", []):
        p = find_product(s.get("product"))
        if p and not db.query(SavedItem).filter_by(product_id=p.id).first():
            db.add(
                SavedItem(
                    product_id=p.id,
                    list_name=s["list_name"],
                    priority=s.get("priority", 2),
                    note=s.get("note"),
                    target_size=s.get("target_size"),
                )
            )
            counts["saved"] += 1
    for e in data.get("evidence", []):
        p = find_product(e.get("product"))
        db.add(
            Evidence(
                product_id=p.id if p else None,
                seller_id=sellers[e["seller"]].id if e.get("seller") in sellers and not p else None,
                kind=e["kind"],
                aspect=e["aspect"],
                claim=e["claim"],
                polarity=e.get("polarity", 0),
                strength=e.get("strength", 0.7),
                source_url=e.get("source_url"),
                source_title=e.get("source_title"),
                is_affiliate=e.get("is_affiliate", False),
                captured_at=datetime.fromisoformat(e["captured_at"]) if e.get("captured_at") else utcnow(),
                created_by="user",
            )
        )
        counts["evidence"] += 1
    for w in data.get("wardrobe", []):
        path = store_image_bytes(base64.b64decode(w["image_b64"]), "uploads")[0] if w.get("image_b64") else None
        db.add(
            WardrobeItem(
                name=w["name"],
                category=w.get("category"),
                brand=w.get("brand"),
                colours=w.get("colours") or [],
                materials=w.get("materials") or [],
                seasons=w.get("seasons") or [],
                source_url=w.get("source_url"),
                notes=w.get("notes"),
                rating=w.get("rating"),
                image_path=path,
            )
        )
        counts["wardrobe"] += 1
    for i in data.get("inspiration", []):
        if i.get("image_b64"):
            path = store_image_bytes(base64.b64decode(i["image_b64"]), "uploads")[0]
            db.add(
                InspirationImage(path=path, note=i.get("note"), polarity=i.get("polarity", 1), tags=i.get("tags") or [])
            )
            counts["inspiration"] += 1
    db.commit()
    return counts
