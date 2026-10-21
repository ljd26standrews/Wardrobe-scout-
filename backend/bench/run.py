"""Run one instrumented scan of a benchmark catalogue and score it against ground truth.

The runner only uses interfaces that exist in both the original and the optimised scanner
(``scan.scan_seller`` / ``scan.scan_many`` and ``PoliteClient``), so the same harness measures both:

    # optimised (this checkout)
    .venv/bin/python -m bench.run --products 300 --label optimised --out results/opt300.json
    # original code from another checkout, same harness
    PYTHONPATH=/path/to/original/backend:. .venv/bin/python -m bench.run --products 300 --label baseline ...

Times are reported in two forms: measured wall-clock at the chosen ``--scale`` (network and politeness
delays multiplied by ``scale``), and request counts per host, which together with the real politeness
delay give the minimum real-world duration (``politeness_floor_s``).
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent


# --------------------------------------------------------------------------- transport
class LocalRoute(httpx.HTTPTransport):
    """Send every request to the local benchmark server, preserving the original Host header."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port
        self.lock = threading.Lock()
        self.requests: dict[str, int] = defaultdict(int)
        self.bytes: dict[str, int] = defaultdict(int)
        self.status: dict[str, int] = defaultdict(int)
        self.conditional = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        request.headers["Host"] = host
        if "if-none-match" in request.headers or "if-modified-since" in request.headers:
            with self.lock:
                self.conditional += 1
        local = httpx.Request(
            request.method,
            request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port),
            headers=request.headers,
            stream=request.stream,
        )
        resp = super().handle_request(local)
        n = int(resp.headers.get("content-length") or 0)
        with self.lock:
            self.requests[host] += 1
            self.bytes[host] += n
            self.status[str(resp.status_code)] += 1
        return resp


# --------------------------------------------------------------------------- instrumentation
class Timers:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.t: dict[str, float] = defaultdict(float)
        self.n: dict[str, int] = defaultdict(int)

    def wrap(self, owner, name: str, label: str) -> None:
        if not hasattr(owner, name):
            return
        orig = getattr(owner, name)
        timers = self

        def timed(*a, **k):
            t0 = time.perf_counter()
            try:
                return orig(*a, **k)
            finally:
                dt = time.perf_counter() - t0
                with timers.lock:
                    timers.t[label] += dt
                    timers.n[label] += 1

        setattr(owner, name, timed)
        # Re-point module-level aliases (``from x import f``) at the wrapper too.
        for mod in list(sys.modules.values()):
            if getattr(mod, "__name__", "").startswith("app.") and getattr(mod, name, None) is orig:
                setattr(mod, name, timed)


def instrument(timers: Timers) -> None:
    from sqlalchemy.orm import Session

    import app.ingestion.http as http
    import app.ingestion.pipeline as pipeline
    import app.ingestion.scan  # noqa: F401  (load aliases before patching)
    import app.ingestion.yupoo as yupoo
    import app.services.dedup as dedup
    import app.services.embeddings as emb
    import app.services.images as images

    timers.wrap(http.PoliteClient, "_throttle", "politeness_wait")
    timers.wrap(http.PoliteClient, "_bounded_get", "http_transfer")
    timers.wrap(yupoo, "parse_listing_page", "parse_listing")
    timers.wrap(yupoo, "parse_album_page", "parse_album")
    timers.wrap(pipeline, "upsert_listing", "db_upsert")
    timers.wrap(images, "store_image_bytes", "image_decode_resize_store")
    timers.wrap(images, "phash_of", "phash")
    timers.wrap(emb.LiteImageBackend, "embed", "embed_image")
    timers.wrap(emb.LiteImageBackend, "embed_batch", "embed_image")
    timers.wrap(dedup, "match_product", "dedup_match")
    timers.wrap(dedup, "match_products", "dedup_match")
    timers.wrap(dedup, "recompute_groups", "dedup_groups")
    timers.wrap(Session, "commit", "db_commit")
    try:
        import app.services.image_select as sel

        timers.wrap(sel, "analyse", "image_analyse")
        timers.wrap(sel, "select", "image_select")
    except ImportError:
        pass


