"""Seller scans and single-URL imports."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import ScanRun, Seller, WatchedUrl, utcnow
from ..services import ai, dedup
from .base import RawListing, SellerAdapter
from .generic import GenericPageAdapter, ShopifyAdapter, WeidianAdapter, parse_generic_product, parse_weidian_html
from .http import BlockedError, FetchError, PoliteClient
from .pipeline import process_images, upsert_listing
from .yupoo import ParserError, YupooAdapter, parse_album_page, yupoo_base

log = get_logger(__name__)

ADAPTERS: dict[str, type[SellerAdapter]] = {
    "yupoo": YupooAdapter,
    "weidian": WeidianAdapter,
    "shopify": ShopifyAdapter,
    "website": GenericPageAdapter,
}

Progress = Callable[[str, float | None], None]

from .scanner import BLOCKED_HELP, drain_pending, scan_many  # noqa: E402

__all__ = ["ADAPTERS", "drain_pending", "import_html", "import_url", "scan_many", "scan_seller"]


def adapter_for_url(url: str) -> type[SellerAdapter]:
    if YupooAdapter.handles(url):
        return YupooAdapter
    if WeidianAdapter.handles(url):
        return WeidianAdapter
    return GenericPageAdapter


def slug_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.endswith(".x.yupoo.com"):
        return host.split(".")[0]
    return re.sub(r"^www\.", "", host)


def get_or_create_seller_for_url(db: Session, url: str) -> Seller:
    slug = slug_from_url(url)
    seller = db.query(Seller).filter_by(slug=slug).one_or_none()
    if seller:
        return seller
    platform = "yupoo" if "yupoo.com" in url else "weidian" if "weidian.com" in url else "website"
    base = yupoo_base(url) if platform == "yupoo" else f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    seller = Seller(
        name=slug if platform != "weidian" else "Weidian (unassigned)",
        slug=slug,
        platform=platform,
        base_url=base,
        status="active" if platform != "weidian" else "paused",
    )
    db.add(seller)
    db.flush()
    return seller


def _ingest_one(
    db: Session,
    seller: Seller | None,
    raw: RawListing,
    client: PoliteClient | None,
    origin: str = "scraped",
    completeness: str | None = None,
):
    use_ai = ai.is_enabled("translate")
    res = upsert_listing(db, seller, raw, origin=origin, use_ai=use_ai, completeness=completeness)
    if get_settings().download_images or client is None:
        process_images(
            db, res.product, client if get_settings().download_images else None, get_settings().max_images_per_product
        )
    dedup.match_product(db, res.product)
    return res


def import_url(db: Session, url: str, *, seller_id: int | None = None, client: PoliteClient | None = None) -> dict:
    """Fetch one product page from a supported site and store it."""
    own = client is None
    client = client or PoliteClient()
    try:
        adapter_cls = adapter_for_url(url)
        adapter = adapter_cls(client)
        seller = db.get(Seller, seller_id) if seller_id else get_or_create_seller_for_url(db, url)
        raw = adapter.fetch_listing(url)
        res = _ingest_one(db, seller, raw, client)
        watched = db.query(WatchedUrl).filter_by(url=url).one_or_none()
        if watched:
            watched.last_checked_at, watched.last_status, watched.product_id = utcnow(), "ok", res.product.id
            watched.last_message = "; ".join(res.changes) or None
        dedup.recompute_groups(db)
        db.commit()
        return {"ok": True, "product_id": res.product.id, "created": res.created, "changes": res.changes}
    except BlockedError as e:
        db.rollback()
        return {"ok": False, "blocked": True, "error": BLOCKED_HELP.format(reason=str(e))}
    except FetchError as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            client.close()


def import_html(
    db: Session, url: str, html: str, *, seller_id: int | None = None, client: PoliteClient | None = None
) -> dict:
    """Bookmarklet / browser-assisted import: parse HTML you loaded in your own browser."""
    host = urlparse(url).netloc
    try:
        if "yupoo.com" in host:
            raw = parse_album_page(html, url)
        elif "weidian.com" in host:
            raw = parse_weidian_html(html, url)
        else:
            raw = parse_generic_product(html, url)
    except (ParserError, FetchError) as e:
        return {"ok": False, "error": str(e)}
    seller = db.get(Seller, seller_id) if seller_id else get_or_create_seller_for_url(db, url)
    own = client is None
    client = client or PoliteClient()
    try:
        res = _ingest_one(db, seller, raw, client, origin="bookmarklet")
        dedup.recompute_groups(db)
        db.commit()
        return {
            "ok": True,
            "product_id": res.product.id,
            "created": res.created,
            "changes": res.changes,
            "images": len(raw.image_urls),
        }
    finally:
        if own:
            client.close()


def scan_seller(
    db: Session,
    seller: Seller,
    *,
    mode: str = "incremental",
    progress: Progress | None = None,
    client: PoliteClient | None = None,
    job_id: int | None = None,
    max_products: int | None = None,
) -> ScanRun:
    """Scan one seller's catalogue (see ``scanner`` for the staged design)."""
    return scan_many(
        db, [seller], mode=mode, progress=progress, client=client, job_id=job_id, max_products=max_products
    )[0]


def check_watched_urls(db: Session, progress: Progress | None = None) -> dict:
    urls = db.query(WatchedUrl).filter(WatchedUrl.active.is_(True)).all()
    ok = fail = 0
    with PoliteClient() as client:
        for i, w in enumerate(urls):
            res = import_url(db, w.url, seller_id=w.seller_id, client=client)
            w = db.get(WatchedUrl, w.id)
            w.last_checked_at = utcnow()
            w.last_status = "ok" if res.get("ok") else ("blocked" if res.get("blocked") else "failed")
            w.last_message = res.get("error") or ("; ".join(res.get("changes") or []) or None)
            if res.get("product_id"):
                w.product_id = res["product_id"]
            db.commit()
            ok += bool(res.get("ok"))
            fail += not res.get("ok")
            if progress:
                progress(f"watched links {i + 1}/{len(urls)}", (i + 1) / max(1, len(urls)))
    return {"checked": len(urls), "ok": ok, "failed": fail}
