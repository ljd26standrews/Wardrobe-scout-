"""Progressive, concurrent catalogue scanning.

Stage A - discovery. Catalogue listing pages are read first (one request per ~40 products). Every product
          seen becomes browsable at once with what the listing shows: title, price, category, cover photo.
Stage B - prioritisation. Discovered products are ordered by predicted relevance to your taste (with a
          share of exploration picks) so the products you are most likely to want are analysed first.
          Nothing is skipped: every product is analysed eventually.
Stage C - analysis. Product pages and photos are fetched with the progressive photo policy
          (``pipeline.next_image``), photos are classified (size charts and banners excluded), the
          aesthetic vector is built and duplicates are matched incrementally. Duplicate candidates and
          products you saved get a deep pass with more photos.

Network work runs in worker threads, at most one request in flight per host and never faster than the
politeness delay; different hosts (each seller's catalogue, the image CDN) proceed in parallel. All database
work happens on the scanning thread. Repeat scans re-read the whole listing (cheap, and safe for sellers who
do not publish chronologically) but only fetch pages and photos that are new, changed, stale or unanalysed.
Progress, pause/resume and cancel go through the Job row, so they survive page reloads.
"""

from __future__ import annotations

import heapq
import itertools
import queue
import random
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import Job, Product, ProductImage, ProductObservation, ProductRelation, SavedItem, ScanRun, Seller, utcnow
from ..services import ai, dedup
from ..services import embeddings as emb
from ..services import image_select as sel
from ..services.images import ImageError
from ..services.settings_store import get_setting
from . import pipeline
from .base import AlbumRef, SellerAdapter
from .http import BlockedError, FetchError, PoliteClient
from .yupoo import ParserError

log = get_logger(__name__)

BLOCKED_HELP = (
    "The site restricted automated access ({reason}). Nothing was bypassed. Use the bookmarklet while "
    "browsing the catalogue yourself, paste individual links, or import a CSV/JSON export."
)

# Task priorities (lower runs sooner). Fractions within a class encode predicted relevance.
P_NEW, P_IMAGE, P_CHANGED, P_RESUME, P_STALE, P_VERIFY, P_DEEP = 1.0, 1.5, 2.0, 3.0, 5.0, 6.0, 7.0
MAX_ERRORS_PER_SELLER = 25
INFLIGHT_PER_HOST = 2  # one request active, one waiting so the host's slot never idles
REPEAT_MIN_PRODUCTS = 3  # the same graphic in >= 3 of a seller's listings is boilerplate
REPEAT_MIN_PRODUCTS_GARMENT_LOOKING = 5
MAX_VERIFY = 200
MAX_DEEP = 300

EMPTY_HELP = (
    "No albums were found on the first catalogue page. The catalogue may be empty, restricted or laid out "
    "differently; nothing was marked as removed."
)


class Cancelled(Exception):
    pass


@dataclass(order=True)
class Task:
    priority: float
    seq: int
    kind: str = field(compare=False)  # detail | image
    host: str = field(compare=False)
    seller_id: int = field(compare=False)
    product_id: int | None = field(compare=False, default=None)
    image_id: int | None = field(compare=False, default=None)
    url: str = field(compare=False, default="")
    referer: str | None = field(compare=False, default=None)
    ref: AlbumRef | None = field(compare=False, default=None)
    purpose: str = field(compare=False, default="analyse")  # analyse | refresh | verify | deep
    etag: str | None = field(compare=False, default=None)
    fallback: str | None = field(compare=False, default=None)  # original image URL if the variant fails
    last_modified: str | None = field(compare=False, default=None)


@dataclass
class SellerState:
    seller: Seller
    adapter: SellerAdapter
    run: ScanRun
    host: str
    seen: set[str] = field(default_factory=set)
    discovery_done: bool = False
    exhausted: bool = False
    status: str | None = None  # blocked | failed when the seller had to stop
    message: str | None = None
    errors: list[str] = field(default_factory=list)
    stale: list[tuple] = field(default_factory=list)
    new_ids: list[int] = field(default_factory=list)
    repeat_idx: dedup.PhashIndex = field(default_factory=dedup.PhashIndex)
    counts: dict = field(
        default_factory=lambda: {"discovered": 0, "new": 0, "changed": 0, "unchanged": 0, "notices": 0}
    )


