"""Normalise raw listings and persist them with change tracking and provenance."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sqlalchemy.orm import Session

from ..logging_setup import get_logger
from ..models import Evidence, Product, ProductImage, ProductObservation, Seller, utcnow
from ..services import embeddings as emb
from ..services import image_select as sel
from ..services.images import ImageError, fetch_image_bytes, phash_of, resolve_path, store_image_bytes
from .base import AlbumRef, RawListing
from .http import PoliteClient
from .parsing import materials_to_text, parse_listing_text
from .translate import translate

log = get_logger(__name__)

# Phrases in listings that constitute construction / hardware / accuracy *claims*.
CLAIM_PATTERNS: list[tuple[str, str, str]] = [
    (
        "hardware",
        r"riri|lampo|excella|ykk|双头拉链|two-way zip|贝壳扣|shell button|牛角扣|horn button|金属五金",
        "Hardware described",
    ),
    (
        "construction",
        r"包边|bound seam|双针|double[- ]needle|平缝|flatlock|手工|hand[- ]finished|全衬|fully lined|半衬|half[- ]lined",
        "Construction detail described",
    ),
    ("accuracy", r"对版|1:1|一比一|原版|accurate|复刻|reference sample|开版", "Seller claims accuracy to reference"),
    (
        "construction",
        r"定织|定染|custom[- ]woven|custom[- ]dyed|进口面料|imported fabric|意大利面料|italian fabric",
        "Fabric sourcing claim",
    ),
]


@dataclass
class UpsertResult:
    product: Product
    created: bool
    changes: list[str] = field(default_factory=list)


def content_hash(raw: RawListing) -> str:
    payload = json.dumps(
        [raw.title, raw.description, raw.price, sorted(raw.image_urls), raw.sizes, raw.available],
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _apply_attributes(p: Product, raw: RawListing, use_ai: bool) -> None:
    p.title_original = raw.title
    p.description_original = raw.description
    title_en, m1 = translate(raw.title, use_ai=use_ai)
    desc_en, m2 = translate(raw.description, use_ai=use_ai) if raw.description else ("", "none")
    p.title_en = title_en
    p.description_en = desc_en or None
    p.translation_method = "ai" if "ai" in (m1, m2) else ("glossary" if "glossary" in (m1, m2) else "none")

    attrs = parse_listing_text(raw.title, raw.description, title_en, desc_en)
    p.seller_item_code = attrs.item_code
    p.brand = (
        attrs.brand
        or (raw.extra.get("vendor") if raw.extra.get("vendor") and raw.extra.get("vendor") != "" else None)
        or p.brand
    )
    p.category = attrs.category or _category_from_hint(raw.category_hint) or p.category
    p.colours = attrs.colours
    p.materials = attrs.materials
    p.material_text = materials_to_text(attrs.materials) or None
    p.gsm = attrs.gsm
    p.sizes = raw.sizes or attrs.sizes
    p.fit_tags = attrs.fit_tags
    p.logo_level = attrs.logo_level
    p.seasons = attrs.seasons
    p.price = raw.price if raw.price is not None else attrs.price
    p.currency = raw.currency or "CNY"
    links = dict(attrs.links)
    links.update(raw.links)
    p.links = links


def _category_from_hint(hint: str | None) -> str | None:
    if not hint:
        return None
    from .parsing import parse_category

    return parse_category(hint)


def _seller_claim_evidence(p: Product, raw: RawListing) -> list[Evidence]:
    """Turn listing statements into explicit *seller claim* evidence rows."""
    out: list[Evidence] = []
    now = raw.fetched_at or utcnow()
    if p.materials:
        out.append(
            Evidence(
                kind="seller_claim",
                aspect="material",
                claim=f"Listing states: {p.material_text}",
                polarity=0,
                strength=0.6 if any(m.get("pct") for m in p.materials) else 0.3,
                source_url=raw.source_url,
                source_title="Seller listing",
                captured_at=now,
            )
        )
    if p.gsm:
        out.append(
            Evidence(
                kind="seller_claim",
                aspect="gsm",
                claim=f"Listing states fabric weight {p.gsm} g/m²",
                polarity=0,
                strength=0.6,
                source_url=raw.source_url,
                source_title="Seller listing",
                captured_at=now,
            )
        )
    text = f"{raw.title}\n{raw.description or ''}\n{p.description_en or ''}".lower()
    for aspect, pattern, label in CLAIM_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            out.append(
                Evidence(
                    kind="seller_claim",
                    aspect=aspect,
                    claim=f"{label}: “{m.group(0)}”",
                    polarity=0,
                    strength=0.3,
                    source_url=raw.source_url,
                    source_title="Seller listing",
                    captured_at=now,
                )
            )
    return out


def upsert_listing(
    db: Session,
    seller: Seller | None,
    raw: RawListing,
    *,
    origin: str = "scraped",
    use_ai: bool = False,
    completeness: str | None = None,
) -> UpsertResult:
    seller_id = seller.id if seller else None
    q = db.query(Product).filter(Product.external_id == raw.external_id)
    q = q.filter(Product.seller_id == seller_id) if seller_id else q.filter(Product.seller_id.is_(None))
    p = q.one_or_none()
    h = content_hash(raw)
    now = raw.fetched_at or utcnow()
    changes: list[str] = []
    created = p is None
    # The first product-page read of a product found on a catalogue listing completes its creation:
    # nothing "changed" yet (its listing observation was recorded at discovery).
    first_read = not created and p.detail_fetched_at is None and p.analysis_state == "discovered"

    if created:
        p = Product(
            seller_id=seller_id,
            external_id=raw.external_id,
            source_url=raw.source_url,
            data_origin=origin,
            first_seen_at=now,
            is_new=True,
        )
        db.add(p)
    else:
        if p.status != "active":
            changes.append(f"status {p.status} → active")
        old_price, old_sizes = p.price, list(p.sizes or [])
        if p.content_hash == h and p.completeness != "seed_unverified":
            p.last_seen_at = now
            p.status = "active"
            p.detail_fetched_at = now
            update_state(p)
            db.flush()
            return UpsertResult(p, False, changes)

    _apply_attributes(p, raw, use_ai)
    p.source_url = raw.source_url
    p.last_seen_at = now
    p.status = "active" if raw.available is not False else "unavailable"
    p.content_hash = h
    if origin != "scraped" or created:
        p.data_origin = origin if created or p.data_origin == "seed" else p.data_origin
    if p.data_origin == "seed" and origin == "scraped":
        p.data_origin = "scraped"  # seed record has now been verified against the live page
    p.completeness = completeness or ("complete" if (raw.title and raw.image_urls and p.price) else "partial")

    if not created and not first_read:
        if old_price is not None and p.price is not None and abs(old_price - p.price) > 0.009:
            changes.append(f"price {old_price:g} → {p.price:g}")
        if sorted(old_sizes) != sorted(p.sizes or []):
            changes.append(f"sizes {old_sizes} → {p.sizes}")
        if raw.available is False:
            changes.append("marked unavailable")

    # Images: keep existing rows (and their analysis) for unchanged URLs, append new ones in order
    existing = {img.url: img for img in p.images}
    new_list = []
    for pos, url in enumerate(raw.image_urls):
        img = existing.get(url) or ProductImage(url=url, referer=raw.image_referer)
        img.position = pos
        thumb = raw.image_thumbs[pos] if pos < len(raw.image_thumbs) else None
        if thumb:
            img.thumb_url = thumb
        new_list.append(img)
    if raw.image_urls or created:
        p.images = new_list if raw.image_urls else [i for i in p.images if i.fetch_status == "uploaded"]

    # Replace system-generated seller claims (user/AI evidence is preserved)
    p.evidence = [e for e in p.evidence if not (e.kind == "seller_claim" and e.created_by == "system")]
    p.evidence.extend(_seller_claim_evidence(p, raw))

    if created or changes:
        db.add(
            ProductObservation(
                product=p,
                observed_at=now,
                price=p.price,
                sizes=p.sizes,
                status=p.status,
                source_url=raw.source_url,
                change="; ".join(changes) or "first seen",
            )
        )
    p.detail_fetched_at = now
    update_state(p)
    db.flush()
    return UpsertResult(p, created, changes)


# ----------------------------------------------------------------------------- progressive scanning
def same_title(listing_title: str | None, stored: str | None) -> bool:
    """Catalogue grids may shorten titles; a listing title that is a prefix of the stored one is unchanged."""
    a = re.sub(r"\s+", " ", (listing_title or "").strip())
    b = re.sub(r"\s+", " ", (stored or "").strip())
    return not a or a == b or (len(a) >= 12 and b.startswith(a.rstrip("…. ")))


def upsert_discovered(db: Session, seller: Seller, ref: AlbumRef, now: datetime) -> tuple[Product, bool, bool]:
    """Record a product seen on a catalogue listing page. Returns (product, created, listing_changed).

    New products become browsable immediately with what the listing shows (title, price, category, cover
    photo) in the ``discovered`` state; their product page and photos are analysed afterwards.
    """
    p = db.query(Product).filter(Product.seller_id == seller.id, Product.external_id == ref.external_id).one_or_none()
    if p is None:
        p = Product(
            seller_id=seller.id,
            external_id=ref.external_id,
            source_url=ref.url,
            data_origin="scraped",
            first_seen_at=now,
            last_seen_at=now,
            is_new=True,
            completeness="partial",
            analysis_state="discovered",
        )
        _apply_attributes(p, RawListing(external_id=ref.external_id, source_url=ref.url, title=ref.title or ""), False)
        if ref.cover_url:
            p.images = [ProductImage(url=ref.cover_url, thumb_url=ref.cover_thumb, referer=ref.url, position=0)]
        p.text_embedding = emb.to_bytes(emb.embed_text(emb.product_text(p)))
        db.add(p)
        db.add(ProductObservation(product=p, observed_at=now, price=p.price, sizes=[], status="active",
                                  source_url=ref.url, change="first seen (catalogue listing)"))  # fmt: skip
        return p, True, False
    changed = not same_title(ref.title, p.title_original)
    if p.status != "active" and p.data_origin == "scraped":
        p.status = "active"
        changed = True
    p.last_seen_at = now
    return p, False, changed


@dataclass
class ImageAnalysis:
    local_path: str
    width: int
    height: int
    phash: str
    vector: np.ndarray
    backend: str
    features: dict
    role: str
    confidence: float
    nbytes: int


def analyse_image_data(data: bytes) -> ImageAnalysis:
    """Store, hash, embed and classify one downloaded image. Thread-safe; no database access."""
    from PIL import Image

    rel, pil = store_image_bytes(data)
    small = pil.copy()
    small.thumbnail((sel.ANALYSIS_SIDE, sel.ANALYSIS_SIDE), Image.BILINEAR)
    f = sel.analyse(small)
    role, conf = sel.classify(f)
    backend = emb.get_image_backend()
    return ImageAnalysis(
        rel,
        pil.width,
        pil.height,
        phash_of(small),
        backend.embed(small),
        backend.name,
        f.as_dict(),
        role,
        conf,
        len(data),
    )


def analyse_image_file(rel: str) -> ImageAnalysis:
    data = resolve_path(rel).read_bytes()
    a = analyse_image_data(data)
    a.local_path = rel
    return a


def fetch_and_analyse(
    client: PoliteClient, img_url: str, referer: str | None, fallback: str | None = None
) -> ImageAnalysis:
    """Fetch (preferring the small display variant) and analyse. Falls back to the original URL if the
    variant is unavailable."""
    try:
        data = fetch_image_bytes(client, img_url, referer)
    except ImageError:
        if not fallback or fallback == img_url:
            raise
        data = fetch_image_bytes(client, fallback, referer)
    return analyse_image_data(data)


def apply_analysis(img: ProductImage, a: ImageAnalysis) -> None:
    img.local_path = a.local_path
    img.fetch_status = img.fetch_status if img.fetch_status == "uploaded" else "ok"
    img.fetch_error = None
    img.width, img.height = a.width, a.height
    img.phash = a.phash
    img.embedding = emb.to_bytes(a.vector)
    img.embedding_backend = a.backend
    img.analysis = a.features
    if img.role != sel.REPEATED:
        img.role, img.role_confidence = a.role, a.confidence


def copy_analysis(dst: ProductImage, src: ProductImage) -> None:
    """Reuse another listing's analysis of the very same image URL (no download)."""
    dst.local_path, dst.fetch_status, dst.fetch_error = src.local_path, "ok", None
    dst.width, dst.height, dst.phash = src.width, src.height, src.phash
    dst.embedding, dst.embedding_backend = src.embedding, src.embedding_backend
    dst.analysis, dst.role, dst.role_confidence = src.analysis, src.role, src.role_confidence