# --------------------------------------------------------------------------- server
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(spec_path: Path, port: int, scale: float, extra: list[str]) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(BACKEND))
    proc = subprocess.Popen(
        [sys.executable, "-m", "bench.server", str(spec_path), "--port", str(port), "--scale", str(scale), *extra],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if "serving" not in line:
        raise RuntimeError(f"benchmark server failed to start: {line}{proc.stdout.read()}")
    return proc


def control(port: int, cmd: str, **params) -> dict:
    r = httpx.get(f"http://127.0.0.1:{port}/__control/{cmd}", params=params, timeout=30)
    return r.json()


# --------------------------------------------------------------------------- scan
def run_scan(db, sellers, client, mode: str) -> list:
    from app.ingestion import scan

    if hasattr(scan, "scan_many"):
        return scan.scan_many(db, sellers, mode=mode, client=client)
    return [scan.scan_seller(db, s, mode=mode, client=client) for s in sellers]


def measure(label: str, fn) -> dict:
    t0, c0 = time.perf_counter(), time.process_time()
    out = fn()
    return {"wall_s": time.perf_counter() - t0, "cpu_s": time.process_time() - c0, "result": out}


def run_info(runs) -> list[dict]:
    keys = ("status", "message", "pages_fetched", "products_seen", "products_new", "products_updated")
    return [{k: getattr(r, k, None) for k in keys} for r in runs]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", type=int, default=50)
    ap.add_argument("--sellers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", type=float, default=0.05)
    ap.add_argument("--delay", type=float, default=2.0, help="politeness delay before scaling (app default 2.0)")
    ap.add_argument("--mode", default="full")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repeat", choices=["none", "unchanged", "changed"], default="none")
    ap.add_argument("--quality", action="store_true", help="also run simulated shoppers on the result")
    ap.add_argument("--keep", help="copy the resulting data dir here")
    ap.add_argument("--server-args", default="", help="extra args for bench.server, e.g. '--error-rate 0.05'")
    args = ap.parse_args()

    from . import catalogue

    work = Path(tempfile.mkdtemp(prefix="wsbench"))
    spec = catalogue.build(args.products, args.sellers, args.seed)
    spec_path = work / "spec.json"
    catalogue.save(spec, spec_path)
    os.environ.update(
        WS_DATA_DIR=str(work / "data"),
        WS_DATABASE_URL=f"sqlite:///{work}/data/bench.db",
        WS_ENABLE_SCHEDULER="false",
        WS_EMBEDDING_BACKEND="lite",
        WS_ALLOW_PRIVATE_URLS="true",
        WS_API_TOKEN="bench",
        WS_LOG_LEVEL="WARNING",
        WS_RESPECT_ROBOTS_TXT="true",
        WS_DEEP_ON_FEEDBACK="false",
    )
    (work / "data").mkdir()
    port = free_port()
    server = start_server(spec_path, port, args.scale, args.server_args.split())
    result: dict = {"label": args.label, "args": vars(args), "catalogue": catalogue.stats(spec)}
    try:
        import app as app_pkg
        from app.main import bootstrap

        result["app_code"] = str(Path(app_pkg.__file__).resolve().parent)  # which implementation was measured
        bootstrap()
        from app.db import SessionLocal
        from app.ingestion.http import PoliteClient
        from app.models import Seller
        from app.services.settings_store import get_setting, set_setting

        timers = Timers()
        instrument(timers)
        db = SessionLocal()
        sched = get_setting(db, "schedule")
        sched["max_products_per_scan"] = 10**6  # measure whole catalogues, not the per-scan cap
        set_setting(db, "schedule", sched)
        sellers = []
        for name in spec["sellers"]:
            s = Seller(name=name, slug=name, platform="yupoo", base_url=f"https://{name}.x.yupoo.com")
            db.add(s)
            sellers.append(s)
        db.commit()
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        def one_scan(mode: str) -> dict:
            transport = LocalRoute(port)
            client = PoliteClient(delay=args.delay * args.scale, transport=transport)
            control(port, "reset")
            m = measure("scan", lambda: run_scan(db, sellers, client, mode))
            client.close()
            per_host = dict(transport.requests)
            seller_hosts = {h: n for h, n in per_host.items() if h != "photo.yupoo.com"}
            floor_serial = sum(per_host.values()) * args.delay
            floor_parallel = max(per_host.values() or [0]) * args.delay
            n_products = sum(len(v) for v in spec["albums"].values())
            return {
                "wall_s": round(m["wall_s"], 2),
                "cpu_s": round(m["cpu_s"], 2),
                "cpu_utilisation": round(m["cpu_s"] / max(1e-9, m["wall_s"]), 2),
                "requests": per_host,
                "requests_total": sum(per_host.values()),
                "requests_per_product": round(sum(per_host.values()) / max(1, n_products), 2),
                "page_requests": sum(seller_hosts.values()),
                "image_requests": per_host.get("photo.yupoo.com", 0),
                "bytes": dict(transport.bytes),
                "image_mb": round(transport.bytes.get("photo.yupoo.com", 0) / 1e6, 2),
                "conditional_requests": transport.conditional,
                "http_status": dict(transport.status),
                "politeness_floor_s": {"if_hosts_serial": floor_serial, "if_hosts_parallel": floor_parallel},
                "scan_runs": run_info(m["result"]),
            }

        result["scan1"] = one_scan(args.mode)
        result["timers_s"] = {k: round(v, 2) for k, v in sorted(timers.t.items(), key=lambda kv: -kv[1])}
        result["timer_calls"] = dict(timers.n)
        result["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        result["rss_before_scan_mb"] = round(rss0 / 1024, 1)
        db.close()

        # Post-scan background analysis, if the optimised scanner defers any (counted separately).
        try:
            from app.ingestion import scan as scan_mod

            if hasattr(scan_mod, "drain_pending"):
                db = SessionLocal()
                transport = LocalRoute(port)
                client = PoliteClient(delay=args.delay * args.scale, transport=transport)
                m = measure("drain", lambda: scan_mod.drain_pending(db, client=client))
                result["drain"] = {"wall_s": round(m["wall_s"], 2), "requests": dict(transport.requests)}
                client.close()
                db.close()
        except ImportError:
            pass

        from . import score

        db = SessionLocal()
        result["accuracy"] = score.score(db, spec)
        db.close()

        if args.repeat != "none":
            if args.repeat == "changed":
                spec2 = catalogue.mutate(spec, seed=args.seed + 1)
                p2 = work / "spec2.json"
                catalogue.save(spec2, p2)
                control(port, "load", path=str(p2))
                result["mutation"] = spec2.get("mutation")
            db = SessionLocal()
            sellers = [db.get(Seller, s.id) for s in sellers]
            timers.t.clear()
            result["scan2"] = one_scan("incremental")
            result["timers2_s"] = {k: round(v, 2) for k, v in sorted(timers.t.items(), key=lambda kv: -kv[1])}
            db.close()
            db = SessionLocal()
            result["accuracy2"] = score.score(db, spec2 if args.repeat == "changed" else spec)
            db.close()

        if args.quality:
            from . import quality

            result["recommendation"] = quality.evaluate(Path(os.environ["WS_DATA_DIR"]), spec)
    finally:
        server.terminate()
        if args.keep:
            shutil.copytree(work, args.keep, dirs_exist_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: result[k] for k in ("label", "scan1") if k in result}, default=str)[:3000])


if __name__ == "__main__":
    main()