class Scanner:
    def __init__(
        self,
        db: Session,
        sellers: list[Seller],
        *,
        mode: str = "incremental",
        client: PoliteClient | None = None,
        progress=None,
        job_id: int | None = None,
        max_products: int | None = None,
        discover: bool = True,
    ) -> None:
        from .scan import ADAPTERS

        self.db, self.mode, self.progress, self.job_id = db, mode, progress, job_id
        self.s = get_settings()
        self.own_client = client is None
        self.client = client or PoliteClient()
        self.discover = discover
        sched = get_setting(db, "schedule")
        self.max_new = max_products or sched.get("max_products_per_scan", 5000)
        self.k = self.s.analysis_images
        self.backend = emb.get_image_backend().name
        self.use_ai = ai.is_enabled("translate")
        self.states: dict[int, SellerState] = {}
        self.unsupported: list[ScanRun] = []
        for seller in sellers:
            run = ScanRun(seller_id=seller.id, job_id=job_id, status="running", mode=mode)
            db.add(run)
            cls = ADAPTERS.get(seller.platform)
            if cls is None or not seller.base_url:
                run.status = "failed"
                run.message = f"No automatic adapter for platform '{seller.platform}'. Use manual import."
                self.unsupported.append(run)
                continue
            self.states[seller.id] = SellerState(seller, cls(self.client), run, urlparse(seller.base_url).netloc)
        db.commit()
        self.queues: dict[str, list[Task]] = {}
        self.seq = itertools.count()
        self.inflight: dict[Future, Task] = {}
        self.inflight_images: set[int] = set()
        self.queued_images: set[int] = set()
        self.queued_details: set[int] = set()
        self.blocked_hosts: set[str] = set()
        self.relevance: dict[int, float] = {}
        self.deep_ids: set[int] = set()
        self.new_queued = 0
        self.analysed_ids: set[int] = set()
        self.discovery_q: queue.Queue = queue.Queue()
        self.paused = threading.Event()
        self.cancelled = threading.Event()
        self.ctx = dedup.MatchContext(db)
        self.t0 = time.monotonic()
        self.done_times: deque[float] = deque(maxlen=4000)
        self.stats = {
            "stage": "starting", "discovered": 0, "new": 0, "changed": 0, "unchanged_skipped": 0,
            "details_fetched": 0, "details_not_modified": 0, "images_downloaded": 0, "image_bytes": 0,
            "images_reused": 0, "size_charts_excluded": 0, "text_graphics_excluded": 0, "repeated_excluded": 0,
            "products_analysed": 0, "duplicates_found": 0, "similar_found": 0, "deep_analysed": 0,
            "removed": 0, "verified_present": 0, "errors": 0, "error_samples": [],
        }  # fmt: skip
        self._last_flush = 0.0
        self._last_control = 0.0
        self._images_per_product: list[int] = []
        self._log: list[str] = []

    # ------------------------------------------------------------------ helpers
    def say(self, msg: str, frac: float | None = None) -> None:
        if self.job_id:
            # Written with the next flush through this session: a second connection writing the job row
            # would compete with the scan for SQLite's single write lock.
            self._log.append(f"{utcnow().strftime('%H:%M:%S')} {msg}")
        elif self.progress:
            self.progress(msg, frac)

    def push(self, task: Task) -> None:
        if task.host in self.blocked_hosts:
            return
        heapq.heappush(self.queues.setdefault(task.host, []), task)
        if task.kind == "image" and task.image_id:
            self.queued_images.add(task.image_id)
        if task.kind == "detail" and task.product_id:
            self.queued_details.add(task.product_id)

    def rel(self, pid: int | None) -> float:
        return self.relevance.get(pid, 0.5) if pid else 0.5

    def error(self, st: SellerState | None, msg: str) -> None:
        self.stats["errors"] += 1
        if len(self.stats["error_samples"]) < 20:
            self.stats["error_samples"].append(msg[:300])
        if st is not None:
            st.errors.append(msg[:300])

    # ------------------------------------------------------------------ stage A: discovery
    def _discover_thread(self, st: SellerState) -> None:
        def page_progress(msg: str) -> None:
            while self.paused.is_set() and not self.cancelled.is_set():
                time.sleep(0.2)
            if self.cancelled.is_set():
                raise Cancelled()
            self.discovery_q.put(("page", st.seller.id, msg))

        try:
            for ref in st.adapter.iter_album_refs(
                st.seller.base_url, self.s.max_pages_per_scan, progress=page_progress
            ):
                self.discovery_q.put(("ref", st.seller.id, ref))
            self.discovery_q.put(("done", st.seller.id, None))
        except Exception as e:  # noqa: BLE001 - reported per seller
            self.discovery_q.put(("done", st.seller.id, e))

    def _on_ref(self, st: SellerState, ref: AlbumRef) -> None:
        if ref.external_id in st.seen:
            return
        st.seen.add(ref.external_id)
        st.run.products_seen += 1
        st.counts["discovered"] += 1
        self.stats["discovered"] += 1
        if ref.notice:
            self._skip_notice(st, ref)
            return
        cached = getattr(st.adapter, "cached_listing", lambda r: None)(ref)
        if cached is not None:  # the catalogue feed already contained the whole product (Shopify)
            res = pipeline.upsert_listing(self.db, st.seller, cached, use_ai=self.use_ai)
            self._count_detail(st, res)
            self._after_detail(st, res.product)
            return
        if not ref.title and not ref.cover_url:
            # Nothing to show yet (e.g. a sitemap URL): the product page creates the record.
            self.push(Task(P_NEW, next(self.seq), "detail", st.host, st.seller.id, ref=ref, url=ref.url))
            return
        now = utcnow()
        p, created, changed = pipeline.upsert_discovered(self.db, st.seller, ref, now)
        if created:
            self.db.flush()
            st.counts["new"] += 1
            self.stats["new"] += 1
            st.run.products_new += 1
            st.new_ids.append(p.id)
            self._queue_detail(st, p, ref, P_NEW, "analyse")
            if p.images:  # analyse the cover photo while the product page waits for its host
                self._queue_image(st, p, p.images[0], P_IMAGE)
            return
        if changed:
            st.counts["changed"] += 1
            self.stats["changed"] += 1
            self._queue_detail(st, p, ref, P_CHANGED, "refresh")
        elif self.mode == "full":
            self._queue_detail(st, p, ref, P_STALE, "refresh")
        elif p.detail_fetched_at is None:
            self._queue_detail(st, p, ref, P_RESUME, "analyse")
        else:
            st.counts["unchanged"] += 1
            self.stats["unchanged_skipped"] += 1
            if p.detail_fetched_at < now - timedelta(days=self.s.refresh_days):
                st.stale.append((p.detail_fetched_at, p.id, ref))
            if p.analysed_at is None or p.analysis_state in ("discovered", "partial"):
                self._continue_images(st, p, P_RESUME)

    def _skip_notice(self, st: SellerState, ref: AlbumRef) -> None:
        """A shop notice (contact details, ordering guide) is not a product; one recorded earlier is set aside."""
        st.counts["notices"] += 1
        p = (
            self.db.query(Product)
            .filter(Product.seller_id == st.seller.id, Product.external_id == ref.external_id)
            .one_or_none()
        )
        if p is not None and p.status == "active" and p.data_origin == "scraped":
            p.status = "notice"
            self.db.add(ProductObservation(product=p, observed_at=utcnow(), price=p.price, sizes=p.sizes,
                                           status="notice", source_url=p.source_url,
                                           change="identified as a shop notice, not a product"))  # fmt: skip

    def _discovery_finished(self, st: SellerState, err: Exception | None) -> None:
        st.discovery_done = True
        st.run.pages_fetched = st.adapter.pages_read
        if isinstance(err, Cancelled):
            return
        if isinstance(err, BlockedError):
            st.status, st.message = "blocked", BLOCKED_HELP.format(reason=str(err))
        elif isinstance(err, ParserError):
            st.status, st.message = "invalid", f"Page layout not recognised — the parser may need updating. {err}"
        elif isinstance(err, FetchError):
            st.status, st.message = "failed", str(err)
        elif err is not None:
            st.status, st.message = "failed", f"{type(err).__name__}: {err}"
            log.exception("discovery_failed", seller=st.seller.slug, exc_info=err)
        st.exhausted = err is None and bool(getattr(st.adapter, "exhausted", False))
        if err is None and getattr(st.adapter, "empty", False):
            st.status, st.message = "empty", EMPTY_HELP
        if st.status:
            self._drop_seller(st)
            return
        # Stale pages: refresh the oldest share each scan so the load is spread across scans.
        cap = max(50, len(st.seen) // 10)
        for i, (_, pid, ref) in enumerate(sorted(st.stale, key=lambda t: t[0])[:cap]):
            p = self.db.get(Product, pid)
            if p is not None:
                self._queue_detail(st, p, ref, P_STALE + i / max(1, cap), "refresh")
        self._prioritise(st)
        if st.exhausted:
            self._queue_removal_checks(st)
        elif self.mode == "full":
            self.say(f"{st.seller.name}: page limit reached before the end of the catalogue; removals not checked")

    # ------------------------------------------------------------------ stage B: prioritisation
    def _prioritise(self, st: SellerState) -> None:
        """Order this seller's queued work by predicted relevance (exploration interleaved)."""
        ids = st.new_ids
        if len(ids) < 2:
            return
        try:
            from ..services.recommender import rank_products

            prods = self.db.query(Product).filter(Product.id.in_(ids)).all()
            ranked = rank_products(self.db, prods)
            order = [r.product.id for r in sorted(ranked, key=lambda r: -r.score)]
        except Exception as e:  # noqa: BLE001 - ordering is an optimisation, never a failure
            log.info("prioritise_failed", error=str(e)[:200])
            return
        rng = random.Random(st.seller.id)
        n = len(order)
        for i, pid in enumerate(order):
            # Every 5th slot goes to a random product so low-ranked items keep making progress.
            self.relevance[pid] = rng.random() if i % 5 == 4 else i / n
        for q in self.queues.values():
            for t in q:
                if t.product_id in self.relevance and t.priority < P_STALE:
                    t.priority = int(t.priority * 2) / 2 + 0.49 * self.relevance[t.product_id]
            heapq.heapify(q)

    # ------------------------------------------------------------------ queueing
    def _queue_detail(self, st: SellerState, p: Product, ref: AlbumRef, prio: float, purpose: str) -> None:
        if p.id in self.queued_details:
            return
        if purpose == "analyse" and prio == P_NEW:
            if self.new_queued >= self.max_new:
                return  # stays discovered (browsable); continued by the next scan
            self.new_queued += 1
        self.push(Task(prio + 0.49 * self.rel(p.id), next(self.seq), "detail", st.host, st.seller.id, p.id,
                       url=ref.url, ref=ref, purpose=purpose, etag=p.etag if purpose == "refresh" else None,
                       last_modified=p.last_modified if purpose == "refresh" else None))  # fmt: skip

    def _queue_image(
        self, st: SellerState, p: Product, img: ProductImage, prio: float, purpose: str = "analyse"
    ) -> None:
        if img.id is None:
            self.db.flush()
        if img.id in self.inflight_images or img.id in self.queued_images:
            return
        if not img.local_path and not self.s.download_images:
            return  # photo downloads are switched off; the product finishes from what is stored
        if img.local_path:  # already stored (uploaded / legacy): analyse locally, no request
            try:
                pipeline.apply_analysis(img, pipeline.analyse_image_file(img.local_path))
            except (ImageError, OSError) as e:
                img.fetch_status, img.fetch_error = "failed", str(e)[:300]
            self._after_image(st, p, img, purpose)
            return
        other = (
            self.db.query(ProductImage)
            .filter(
                ProductImage.url == img.url,
                ProductImage.id != img.id,
                ProductImage.embedding.isnot(None),
                ProductImage.embedding_backend == self.backend,
            )  # fmt: skip
            .first()
        )
        if other is not None:  # the same photo was analysed for another listing: reuse, no download
            pipeline.copy_analysis(img, other)
            self.stats["images_reused"] += 1
            self._after_image(st, p, img, purpose)
            return
        url = img.thumb_url or img.url
        self.push(Task(prio + 0.49 * self.rel(p.id), next(self.seq), "image", urlparse(url).netloc, st.seller.id,
                       p.id, img.id, url=url, referer=img.referer, purpose=purpose, fallback=img.url))  # fmt: skip

    def _continue_images(self, st: SellerState, p: Product, prio: float, purpose: str = "analyse") -> None:
        """Queue the next photo the progressive policy wants, or finish the product."""
        deep = purpose == "deep"
        k = self.s.deep_images if deep else self.k
        budget = self.s.deep_images if deep else self.s.max_examine_images
        img = pipeline.next_image(p, k, budget, self.backend)
        if img is not None and not img.local_path and (not self.s.download_images or img.url.startswith("upload:")):
            img = None  # photo downloads are switched off: analyse what is stored, fetch nothing
        if img is not None and img.id in self.inflight_images | self.queued_images:
            return  # wait for it
        if img is not None and (p.detail_fetched_at is not None or img.position == 0 or deep):
            self._queue_image(st, p, img, prio, purpose)
            return
        if img is not None:
            return  # beyond the cover we need the product page's gallery first
        self._finish_product(st, p, deep)

    def _finish_product(self, st: SellerState, p: Product, deep: bool = False) -> None:
        pipeline.finalize_product(self.db, p, self.k)
        if deep:
            p.analysis_state = "deep" if p.detail_fetched_at else p.analysis_state
            self.stats["deep_analysed"] += 1
        if p.analysis_state in ("analysed", "deep") and p.id not in self.analysed_ids:
            self.analysed_ids.add(p.id)
            self.stats["products_analysed"] = len(self.analysed_ids)
            self.done_times.append(time.monotonic())
            self._images_per_product.append(sum(1 for i in p.images if i.fetch_status in ("ok", "failed")))
        if p.image_embedding is not None or p.links:
            before = self.stats["duplicates_found"]
            self.db.flush()
            self.ctx.match(p)
            strong = (
                self.db.query(ProductRelation)
                .filter((ProductRelation.a_id == p.id) | (ProductRelation.b_id == p.id))
                .filter(ProductRelation.relation.in_(dedup.STRONG_RELATIONS))
                .count()
            )
            weak = (
                self.db.query(ProductRelation)
                .filter((ProductRelation.a_id == p.id) | (ProductRelation.b_id == p.id))
                .filter(ProductRelation.relation == "similar_photo")
                .count()
            )
            self.stats["duplicates_found"] = before + strong
            self.stats["similar_found"] += weak
            if not deep and p.id not in self.deep_ids and len(self.deep_ids) < MAX_DEEP:
                self._queue_deep_pair(p)

    def _queue_deep_pair(self, p: Product) -> None:
        """Duplicate candidates - one shared photo, or a near-identical look at another seller - are only
        hints: fetch more photos of both listings so photo matching can confirm or reject them."""
        rels = (
            self.db.query(ProductRelation)
            .filter((ProductRelation.a_id == p.id) | (ProductRelation.b_id == p.id))
            .filter(ProductRelation.relation.in_(("similar_photo", "visually_similar")))
            .all()
        )
        for r in rels:
            if r.relation == "visually_similar":
                a, b = self.db.get(Product, r.a_id), self.db.get(Product, r.b_id)
                if a is None or b is None or a.seller_id == b.seller_id:
                    continue  # same-seller look-alikes are usually a range, not a re-post
            for pid in (r.a_id, r.b_id):
                if pid in self.deep_ids or len(self.deep_ids) >= MAX_DEEP:
                    continue
                q = self.db.get(Product, pid)
                st = self.states.get(q.seller_id) if q else None
                if q is None or st is None or q.detail_fetched_at is None:
                    continue
                self.deep_ids.add(pid)
                self._continue_images(st, q, P_DEEP, "deep")

    def _queue_removal_checks(self, st: SellerState) -> None:
        """Products missing from a complete listing: confirm with their own page before retiring them."""
        missing = (
            self.db.query(Product)
            .filter(Product.seller_id == st.seller.id, Product.status == "active", Product.data_origin == "scraped")
            .all()
        )
        missing = [p for p in missing if p.external_id not in st.seen]
        for p in missing[:MAX_VERIFY]:
            ref = AlbumRef(external_id=p.external_id, url=p.source_url, title=p.title_original)
            self.push(Task(P_VERIFY, next(self.seq), "detail", st.host, st.seller.id, p.id, url=p.source_url,
                           ref=ref, purpose="verify"))  # fmt: skip
        if len(missing) > MAX_VERIFY:
            self.say(f"{st.seller.name}: {len(missing) - MAX_VERIFY} more missing products will be checked next scan")

    def _queue_saved_for_deep(self) -> None:
        saved = {pid for (pid,) in self.db.query(SavedItem.product_id)}
        for pid in saved:
            p = self.db.get(Product, pid)
            st = self.states.get(p.seller_id) if p else None
            if p is None or st is None or pid in self.deep_ids or p.analysis_state == "deep" or not p.detail_fetched_at:
                continue
            if len(self.deep_ids) >= MAX_DEEP:
                break
            self.deep_ids.add(pid)
            self._continue_images(st, p, P_DEEP, "deep")

    def _resume_pending(self, st: SellerState) -> None:
        """Products left discovered/partial by an earlier, interrupted or capped scan."""
        for p in self.db.query(Product).filter(
            Product.seller_id == st.seller.id,
            Product.status == "active",
            Product.analysis_state.in_(("discovered", "partial")),
        ):
            if p.detail_fetched_at is None:
                ref = AlbumRef(external_id=p.external_id, url=p.source_url, title=p.title_original)
                self._queue_detail(st, p, ref, P_RESUME, "analyse")
            else:
                self._continue_images(st, p, P_RESUME)

    # ------------------------------------------------------------------ workers (no database access)
    def _work(self, task: Task, st: SellerState):
        if task.kind == "detail":
            fetch_c = getattr(st.adapter, "fetch_listing_conditional", None)
            if fetch_c is not None and task.ref is not None:
                return fetch_c(task.ref, task.etag, task.last_modified)
            return st.adapter.fetch_listing(task.ref or task.url), {}
        return pipeline.fetch_and_analyse(self.client, task.url, task.referer, fallback=task.fallback)

    # ------------------------------------------------------------------ results (scanning thread)
    def _count_detail(self, st: SellerState, res) -> None:
        self.stats["details_fetched"] += 1
        if not res.created and res.changes:
            st.run.products_updated += 1

    def _on_detail(self, task: Task, st: SellerState, result) -> None:
        raw, validators = result
        p = self.db.get(Product, task.product_id) if task.product_id else None
        if raw is None:  # 304 Not Modified
            self.stats["details_not_modified"] += 1
            if p is not None:
                p.detail_fetched_at = p.last_seen_at = utcnow()
                pipeline.update_state(p)
                if task.purpose == "verify":
                    self.stats["verified_present"] += 1
            return
        if task.purpose == "verify":
            self.stats["verified_present"] += 1
        created_here = p is None
        first_read = p is not None and p.detail_fetched_at is None
        res = pipeline.upsert_listing(self.db, st.seller, raw, use_ai=self.use_ai)
        p = res.product
        if created_here and res.created:
            st.run.products_new += 1
            self.stats["new"] += 1
        self.stats["details_fetched"] += 1
        if not res.created and res.changes and not first_read:
            st.run.products_updated += 1
        if validators.get("etag") or validators.get("last_modified"):
            p.etag, p.last_modified = validators.get("etag"), validators.get("last_modified")
        self._after_detail(st, p)

    def _after_detail(self, st: SellerState, p: Product) -> None:
        self.db.flush()
        if p.analysed_at is None or any(i.embedding is None and i.fetch_status == "pending" for i in p.images[:1]):
            self._continue_images(st, p, P_IMAGE)
        elif p.image_embedding is None and p.images:
            self._continue_images(st, p, P_IMAGE)
        else:
            pipeline.finalize_product(self.db, p, self.k)

    def _on_detail_error(self, task: Task, st: SellerState, e: Exception) -> None:
        if isinstance(e, BlockedError):
            st.status, st.message = "blocked", BLOCKED_HELP.format(reason=str(e))
            self._drop_seller(st)
            return
        if task.purpose == "verify" and ("HTTP 404" in str(e) or "HTTP 410" in str(e)):
            p = self.db.get(Product, task.product_id)
            if p is not None and p.status == "active":
                p.status = "unavailable"
                self.db.add(ProductObservation(product=p, observed_at=utcnow(), price=p.price, sizes=p.sizes,
                                               status="unavailable", source_url=p.source_url,
                                               change="removed from the catalogue"))  # fmt: skip
                self.stats["removed"] += 1
            return
        self.error(st, f"{task.url}: {e}")
        if len(st.errors) > MAX_ERRORS_PER_SELLER:
            st.status, st.message = "failed", "too many product-page errors; stopping this seller"
            self._drop_seller(st)

    def _on_image(self, task: Task, st: SellerState, a: pipeline.ImageAnalysis) -> None:
        img = self.db.get(ProductImage, task.image_id)
        if img is None:
            return
        pipeline.apply_analysis(img, a)
        self.stats["images_downloaded"] += 1
        self.stats["image_bytes"] += a.nbytes
        self._check_repeated(st, img)
        if img.role == sel.SIZE_CHART:
            self.stats["size_charts_excluded"] += 1
        elif img.role == sel.TEXT_GRAPHIC:
            self.stats["text_graphics_excluded"] += 1
        self._after_image(st, img.product, img, task.purpose)

    def _after_image(self, st: SellerState, p: Product, img: ProductImage, purpose: str) -> None:
        self._continue_images(st, p, P_DEEP if purpose == "deep" else P_IMAGE, purpose)

    def _on_image_error(self, task: Task, st: SellerState, e: Exception) -> None:
        img = self.db.get(ProductImage, task.image_id)
        if isinstance(e, BlockedError):
            self.blocked_hosts.add(task.host)
            self.queues.pop(task.host, None)
            self.error(st, f"image host {task.host} restricted access: {e}")
            return
        if img is None:
            return
        img.fetch_status, img.fetch_error = "failed", str(e)[:300]
        self.error(None, f"image {task.url}: {e}")
        self._after_image(st, img.product, img, task.purpose)

    def _check_repeated(self, st: SellerState, img: ProductImage) -> None:
        """Banners, notices and size charts a seller reuses across listings carry no product information."""
        if not img.phash:
            return
        hits = st.repeat_idx.query(img.phash, max_dist=4)
        others = {pid for pid in hits if pid != img.product_id}
        st.repeat_idx.add(img.product_id, img.phash, img.id)
        n = len(others) + 1
        garment_like = img.role in (sel.GARMENT, sel.DETAIL)
        if n < REPEAT_MIN_PRODUCTS or (garment_like and n < REPEAT_MIN_PRODUCTS_GARMENT_LOOKING):
            return
        ids = {img.id} | {iid for pid in others for _, iid in hits[pid]}
        for other in self.db.query(ProductImage).filter(ProductImage.id.in_(ids)):
            if other.role != sel.REPEATED:
                other.role = sel.REPEATED
                self.stats["repeated_excluded"] += 1
                if other.selected and other.product_id != img.product_id:
                    q = other.product
                    q.analysed_at = None  # its vector used boilerplate: rebuild it
                    self._continue_images(st, q, P_IMAGE)

    def _drop_seller(self, st: SellerState) -> None:
        for host, q in self.queues.items():
            self.queues[host] = [t for t in q if t.seller_id != st.seller.id]
            heapq.heapify(self.queues[host])

    # ------------------------------------------------------------------ control, stats
    def _check_control(self) -> None:
        if not self.job_id or time.monotonic() - self._last_control < 1.0:
            return
        self._last_control = time.monotonic()
        control = self.db.query(Job.control).filter(Job.id == self.job_id).scalar()
        if control == "cancel":
            self.cancelled.set()
        if control == "pause":
            self.paused.set()
        elif self.paused.is_set():
            self.paused.clear()

    def _eta(self) -> list[float] | None:
        if not all(st.discovery_done for st in self.states.values()):
            return None
        per_host = []
        avg_imgs = (sum(self._images_per_product) / len(self._images_per_product)) if self._images_per_product else 1.2
        stats = self.client.stats()
        for host, q in self.queues.items():
            n = len(q)
            if any(t.kind == "image" for t in q):
                # products still waiting for their first photo may need more than one
                n = int(n * max(1.0, avg_imgs))
            delay = stats.get(host, {}).get("delay", self.client.delay)
            per_host.append(n * max(delay, 0.05))
        if not per_host:
            return [0.0, 0.0]
        eta = max(per_host)
        return [round(eta * 0.85), round(eta * 1.35 + 5)]

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < 1.0:
            return
        self._last_flush = now
        recent = [t for t in self.done_times if now - t <= 60]
        elapsed = now - self.t0
        self.stats.update(
            elapsed_s=round(elapsed),
            pending_details=sum(1 for q in self.queues.values() for t in q if t.kind == "detail"),
            pending_images=sum(1 for q in self.queues.values() for t in q if t.kind == "image"),
            # products (not tasks) still waiting for a page or photo
            pending_products=len(
                {t.product_id or id(t) for q in self.queues.values() for t in q}
                | {t.product_id or id(t) for t in self.inflight.values()}
            ),
            in_flight=len(self.inflight),
            requests=self.client.requests_made,
            hosts=self.client.stats(),
            throughput_per_min=round(len(recent) * 60 / min(60, max(elapsed, 1)), 1),
            eta_seconds=self._eta(),
            discovery_complete=all(st.discovery_done for st in self.states.values()),
            sellers=[
                {
                    "id": st.seller.id, "name": st.seller.name, "pages": st.adapter.pages_read,
                    "total_pages": st.adapter.total_pages, "discovery_done": st.discovery_done,
                    "catalogue_complete": st.exhausted, "status": st.status, **st.counts,
                }
                for st in self.states.values()
            ],
        )  # fmt: skip
        for st in self.states.values():
            st.run.stats = dict(st.counts, pages=st.adapter.pages_read, total_pages=st.adapter.total_pages)
            st.run.pages_fetched = st.adapter.pages_read
        if self.job_id:
            job = self.db.get(Job, self.job_id)
            if job is not None:
                job.stats = dict(self.stats)
                job.message = self._summary()
                if self._log:
                    job.log = (job.log or [])[-(200 - len(self._log[-50:])) :] + self._log[-50:]
                    self._log.clear()
                if self.stats["discovery_complete"]:
                    done = self.stats["products_analysed"]
                    job.progress = done / max(1, done + self.stats["pending_products"])
        self.db.commit()

    def _summary(self) -> str:
        s = self.stats
        return (
            f"{s['stage']}: {s['discovered']} discovered ({s['new']} new) · {s['products_analysed']} analysed · "
            f"{s['images_downloaded']} photos · {s['size_charts_excluded']} size charts excluded"
        )

    # ------------------------------------------------------------------ main loop
    def run(self) -> list[ScanRun]:
        if not self.states:
            return self._finish()
        self._check_control()
        if self.cancelled.is_set():
            self.stats["stage"] = "cancelled"
            return self._finish()
        workers = ThreadPoolExecutor(max_workers=max(4, 2 * INFLIGHT_PER_HOST * (len(self.states) + 1)))
        threads = []
        for st in self.states.values():
            self._resume_pending(st)
            if self.discover:
                t = threading.Thread(
                    target=self._discover_thread, args=(st,), daemon=True, name=f"ws-discover-{st.seller.id}"
                )
                t.start()
                threads.append(t)
            else:
                st.discovery_done = True
        self.stats["stage"] = "discovering" if self.discover else "analysing"
        saved_queued = False
        try:
            while True:
                self._check_control()
                # Stage A results
                for _ in range(500):
                    try:
                        kind, sid, payload = self.discovery_q.get_nowait()
                    except queue.Empty:
                        break
                    st = self.states[sid]
                    if kind == "ref" and not st.status:
                        self._on_ref(st, payload)
                    elif kind == "done":
                        self._discovery_finished(st, payload)
                    elif kind == "page":
                        self.say(f"{st.seller.name}: {payload}")
                all_discovered = all(st.discovery_done for st in self.states.values())
                if all_discovered and self.stats["stage"] == "discovering":
                    self.stats["stage"] = "analysing"
                # Schedule
                if self.paused.is_set():
                    self.stats["stage"] = "paused"
                elif self.stats["stage"] == "paused":
                    self.stats["stage"] = "analysing" if all_discovered else "discovering"
                if not self.paused.is_set() and not self.cancelled.is_set():
                    self._schedule(workers)
                # Results. Commit before blocking so the write lock is never held while waiting: you can
                # keep browsing and rating products while a scan runs.
                if self.db.in_transaction():
                    self.db.commit()
                if self.inflight:
                    done, _ = wait(list(self.inflight), timeout=0.2, return_when=FIRST_COMPLETED)
                    for fut in done:
                        self._handle(fut)
                else:
                    time.sleep(0.02)
                self._flush()
                if self.cancelled.is_set() and not self.inflight:
                    self.stats["stage"] = "cancelled"
                    break
                idle = not self.inflight and not any(self.queues.values()) and self.discovery_q.empty()
                if all_discovered and idle and not self.paused.is_set():
                    if not saved_queued:
                        saved_queued = True
                        self.stats["stage"] = "deep analysis"
                        self._queue_saved_for_deep()
                        if any(self.queues.values()):
                            continue
                    break
        finally:
            workers.shutdown(wait=True, cancel_futures=True)
            for t in threads:
                t.join(timeout=5)
            if self.own_client:
                self.client.close()
        return self._finish()

    def _schedule(self, workers: ThreadPoolExecutor) -> None:
        busy: dict[str, int] = {}
        for t in self.inflight.values():
            busy[t.host] = busy.get(t.host, 0) + 1
        discovering = {sid for sid, st in self.states.items() if not st.discovery_done}
        for host, q in self.queues.items():
            while q and busy.get(host, 0) < INFLIGHT_PER_HOST:
                task = q[0]
                # A seller's own pages wait until its listing has been read (fast discovery first).
                if task.kind == "detail" and task.seller_id in discovering:
                    break
                heapq.heappop(q)
                st = self.states.get(task.seller_id)
                if st is None or st.status:
                    continue
                if task.kind == "image":
                    self.queued_images.discard(task.image_id)
                    self.inflight_images.add(task.image_id)
                else:
                    self.queued_details.discard(task.product_id)
                self.inflight[workers.submit(self._work, task, st)] = task
                busy[host] = busy.get(host, 0) + 1

    def _handle(self, fut: Future) -> None:
        task = self.inflight.pop(fut)
        st = self.states.get(task.seller_id)
        if task.kind == "image":
            self.inflight_images.discard(task.image_id)
        if st is None:
            return
        try:
            result = fut.result()
        except Exception as e:  # noqa: BLE001 - per-task failures are recorded, never fatal
            if task.kind == "detail":
                self._on_detail_error(task, st, e)
            else:
                self._on_image_error(task, st, e)
            return
        if st.status:
            return
        if task.kind == "detail":
            self._on_detail(task, st, result)
        else:
            self._on_image(task, st, result)

    def _finish(self) -> list[ScanRun]:
        db = self.db
        cancelled = self.cancelled.is_set() and self.stats["stage"] == "cancelled"
        dedup.recompute_groups(db)
        runs = list(self.unsupported)
        for st in self.states.values():
            run = st.run
            run.finished_at = utcnow()
            run.pages_fetched = st.adapter.pages_read
            run.discovery_complete = st.exhausted
            run.errors = st.errors[:25]
            c = st.counts
            if cancelled:
                run.status, run.message = "cancelled", f"Cancelled: {c['discovered']} discovered, {c['new']} new so far"
            elif st.status:
                run.status, run.message = st.status, st.message
            else:
                run.status = "partial" if st.errors else "succeeded"
                coverage = "whole catalogue read" if st.exhausted else "catalogue NOT fully read (page limit)"
                run.message = (
                    f"{c['discovered']} albums seen ({coverage}), {c['new']} new, {c['changed']} changed, "
                    f"{c['unchanged']} unchanged"
                )
            run.stats = dict(c, pages=st.adapter.pages_read, total_pages=st.adapter.total_pages)
            st.seller.last_scan_at, st.seller.last_scan_status, st.seller.last_scan_message = (
                utcnow(), run.status, run.message,
            )  # fmt: skip
            runs.append(run)
            log.info("scan_finished", seller=st.seller.slug, status=run.status, new=c["new"], seen=c["discovered"])
        for run in self.unsupported:
            run.finished_at = utcnow()
            seller = db.get(Seller, run.seller_id)
            seller.last_scan_at, seller.last_scan_status, seller.last_scan_message = utcnow(), run.status, run.message
        if self.stats["stage"] not in ("cancelled",):
            self.stats["stage"] = "done"
        self._flush(force=True)
        db.commit()
        return runs


def scan_many(
    db: Session,
    sellers: list[Seller],
    *,
    mode: str = "incremental",
    progress=None,
    client: PoliteClient | None = None,
    job_id: int | None = None,
    max_products: int | None = None,
) -> list[ScanRun]:
    return Scanner(
        db, sellers, mode=mode, client=client, progress=progress, job_id=job_id, max_products=max_products
    ).run()


def drain_pending(db: Session, *, client: PoliteClient | None = None, progress=None, job_id: int | None = None) -> dict:
    """Continue analysis left pending by a cancelled, interrupted or capped scan (no listing traversal)."""
    sellers = (
        db.query(Seller)
        .join(Product, Product.seller_id == Seller.id)
        .filter(Product.analysis_state.in_(("discovered", "partial")), Product.status == "active")
        .distinct()
        .all()
    )
    if not sellers:
        return {"products": 0}
    sc = Scanner(db, sellers, mode="resume", client=client, progress=progress, job_id=job_id, discover=False)
    sc.run()
    return {"products": sc.stats["products_analysed"], "images": sc.stats["images_downloaded"]}