def _usable(img: ProductImage, backend: str) -> bool:
    return img.embedding is not None and img.embedding_backend == backend


def _aesthetic(img: ProductImage) -> bool:
    return img.role is None or img.role in sel.AESTHETIC_ROLES  # unclassified legacy images count as before


def next_image(p: Product, k: int, max_examine: int, backend: str) -> ProductImage | None:
    """Progressive policy: the next gallery image to analyse, or None when ``k`` useful non-redundant views
    exist, ``max_examine`` images were downloaded, or the gallery is exhausted. Size charts, text graphics
    and repeated banners still count towards the download budget but never as useful views."""
    useful: list[np.ndarray] = []
    downloaded = 0
    for img in sorted(p.images, key=lambda i: i.position):
        done = (
            _usable(img, backend) or img.fetch_status == "failed" or (img.role == sel.REPEATED and not img.local_path)
        )
        if done:
            if img.fetch_status in ("ok", "failed"):
                downloaded += 1
            if _usable(img, backend) and _aesthetic(img):
                v = emb.from_bytes(img.embedding)
                if all(float(v @ u) < 0.97 for u in useful):
                    useful.append(v)
                    if len(useful) >= k:
                        return None
            continue
        if img.local_path:
            return img  # stored or uploaded file: analyse without network
        if downloaded >= max_examine:
            return None
        return img
    return None


