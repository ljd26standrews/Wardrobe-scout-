"""Seller scans and single-URL imports."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import Product, ScanRun, Seller, WatchedUrl, utcnow
from ..services import ai, dedup
from ..services.settings_store import get_setting
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

BLOCKED_HELP = (
    "The site restricted automated access ({reason}). Nothing was bypassed. Use the bookmarklet while "
    "browsing the catalogue yourself, paste individual links, or import a CSV/JSON export."
)


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
    """Traverse a seller catalogue. Incremental mode stops after a run of already-known albums."""
    s = get_settings()
    sched = get_setting(db, "schedule")
    max_products = max_products or sched.get("max_products_per_scan", 300)
    run = ScanRun(seller_id=seller.id, job_id=job_id, status="running")
    db.add(run)
    db.commit()
    adapter_cls = ADAPTERS.get(seller.platform)
    if adapter_cls is None or not seller.base_url:
        run.status, run.message = "failed", f"No automatic adapter for platform '{seller.platform}'. Use manual import."
        run.finished_at = utcnow()
        seller.last_scan_at, seller.last_scan_status, seller.last_scan_message = utcnow(), run.status, run.message
        db.commit()
        return run

    own = client is None
    client = client or PoliteClient()
    adapter = adapter_cls(client)
    known = {
        ext: (pid, title, last)
        for pid, ext, title, last in db.query(
            Product.id, Product.external_id, Product.title_original, Product.last_seen_at
        ).filter(Product.seller_id == seller.id)
    }
    seen_ids: set[str] = set()
    consecutive_known = 0
    refresh_after = timedelta(days=7)
    completed_traversal = False

    def say(msg: str, frac: float | None = None) -> None:
        if progress:
            progress(f"{seller.name}: {msg}", frac)

    try:
        for ref in adapter.iter_album_refs(seller.base_url, s.max_pages_per_scan, progress=lambda m: say(m)):
            run.pages_fetched = client.requests_made
            seen_ids.add(ref.external_id)
            run.products_seen += 1
            k = known.get(ref.external_id)
            if k is not None:
                pid, old_title, last_seen = k
                title_same = ref.title is None or (old_title or "").strip() == (ref.title or "").strip()
                if mode == "incremental" and title_same and last_seen and utcnow() - last_seen < refresh_after:
                    db.query(Product).filter(Product.id == pid).update(
                        {Product.last_seen_at: utcnow(), Product.status: "active"}
                    )
                    consecutive_known += 1
                    if consecutive_known >= 40:
                        say("reached previously-scanned albums; stopping incremental scan")
                        break
                    continue
            consecutive_known = 0
            try:
                raw = adapter.fetch_listing(ref)
                res = _ingest_one(db, seller, raw, client)
                if res.created:
                    run.products_new += 1
                elif res.changes:
                    run.products_updated += 1
                db.commit()
            except BlockedError:
                raise
            except FetchError as e:
                db.rollback()
                run.errors = (run.errors or []) + [f"{ref.url}: {e}"][:1]
                if len(run.errors) > 25:
                    raise FetchError("too many product-page errors; stopping") from e
            say(f"{run.products_seen} seen · {run.products_new} new · {run.products_updated} updated")
            if run.products_new + run.products_updated >= max_products:
                say(f"reached per-scan limit of {max_products} new/updated products")
                break
        else:
            completed_traversal = True

        # Products not seen in a complete full traversal are marked unavailable (never deleted).
        # "Complete" requires the adapter to confirm pagination ended - a page limit is not the end.
        exhausted = completed_traversal and getattr(adapter, "exhausted", False)
        if mode == "full" and completed_traversal and not exhausted:
            say("page limit reached before the end of the catalogue; removals were not checked")
        if mode == "full" and exhausted and seen_ids:
            for p in db.query(Product).filter(
                Product.seller_id == seller.id, Product.status == "active", Product.data_origin == "scraped"
            ):
                if p.external_id not in seen_ids:
                    p.status = "unavailable"
        run.status = "partial" if run.errors else "succeeded"
        run.message = f"{run.products_seen} albums seen, {run.products_new} new, {run.products_updated} updated"
    except BlockedError as e:
        db.rollback()
        run.status, run.message = "blocked", BLOCKED_HELP.format(reason=str(e))
    except ParserError as e:
        db.rollback()
        run.status, run.message = "failed", f"Page layout not recognised — the parser may need updating. {e}"
    except FetchError as e:
        db.rollback()
        run.status, run.message = "failed", str(e)
    finally:
        if own:
            client.close()
    run.finished_at = utcnow()
    run.pages_fetched = client.requests_made
    seller.last_scan_at, seller.last_scan_status, seller.last_scan_message = utcnow(), run.status, run.message
    dedup.recompute_groups(db)
    db.commit()
    log.info("scan_finished", seller=seller.slug, status=run.status, new=run.products_new, seen=run.products_seen)
    return run


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
