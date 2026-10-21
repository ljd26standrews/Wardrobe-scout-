"""Background job runner + scheduler.

Jobs run one at a time in a dedicated worker thread (ingestion is deliberately
serial to stay polite to seller sites). Each job is persisted with progress and
log lines so the UI can show status. APScheduler checks periodically which
active sellers are due for a refresh.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import get_settings
from ..db import SessionLocal, session_scope
from ..logging_setup import get_logger
from ..models import Job, Seller, utcnow
from .settings_store import get_setting

log = get_logger(__name__)

# Two serial queues: long-running catalogue work, and interactive jobs you are waiting on (importing a
# pasted link, an AI photo check). Without the split a pasted link could wait hours behind a scan.
_background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ws-bg")
_interactive = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ws-ui")
BACKGROUND_KINDS = {
    "scan_seller", "scan_all", "check_watched", "recompute", "dedup", "fetch_images", "analyse_pending", "reanalyse",
}  # fmt: skip
SCAN_KINDS = {"scan_seller", "scan_all", "analyse_pending"}
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _job_log(job_id: int, message: str, progress: float | None = None) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.message = message
        job.log = (job.log or [])[-199:] + [f"{utcnow().strftime('%H:%M:%S')} {message}"]
        if progress is not None:
            job.progress = max(0.0, min(1.0, progress))


def _run(job_id: int) -> None:
    from ..ingestion import scan
    from . import dedup

    with session_scope() as db:
        job = db.get(Job, job_id)
        if job.status == "cancelled":
            return
        job.status, job.started_at = "running", utcnow()
        kind, params = job.kind, dict(job.params or {})
    db = SessionLocal()
    status, result = "succeeded", {}
    try:
        progress = lambda m, f=None: _job_log(job_id, m, f)  # noqa: E731
        if kind == "scan_seller":
            seller = db.get(Seller, params["seller_id"])
            run = scan.scan_seller(db, seller, mode=params.get("mode", "incremental"), progress=progress, job_id=job_id)
            status = {"succeeded": "succeeded", "partial": "partial", "cancelled": "cancelled"}.get(
                run.status, "failed"
            )
            result = {
                "scan_run_id": run.id,
                "scan_status": run.status,
                "message": run.message,
                "new": run.products_new,
                "updated": run.products_updated,
            }
        elif kind == "scan_all":
            sellers = (
                db.query(Seller)
                .filter(Seller.status == "active", Seller.base_url.isnot(None))
                .order_by(Seller.priority)
                .all()
            )
            # One concurrent scan: each seller's catalogue host and the image hosts proceed in parallel.
            runs = scan.scan_many(db, sellers, mode=params.get("mode", "incremental"), progress=progress, job_id=job_id)
            summary = [
                {"seller": db.get(Seller, r.seller_id).name, "status": r.status, "new": r.products_new} for r in runs
            ]
            if all(r.status != "cancelled" for r in runs):
                watched = scan.check_watched_urls(db, progress)
            else:
                watched, status = {}, "cancelled"
            result = {"sellers": summary, "watched": watched}
            if status != "cancelled" and any(s["status"] in ("failed", "blocked") for s in summary):
                status = "partial"
        elif kind == "deep_product":
            result = deep_product(db, params["product_id"])
        elif kind == "analyse_pending":
            result = scan.drain_pending(db, progress=progress, job_id=job_id)
        elif kind == "reanalyse":
            result = reanalyse_stored(db, progress)
        elif kind == "import_url":
            result = scan.import_url(db, params["url"], seller_id=params.get("seller_id"))
            status = "succeeded" if result.get("ok") else "failed"
        elif kind == "fetch_images":
            from ..ingestion.http import PoliteClient
            from ..ingestion.pipeline import process_images
            from ..models import Product

            ids = params.get("product_ids", [])
            fetched = 0
            with PoliteClient() as client:
                for i, pid in enumerate(ids):
                    p = db.get(Product, pid)
                    if p is None:
                        continue
                    fetched += process_images(db, p, client)
                    dedup.match_product(db, p)
                    db.commit()
                    progress(f"images for {i + 1}/{len(ids)} products", (i + 1) / max(1, len(ids)))
            dedup.recompute_groups(db)
            db.commit()
            result = {"products": len(ids), "images_processed": fetched}
        elif kind == "check_watched":
            result = scan.check_watched_urls(db, progress)
        elif kind == "recompute":
            result = recompute_all(db, progress)
        elif kind == "dedup":
            result = dedup.match_all(db)
        elif kind == "community_search":
            from .community import search_reddit

            result = search_reddit(
                db, params["query"], seller_id=params.get("seller_id"), product_id=params.get("product_id")
            )
        elif kind == "ai_assess":
            from .research import ai_visual_assess

            result = ai_visual_assess(db, params["product_id"])
        else:
            raise ValueError(f"unknown job kind {kind}")
        status = outcome(status, result)
    except Exception as e:  # job failures are recorded, never crash the worker
        log.exception("job_failed", job_id=job_id, kind=kind)
        db.rollback()
        status, result = "failed", {"error": str(e)}
    finally:
        db.close()
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status, job.result, job.finished_at, job.progress = status, result, utcnow(), 1.0
        job.message = result.get("message") or result.get("error") or job.message
        job.control = None


def outcome(status: str, result: dict) -> str:
    """A job that returned an error is not a success: failed if nothing was done, partial if some was."""
    if status != "succeeded":
        return status
    if result.get("error") or result.get("ok") is False:
        return "failed"
    failed = result.get("errors") or result.get("failed")
    if failed:
        done = result.get("found") or result.get("ok") or 0
        return "partial" if done else "failed"
    return status


def deep_product(db, product_id: int) -> dict:
    """Full gallery + cross-seller matching for one product you showed interest in."""
    from ..ingestion import scan
    from ..ingestion.http import PoliteClient
    from ..ingestion.pipeline import process_images
    from ..models import Product
    from . import dedup

    p = db.get(Product, product_id)
    if p is None or p.data_origin != "scraped":
        return {"skipped": True}
    if p.detail_fetched_at is None:  # only seen in a catalogue listing so far
        res = scan.import_url(db, p.source_url, seller_id=p.seller_id)
        if not res.get("ok"):
            return res
        p = db.get(Product, product_id)
    else:
        with PoliteClient() as client:
            process_images(db, p, client, deep=True)
        dedup.match_product(db, p)
        dedup.recompute_groups(db)
    p.analysis_state = "deep"
    db.commit()
    return {"product_id": p.id, "photos": sum(1 for i in p.images if i.local_path)}


def reanalyse_stored(db, progress=None) -> dict:
    """One-off upgrade for photos stored before image roles existed: classify them (size charts, text
    graphics) and rebuild product vectors from the selected views. Local files only - no network.
    Resumable: only images without a role are processed."""
    from ..ingestion.pipeline import analyse_image_file, apply_analysis, finalize_product
    from ..models import Product, ProductImage
    from ..services.images import ImageError
    from . import dedup

    k = get_settings().analysis_images
    ids = [
        pid
        for (pid,) in db.query(ProductImage.product_id)
        .filter(ProductImage.role.is_(None), ProductImage.local_path.isnot(None))
        .distinct()
    ]
    for i, pid in enumerate(ids):
        p = db.get(Product, pid)
        for img in p.images:
            if img.role is None and img.local_path:
                try:
                    apply_analysis(img, analyse_image_file(img.local_path))
                except (ImageError, OSError) as e:
                    img.fetch_error = str(e)[:300]
                    img.role = "uncertain"
        finalize_product(db, p, k)
        if i % 25 == 0:
            db.commit()
            if progress:
                progress(f"re-analysed photos of {i + 1}/{len(ids)} products", (i + 1) / max(1, len(ids)))
    db.commit()
    res = dedup.match_all(db) if ids else {}
    return {"products": len(ids), **res}


def recompute_all(db, progress=None) -> dict:
    from PIL import Image

    from ..ingestion.pipeline import process_images
    from ..models import InspirationImage, Product, WardrobeItem
    from . import dedup
    from . import embeddings as emb
    from .images import resolve_path

    backend = emb.get_image_backend()
    products = db.query(Product).all()
    for i, p in enumerate(products):
        process_images(db, p, None)
        if i % 25 == 0:
            db.commit()
            if progress:
                progress(f"embeddings {i + 1}/{len(products)}", (i + 1) / max(1, len(products)) * 0.8)
    for item in list(db.query(InspirationImage)) + list(db.query(WardrobeItem)):
        path = getattr(item, "path", None) or getattr(item, "image_path", None)
        if path and resolve_path(path).exists():
            item.embedding = emb.to_bytes(backend.embed(Image.open(resolve_path(path)).convert("RGB")))
            item.embedding_backend = backend.name
    db.commit()
    res = dedup.match_all(db)
    return {"products": len(products), "backend": backend.name, **res}


def enqueue(kind: str, params: dict | None = None) -> int:
    with session_scope() as db:
        # Avoid duplicate queued jobs of the same kind+params
        existing = db.query(Job).filter(Job.kind == kind, Job.status.in_(["queued", "running"])).all()
        for j in existing:
            if (j.params or {}) == (params or {}):
                return j.id
        job = Job(kind=kind, params=params or {}, status="queued", message="queued")
        db.add(job)
        db.flush()
        job_id = job.id
    (_background if kind in BACKGROUND_KINDS else _interactive).submit(_run, job_id)
    return job_id


def set_control(job_id: int, action: str) -> dict:
    """pause | resume | cancel a running or queued job (scans honour these within about a second)."""
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status not in ("queued", "running"):
            return {"ok": False, "status": job.status}
        job.control = None if action == "resume" else action
        if action == "cancel" and job.status == "queued":
            job.status, job.finished_at, job.message = "cancelled", utcnow(), "cancelled before it started"
        return {"ok": True, "status": job.status, "control": job.control}


def retry(job_id: int) -> int:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        kind, params = job.kind, dict(job.params or {})
    return enqueue(kind, params)


def run_sync(kind: str, params: dict | None = None) -> int:
    """Run a job in the calling thread (used by CLI scripts and tests)."""
    with session_scope() as db:
        job = Job(kind=kind, params=params or {}, status="queued")
        db.add(job)
        db.flush()
        job_id = job.id
    _run(job_id)
    return job_id


def _scheduled_tick() -> None:
    with session_scope() as db:
        sched = get_setting(db, "schedule")
        if not sched.get("enabled"):
            return
        due = []
        for s in db.query(Seller).filter(Seller.status == "active", Seller.base_url.isnot(None)):
            interval = timedelta(hours=s.scan_interval_hours or sched.get("default_interval_hours", 24))
            if s.platform in ("yupoo", "shopify", "website") and (
                s.last_scan_at is None or utcnow() - s.last_scan_at >= interval
            ):
                due.append(s.id)
    for sid in due:
        enqueue("scan_seller", {"seller_id": sid, "mode": "incremental"})


def start_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return
        with session_scope() as db:
            minutes = get_setting(db, "schedule").get("check_every_minutes", 60)
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(
            _scheduled_tick,
            "interval",
            minutes=minutes,
            id="seller_refresh",
            next_run_time=utcnow() + timedelta(minutes=2),
        )
        _scheduler.start()
        log.info("scheduler_started", every_minutes=minutes)


def stop_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


def mark_interrupted_jobs() -> None:
    """Jobs left 'running'/'queued' by a previous process are marked interrupted on startup. Scans resume
    on their own: products they left discovered/partial are picked up by the next scan or by
    'Continue analysis'."""
    with session_scope() as db:
        for j in db.query(Job).filter(Job.status.in_(["queued", "running"])):
            j.status, j.message, j.finished_at = "failed", "interrupted by restart", utcnow()
            j.control = None


def enqueue_upgrade_jobs() -> None:
    """After an upgrade, classify photos stored by the previous pipeline (once, in the background)."""
    from ..models import ProductImage

    with session_scope() as db:
        pending = (
            db.query(ProductImage.id).filter(ProductImage.role.is_(None), ProductImage.local_path.isnot(None)).first()
        )
    if pending:
        enqueue("reanalyse")