def _quality(img: ProductImage) -> float:
    d = img.analysis or {}
    f = sel.Features(**{k: d.get(k, 0) for k in sel.Features.__dataclass_fields__}) if d else None
    return sel.quality(f, img.position) if f else 0.5 - 0.02 * img.position


def finalize_product(db: Session, p: Product, k: int, now: datetime | None = None) -> bool:
    """Choose the aesthetic views among analysed images and rebuild the product vector. Returns has_vector."""
    backend = emb.get_image_backend().name
    imgs = sorted(p.images, key=lambda i: i.position)
    cands = [
        sel.Candidate(i, img.role or sel.GARMENT, _quality(img), emb.from_bytes(img.embedding))
        for i, img in enumerate(imgs)
        if _usable(img, backend)
    ]
    chosen = sel.select(cands, max_k=k)
    for i, img in enumerate(imgs):
        if _usable(img, backend):
            img.selected = i in chosen
    if chosen:
        p.image_embedding = emb.to_bytes(sel.product_vector([emb.from_bytes(imgs[i].embedding) for i in chosen]))
        p.embedding_backend = backend
    elif any(_usable(i, backend) for i in imgs) or not any(i.embedding for i in imgs):
        p.image_embedding = None
    p.text_embedding = emb.to_bytes(emb.embed_text(emb.product_text(p)))
    p.analysed_at = now or utcnow()
    update_state(p)
    return p.image_embedding is not None


