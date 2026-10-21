"""Normalise raw listings and persist them with change tracking and provenance."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy.orm import Session

from ..logging_setup import get_logger
from ..models import Evidence, Product, ProductImage, ProductObservation, Seller, utcnow
from ..services import embeddings as emb
from ..services.images import ImageError, download_image, phash_of
from .base import RawListing
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

    if not created:
        if old_price is not None and p.price is not None and abs(old_price - p.price) > 0.009:
            changes.append(f"price {old_price:g} → {p.price:g}")
        if sorted(old_sizes) != sorted(p.sizes or []):
            changes.append(f"sizes {old_sizes} → {p.sizes}")
        if raw.available is False:
            changes.append("marked unavailable")

    # Images: keep existing rows for unchanged URLs, append new ones in order
    existing = {img.url: img for img in p.images}
    new_list = []
    for pos, url in enumerate(raw.image_urls):
        img = existing.get(url) or ProductImage(url=url, referer=raw.image_referer)
        img.position = pos
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
    db.flush()
    return UpsertResult(p, created, changes)


def process_images(db: Session, p: Product, client: PoliteClient | None, max_images: int = 12) -> int:
    """Download (if needed), hash and embed product images. Returns number embedded."""
    backend = emb.get_image_backend()
    done = 0
    vectors = []
    for img in p.images[:max_images]:
        try:
            if img.local_path is None and img.fetch_status != "uploaded":
                if client is None:
                    continue
                rel, pil = download_image(client, img.url, img.referer)
                img.local_path = rel
                img.fetch_status = "ok"
                img.fetch_error = None
            else:
                if img.local_path is None:
                    continue
                if img.embedding is not None and img.embedding_backend == backend.name and img.phash:
                    vectors.append(emb.from_bytes(img.embedding))
                    continue
                from PIL import Image

                from ..services.images import resolve_path

                pil = Image.open(resolve_path(img.local_path)).convert("RGB")
            img.width, img.height = pil.size
            img.phash = phash_of(pil)
            v = backend.embed(pil)
            img.embedding = emb.to_bytes(v)
            img.embedding_backend = backend.name
            vectors.append(v)
            done += 1
        except (ImageError, OSError) as e:
            img.fetch_status = "failed"
            img.fetch_error = str(e)[:500]
            log.info("image_failed", product=p.id, url=img.url, error=str(e)[:200])
    if vectors:
        # Product vector: first image (usually the hero shot) weighted double + mean of the rest
        stack = np.vstack(vectors)
        weights = np.ones(len(vectors))
        weights[0] = 2.0
        p.image_embedding = emb.to_bytes(emb.normalise((stack * weights[:, None]).sum(0)))
        p.embedding_backend = backend.name
    p.text_embedding = emb.to_bytes(emb.embed_text(emb.product_text(p)))
    db.flush()
    return done
