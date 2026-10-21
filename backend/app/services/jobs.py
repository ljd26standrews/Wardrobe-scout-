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

from ..db import SessionLocal, session_scope
from ..logging_setup import get_logger
from ..models import Job, Seller, utcnow
from .settings_store import get_setting

log = get_logger(__name__)

# Two serial queues: long-running catalogue work, and interactive jobs you are waiting on (importing a
# pasted link, an AI photo check). Without the split a pasted link could wait hours behind a scan.
_background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ws-bg")
_interactive = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ws-ui")
BACKGROUND_KINDS = {"scan_seller", "scan_all", "check_watched", "recompute", "dedup", "fetch_images"}
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
        job.status, job.started_at = "running", utcnow()
        kind, params = job.kind, dict(job.params or {})
    db = SessionLocal()
    status, result = "succeeded", {}
    try:
        progress = lambda m, f=None: _job_log(job_id, m, f)  # noqa: E731
        if kind == "scan_seller":
            seller = db.get(Seller, params["seller_id"])
            run = scan.scan_seller(db, seller, mode=params.get("mode", "incremental"), progress=progress, job_id=job_id)
            status = {"succeeded": "succeeded", "partial": "partial"}.get(run.status, "failed")
            result = {
                "scan_run_id": run.id,
                "scan_status": run.status,
                "message": run.message,
                "new": run.products_new,
                "updated": run.products_updated,
            }
        elif kind == "scan_all":
            ids = [
                s.id
                for s in db.query(Seller)
                .filter(Seller.status == "active", Seller.base_url.isnot(None))
                .order_by(Seller.priority)
            ]
            summary = []
            for i, sid in enumerate(ids):
                seller = db.get(Seller, sid)
                run = scan.scan_seller(
                    db, seller, mode=params.get("mode", "incremental"), progress=progress, job_id=job_id
                )
                summary.append({"seller": seller.name, "status": run.status, "new": run.products_new})
                _job_log(job_id, f"finished {seller.name}: {run.status}", (i + 1) / max(1, len(ids)))
            watched = scan.check_watched_urls(db, progress)
            result = {"sellers": summary, "watched": watched}
            if any(s["status"] in ("failed", "blocked") for s in summary):
                status = "partial"
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
    """Jobs left 'running'/'queued' by a previous process are marked failed on startup."""
    with session_scope() as db:
        for j in db.query(Job).filter(Job.status.in_(["queued", "running"])):
            j.status, j.message, j.finished_at = "failed", "interrupted by restart", utcnow()
