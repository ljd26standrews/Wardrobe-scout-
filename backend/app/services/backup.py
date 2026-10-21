"""Backups.

Two complementary formats:

* **JSON export** (``export_all`` / ``import_all``): everything personal - settings, sellers, watched links,
  feedback (with its product snapshot), saved items, your evidence, your evaluation judgements, wardrobe,
  inspiration, your
  "same / not the same" match decisions, and every product any of these refer to. Photos you uploaded
  (wardrobe, inspiration, manually added products) are embedded; scraped product photos are referenced by
  URL. Import is a *merge* and is **idempotent**: importing the same file twice changes nothing the second
  time. Stable identities: products by (seller slug, external id); user evidence and wardrobe items by a
  content fingerprint; inspiration photos by image content. Version 1 files still import.
* **Full snapshot** (``write_full_snapshot`` / ``restore_full_snapshot``, CLI ``backup-full`` /
  ``restore-full``): a zip with a consistent copy of the SQLite database plus the images and uploads
  folders. Use it to move or restore an installation exactly, including every downloaded photo.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import (
    AppSetting,
    Evidence,
    Feedback,
    InspirationImage,
    Judgement,
    Product,
    ProductImage,
    ProductRelation,
    SavedItem,
    Seller,
    WardrobeItem,
    WatchedUrl,
    utcnow,
)
from .images import resolve_path

EXPORT_VERSION = 2
FORMAT = "wardrobe-scout-backup"
USER_ORIGINS = ("manual", "csv", "json", "bookmarklet")


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _file_b64(rel: str | None) -> str | None:
    if not rel:
        return None
    path = resolve_path(rel)
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def _fingerprint(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()


def _evidence_fp(product_key: dict | None, seller: str | None, e: dict) -> str:
    return _fingerprint(
        product_key, seller, e.get("kind"), e.get("aspect"), e.get("claim"), e.get("source_url"), e.get("polarity")
    )


def _wardrobe_fp(w: dict, dated: bool = True) -> str:
    return _fingerprint(
        w.get("name"), w.get("category"), w.get("brand"), w.get("source_url"), w.get("added_at") if dated else None
    )


# ----------------------------------------------------------------------------- export
def export_all(db: Session, include_images: bool = True) -> dict:
    sellers = db.query(Seller).all()
    seller_slug = {s.id: s.slug for s in sellers}
    decisions = db.query(ProductRelation).filter(ProductRelation.user_confirmed.isnot(None)).all()
    user_evidence = db.query(Evidence).filter(Evidence.created_by == "user").all()
    wardrobe = db.query(WardrobeItem).all()
    judgements = db.query(Judgement).order_by(Judgement.created_at).all()
    product_ids = (
        {f.product_id for f in db.query(Feedback) if f.product_id}
        | {j.product_id for j in judgements if j.product_id}
        | {s.product_id for s in db.query(SavedItem)}
        | {e.product_id for e in user_evidence if e.product_id}
        | {w.product_id for w in wardrobe if w.product_id}
        | {r.a_id for r in decisions}
        | {r.b_id for r in decisions}
        # products you entered yourself are your data, referenced or not
        | {pid for (pid,) in db.query(Product.id).filter(Product.data_origin.in_(USER_ORIGINS))}
    )
    products = db.query(Product).filter(Product.id.in_(product_ids)).all() if product_ids else []
    pkey = {p.id: {"seller": seller_slug.get(p.seller_id), "external_id": p.external_id} for p in products}

    def product_view(p: Product) -> dict:
        uploads = [i for i in p.images if i.url.startswith("upload:")]
        return {
            "seller": seller_slug.get(p.seller_id),
            "external_id": p.external_id,
            "title_original": p.title_original,
            "title_en": p.title_en,
            "description_original": p.description_original,
            "brand": p.brand,
            "category": p.category,
            "colours": p.colours,
            "materials": p.materials,
            "material_text": p.material_text,
            "sizes": p.sizes,
            "fit_tags": p.fit_tags,
            "logo_level": p.logo_level,
            "price": p.price,
            "currency": p.currency,
            "source_url": p.source_url,
            "links": p.links,
            "status": p.status,
            "data_origin": p.data_origin,
            "completeness": p.completeness,
            "seasons": p.seasons,
            "first_seen_at": _iso(p.first_seen_at),
            "image_urls": [i.url for i in p.images if not i.url.startswith("upload:")],
            "uploaded_images_b64": [b for i in uploads if (b := _file_b64(i.local_path))] if include_images else [],
        }

    evidence_out = []
    for e in user_evidence:
        row = {
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
        row["fingerprint"] = _evidence_fp(row["product"], row["seller"], row)
        evidence_out.append(row)

    wardrobe_out = []
    for w in wardrobe:
        row = {
            "name": w.name,
            "category": w.category,
            "brand": w.brand,
            "colours": w.colours,
            "materials": w.materials,
            "seasons": w.seasons,
            "source_url": w.source_url,
            "notes": w.notes,
            "rating": w.rating,
            "added_at": _iso(w.added_at),
            "product": pkey.get(w.product_id) if w.product_id else None,
            "image_b64": _file_b64(w.image_path) if include_images else None,
        }
        row["fingerprint"] = _wardrobe_fp(row)
        wardrobe_out.append(row)

    return {
        "format": FORMAT,
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
                "reputation_status": s.reputation_status,
            }
            for s in sellers
        ],
        "watched_urls": [
            {"url": w.url, "seller": seller_slug.get(w.seller_id), "active": w.active} for w in db.query(WatchedUrl)
        ],
        "products": [product_view(p) for p in products],
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
                "added_at": _iso(s.added_at),
            }
            for s in db.query(SavedItem)
        ],
        "evidence": evidence_out,
        "judgements": [
            {
                "product": pkey.get(j.product_id) if j.product_id else None,
                "family_key": j.family_key,
                "taste": j.taste,
                "owned_similar": j.owned_similar,
                "too_expensive": j.too_expensive,
                "quality_unclear": j.quality_unclear,
                "note": j.note,
                "sampled_by": j.sampled_by,
                "snapshot": j.snapshot,
                "created_at": _iso(j.created_at),
            }
            for j in judgements
        ],  # fmt: skip
        "match_decisions": [
            {"a": pkey.get(r.a_id), "b": pkey.get(r.b_id), "same": bool(r.user_confirmed)} for r in decisions
        ],
        "wardrobe": wardrobe_out,
        "inspiration": [
            {
                "note": i.note,
                "polarity": i.polarity,
                "tags": i.tags,
                "added_at": _iso(i.added_at),
                "image_b64": _file_b64(i.path) if include_images else None,
            }
            for i in db.query(InspirationImage)
        ],
    }


# ----------------------------------------------------------------------------- import
def _embed_upload(data: bytes) -> tuple[str, bytes | None, str | None]:
    """Store an uploaded photo and compute its embedding, so restored items keep teaching the model."""
    from . import embeddings as emb
    from .images import store_image_bytes

    path, pil = store_image_bytes(data, "uploads")
    backend = emb.get_image_backend()
    return path, emb.to_bytes(backend.embed(pil)), backend.name


def import_all(db: Session, data: dict) -> dict:
    """Merge a backup into the current database. Idempotent: a second import of the same file changes nothing."""
    if data.get("format") != FORMAT:
        raise ValueError("not a Wardrobe Scout backup file")
    if int(data.get("version") or 1) > EXPORT_VERSION:
        raise ValueError(f"backup version {data.get('version')} is newer than this app supports ({EXPORT_VERSION})")
    counts = dict.fromkeys(
        ("settings", "sellers", "products", "feedback", "saved", "evidence", "match_decisions", "wardrobe",
         "inspiration", "watched_urls", "judgements"),
        0,
    )  # fmt: skip
    for k, v in (data.get("settings") or {}).items():
        row = db.get(AppSetting, k)
        if row is None:
            db.add(AppSetting(key=k, value=v))
            counts["settings"] += 1
        elif row.value != v:
            row.value = v
            counts["settings"] += 1
    sellers = {s.slug: s for s in db.query(Seller)}
    for s in data.get("sellers", []):
        if s["slug"] not in sellers:
            fields = ("name", "slug", "platform", "base_url", "status", "priority", "notes", "scan_interval_hours",
                      "reputation_status")  # fmt: skip
            obj = Seller(**{k: s.get(k) for k in fields if s.get(k) is not None})
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
        if find_product({"seller": p.get("seller"), "external_id": p["external_id"]}) is not None:
            continue
        obj = Product(
            seller_id=sellers[p["seller"]].id if p.get("seller") in sellers else None,
            external_id=p["external_id"],
            title_original=p.get("title_original") or "",
            title_en=p.get("title_en") or "",
            description_original=p.get("description_original"),
            brand=p.get("brand"),
            category=p.get("category"),
            colours=p.get("colours") or [],
            materials=p.get("materials") or [],
            material_text=p.get("material_text"),
            sizes=p.get("sizes") or [],
            fit_tags=p.get("fit_tags") or [],
            logo_level=p.get("logo_level") or "unknown",
            price=p.get("price"),
            currency=(p["currency"] if "currency" in p else "CNY") or "",  # "" = not stated, kept as unknown
            source_url=p["source_url"],
            links=p.get("links") or {},
            status=p.get("status") or "active",
            data_origin=p.get("data_origin") or "manual",
            completeness=p.get("completeness") or "partial",
            seasons=p.get("seasons") or [],
            first_seen_at=_dt(p.get("first_seen_at")) or utcnow(),
            is_new=False,
            # Restored from a backup: its page and photos are re-read by the next scan.
            analysis_state="discovered" if (p.get("data_origin") or "manual") == "scraped" else "analysed",
        )
        images = [ProductImage(url=u, position=i) for i, u in enumerate(p.get("image_urls") or [])]
        for b64 in p.get("uploaded_images_b64") or []:
            from .images import store_image_bytes

            rel, _ = store_image_bytes(base64.b64decode(b64), "uploads")
            images.append(ProductImage(url=f"upload:{rel}", local_path=rel, fetch_status="uploaded",
                                       position=len(images)))  # fmt: skip
        obj.images = images
        db.add(obj)
        counts["products"] += 1
    db.flush()

    existing_fb = {(f.product_id, f.action, _iso(f.created_at)) for f in db.query(Feedback)}
    for f in data.get("feedback", []):
        p = find_product(f.get("product"))
        created = _dt(f.get("created_at")) or utcnow()
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
        existing_fb.add((p.id if p else None, f["action"], _iso(created)))
        counts["feedback"] += 1
    db.flush()
    existing_j = {(j.family_key, _iso(j.created_at)) for j in db.query(Judgement)}
    for j in data.get("judgements", []):
        p = find_product(j.get("product"))
        created = _dt(j.get("created_at")) or utcnow()
        # Family keys embed local ids; on another database the product's own family is the stable identity.
        key = (f"g{p.group_id}" if p.group_id is not None else f"p{p.id}") if p else j.get("family_key") or ""
        if (key, _iso(created)) in existing_j:
            continue
        db.add(Judgement(product_id=p.id if p else None, family_key=key, taste=int(j["taste"]),
                         owned_similar=bool(j.get("owned_similar")), too_expensive=bool(j.get("too_expensive")),
                         quality_unclear=bool(j.get("quality_unclear")), note=j.get("note"),
                         sampled_by=j.get("sampled_by") or "uniform", snapshot=j.get("snapshot") or {},
                         created_at=created))  # fmt: skip
        existing_j.add((key, _iso(created)))
        counts["judgements"] += 1
    db.flush()
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
                    added_at=_dt(s.get("added_at")) or utcnow(),
                )
            )
            counts["saved"] += 1
    db.flush()

    # Evidence: identity = content fingerprint, so re-imports and repeated restores never duplicate it.
    pkey_of = {}
    for prod in db.query(Product):
        seller = next((slug for slug, obj in sellers.items() if obj.id == prod.seller_id), None)
        pkey_of[prod.id] = {"seller": seller, "external_id": prod.external_id}
    slug_of = {obj.id: slug for slug, obj in sellers.items()}
    have_ev = set()
    for e in db.query(Evidence).filter(Evidence.created_by == "user"):
        key = pkey_of.get(e.product_id) if e.product_id else None
        have_ev.add(_evidence_fp(key, slug_of.get(e.seller_id), {
            "kind": e.kind, "aspect": e.aspect, "claim": e.claim, "source_url": e.source_url, "polarity": e.polarity,
        }))  # fmt: skip
    for e in data.get("evidence", []):
        if e.get("product") and find_product(e["product"]) is None:
            continue  # product missing from a version-1 file: cannot attach faithfully
        fp = _evidence_fp(e.get("product"), e.get("seller"), e)
        if fp in have_ev:
            continue
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
                captured_at=_dt(e.get("captured_at")) or utcnow(),
                created_by="user",
            )
        )
        have_ev.add(fp)
        counts["evidence"] += 1

    # Your "same / not the same" decisions.
    for m in data.get("match_decisions", []):
        a, b = find_product(m.get("a")), find_product(m.get("b"))
        if a is None or b is None:
            continue
        lo, hi = min(a.id, b.id), max(a.id, b.id)
        rel = db.query(ProductRelation).filter_by(a_id=lo, b_id=hi).one_or_none()
        same = bool(m.get("same"))
        if rel is not None and rel.user_confirmed == same:
            continue
        if rel is None:
            rel = ProductRelation(a_id=lo, b_id=hi, relation="visually_similar", score=0, signals={})
            db.add(rel)
        rel.user_confirmed = same
        rel.relation = "confirmed_identical" if same else "not_same"
        counts["match_decisions"] += 1
    db.flush()

    have_wardrobe: set[str] = set()
    for w in db.query(WardrobeItem):
        row = {"name": w.name, "category": w.category, "brand": w.brand, "source_url": w.source_url,
               "added_at": _iso(w.added_at)}  # fmt: skip
        have_wardrobe |= {_wardrobe_fp(row), "undated:" + _wardrobe_fp(row, dated=False)}
    for w in data.get("wardrobe", []):
        # Version-1 files carry no date: match those on the remaining fields.
        fp = _wardrobe_fp(w) if w.get("added_at") else "undated:" + _wardrobe_fp(w, dated=False)
        if fp in have_wardrobe:
            continue
        path = embedding = backend = None
        if w.get("image_b64"):
            path, embedding, backend = _embed_upload(base64.b64decode(w["image_b64"]))
        p = find_product(w.get("product"))
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
                product_id=p.id if p else None,
                embedding=embedding,
                embedding_backend=backend,
                added_at=_dt(w.get("added_at")) or utcnow(),
            )
        )
        have_wardrobe |= {fp, "undated:" + _wardrobe_fp(w, dated=False)}
        counts["wardrobe"] += 1

    # Inspiration photos are stored content-addressed: the same image always lands on the same path.
    have_insp = {(i.path, i.polarity) for i in db.query(InspirationImage)}
    for i in data.get("inspiration", []):
        if not i.get("image_b64"):
            continue
        path, embedding, backend = _embed_upload(base64.b64decode(i["image_b64"]))
        if (path, i.get("polarity", 1)) in have_insp:
            continue
        db.add(
            InspirationImage(
                path=path, note=i.get("note"), polarity=i.get("polarity", 1), tags=i.get("tags") or [],
                embedding=embedding, embedding_backend=backend, added_at=_dt(i.get("added_at")) or utcnow(),
            )
        )  # fmt: skip
        have_insp.add((path, i.get("polarity", 1)))
        counts["inspiration"] += 1
    db.flush()
    if counts["match_decisions"]:
        from .dedup import recompute_groups

        recompute_groups(db)
    db.commit()
    return counts


# ----------------------------------------------------------------------------- full snapshot (database + media)
SNAPSHOT_DIRS = ("images", "uploads")


def write_full_snapshot(database_path: Path, data_dir: Path, out: Path) -> dict:
    """Zip a consistent SQLite copy (online backup API, safe while the app runs) and the photo folders."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".db.tmp")
    with sqlite3.connect(database_path) as a, sqlite3.connect(tmp) as b:
        a.backup(b)
    files = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "wardrobe_scout.db")
        for sub in SNAPSHOT_DIRS:
            root = data_dir / sub
            for f in sorted(root.rglob("*")) if root.exists() else []:
                if f.is_file():
                    z.write(f, f"{sub}/{f.relative_to(root).as_posix()}", compress_type=zipfile.ZIP_STORED)
                    files += 1
        z.writestr("manifest.json", json.dumps({"format": "wardrobe-scout-snapshot", "version": 1,
                                                "created_at": utcnow().isoformat(), "files": files}))  # fmt: skip
    tmp.unlink(missing_ok=True)
    return {"path": str(out), "files": files, "bytes": out.stat().st_size}


def restore_full_snapshot(snapshot: Path, data_dir: Path) -> dict:
    """Restore a snapshot into an EMPTY data directory (never overwrites an existing installation)."""
    if data_dir.exists() and any(p for p in data_dir.iterdir() if p.name not in ("api_token",)):
        raise ValueError(f"{data_dir} is not empty; restore into a new folder and point WS_DATA_DIR at it")
    data_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(snapshot) as z:
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != "wardrobe-scout-snapshot":
            raise ValueError("not a Wardrobe Scout snapshot")
        root = data_dir.resolve()
        for name in z.namelist():
            if name == "manifest.json":
                continue
            target = (data_dir / name).resolve()
            if not target.is_relative_to(root):  # zip-slip protection
                raise ValueError(f"unsafe path in snapshot: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src:
                target.write_bytes(src.read())
    return {"database": str(data_dir / "wardrobe_scout.db"), "files": manifest.get("files", 0)}