def update_state(p: Product) -> None:
    detailed = p.detail_fetched_at is not None or p.data_origin != "scraped"
    analysed = p.analysed_at is not None
    if detailed and analysed:
        p.analysis_state = "deep" if p.analysis_state == "deep" else "analysed"
    elif detailed or analysed:
        p.analysis_state = "partial"
    else:
        p.analysis_state = "discovered"


def process_images(
    db: Session, p: Product, client: PoliteClient | None, max_images: int = 12, *, deep: bool = True
) -> int:
    """Analyse a product's photos (used by imports, the image job and recompute).

    ``deep`` (a product you asked for directly): up to ``deep_images`` useful views, so cross-seller photo
    matching has several photos to compare. Otherwise the scan policy: ``analysis_images`` useful views from
    at most ``max_examine_images`` downloads. Stored photos are re-analysed without network. The aesthetic
    vector always uses the selected best views. Returns the number of images analysed.
    """
    from ..config import get_settings

    s = get_settings()
    backend = emb.get_image_backend().name
    if deep:
        k = budget = min(max_images, s.deep_images)
    else:
        k, budget = s.analysis_images, min(max_images, s.max_examine_images)
    done = 0
    # Stored photos analysed by an older pipeline or another backend: re-analyse locally (no network).
    for img in p.images:
        if img.local_path and not _usable(img, backend) or (img.local_path and img.role is None):
            try:
                apply_analysis(img, analyse_image_file(img.local_path))
                done += 1
            except (ImageError, OSError) as e:
                img.fetch_status, img.fetch_error = "failed", str(e)[:500]
    while True:
        img = next_image(p, k, budget, backend)
        if img is None:
            break
        try:
            if img.local_path:
                a = analyse_image_file(img.local_path)
            elif client is None:
                break
            else:
                a = fetch_and_analyse(client, img.thumb_url or img.url, img.referer, fallback=img.url)
            apply_analysis(img, a)
            done += 1
        except (ImageError, OSError) as e:
            img.fetch_status = "failed"
            img.fetch_error = str(e)[:500]
            log.info("image_failed", product=p.id, url=img.url, error=str(e)[:200])
    finalize_product(db, p, s.analysis_images)
    db.flush()
    return done
