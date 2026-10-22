"""Discover's requests on a representative catalogue: cold and warm latency (p50/p95), SQL statements, response size
and peak memory (SYNTHETIC catalogue; no network).

Usage (from backend/):
    .venv/bin/python -m bench.feed_latency 1000,5000,20000 > bench/results/feed_latency.jsonl
    .venv/bin/python -m bench.feed_latency --profile 20000      # where the time goes in one feed request

Each size runs in its own process, so memory does not carry over. The catalogue is meant to look like a real one:
- every product has 3 photo records (one analysed garment photo with an embedding, a second garment view, a size
  chart), parsed attributes and a price (90% yuan, 5% pounds, 5% unstated currency);
- 12 sellers, a quarter of them with seller-level evidence; 10% of products carry 1-3 evidence items;
- 10% of products belong to garment families of 2-3 listings (same group, a strong relation between them);
- 400 decisions with reasons as people give them (likes, loves, "not now", dislikes about the look, about price,
  size or owning something similar, maybes, purchases, skips), one calibration fitted before timing.

Measured through the real app, per request kind:
- cold: the first request after the data changed (here: after one more decision), so caches keyed by data miss;
- warm: 20 repeats with nothing changed (p50, p95);
- SQL statements executed and response bytes for one warm request;
- peak resident memory of the process at the end.
Timings depend on this machine (cores, disk): compare sizes and request kinds with each other.
"""

from __future__ import annotations

import cProfile
import io
import json
import os
import pstats
import resource
import statistics as st
import subprocess
import sys
import tempfile
import time

COLOURS = ["black", "navy", "grey", "cream", "white", "olive", "camel", "red", "brown", "blue"]
CATEGORIES = ["overcoat", "trousers", "hoodie", "knitwear", "shirt", "t-shirt", "cardigan", "jeans"]
FIBRES = ["wool", "cashmere", "cotton", "linen", "polyester"]


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))]


def build(n: int, decisions: int = 400, seed: int = 0):
    """A fresh app on a temporary database with the catalogue above. Returns (client, product ids)."""
    tmp = tempfile.mkdtemp(prefix="wsfeed")
    os.environ.update(
        WS_DATA_DIR=tmp, WS_DATABASE_URL=f"sqlite:///{tmp}/b.db", WS_ENABLE_SCHEDULER="false",
        WS_EMBEDDING_BACKEND="lite", WS_DOWNLOAD_IMAGES="false", WS_API_TOKEN="bench", WS_DEEP_ON_FEEDBACK="false",
    )  # fmt: skip
    import numpy as np
    from fastapi.testclient import TestClient

    from app.config import get_settings

    get_settings.cache_clear()
    from app.services import embeddings as emb

    emb.reset_backend()
    from app.db import SessionLocal
    from app.main import create_app
    from app.models import Evidence, Feedback, Product, ProductImage, ProductRelation, Seller
    from app.services import calibration, jobs

    jobs.enqueue = lambda kind, params=None: 0  # no background work during timing
    rng = np.random.default_rng(seed)
    client = TestClient(create_app(), headers={"X-WS-Token": "bench"})
    client.__enter__()
    db = SessionLocal()
    sellers = [Seller(name=f"b{i}", slug=f"b{i}", platform="manual") for i in range(12)]
    db.add_all(sellers)
    db.flush()
    for s in sellers[:3]:
        db.add(Evidence(seller_id=s.id, kind="buyer_report", aspect="service", claim="Shipped quickly", polarity=1,
                        strength=0.7, source_url=f"https://forum.example/{s.slug}"))  # fmt: skip
    backend = emb.get_image_backend().name
    logit = {}
    for i in range(n):
        col, cat, fib = COLOURS[i % 10], CATEGORIES[(i // 10) % 8], FIBRES[(i // 80) % 5]
        r = rng.random()
        currency = "CNY" if r < 0.9 else "GBP" if r < 0.95 else ""
        price = float(rng.integers(20, 200) * 10) if currency != "GBP" else float(rng.integers(20, 200))
        p = Product(seller_id=sellers[i % 12].id, external_id=f"x{i}", title_original=f"{col} {fib} {cat} {i}",
                    title_en=f"{col} {fib} {cat} {i}", category=cat, colours=[col],
                    materials=[{"fibre": fib, "pct": 100.0}], material_text=f"100% {fib}", price=price,
                    currency=currency, sizes=["S", "M", "L"], source_url=f"https://shop.example/x{i}",
                    data_origin="manual", logo_level="none",
                    image_embedding=emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32))),
                    embedding_backend=backend)  # fmt: skip
        db.add(p)
        logit[i] = (2.2 if col in ("navy", "black", "camel") else -1.5) + (
            1.2 if cat in ("overcoat", "knitwear") else -0.6
        )
    db.flush()
    ids = [pid for (pid,) in db.query(Product.id).filter(Product.data_origin == "manual").order_by(Product.id)]
    for pid in ids:
        for pos, role in enumerate(("garment", "garment", "size_chart")):
            db.add(ProductImage(product_id=pid, url=f"https://img.example/{pid}/{pos}.jpg", position=pos, role=role,
                                selected=role == "garment", fetch_status="ok"))  # fmt: skip
    for pid in rng.choice(ids, size=n // 10, replace=False):
        for k in range(int(rng.integers(1, 4))):
            db.add(Evidence(product_id=int(pid), kind="buyer_report", aspect="construction", claim=f"QC note {k}",
                            polarity=int(rng.choice([-1, 1])), strength=0.6,
                            source_url=f"https://forum.example/t/{pid}/{k}"))  # fmt: skip
    fam_members = rng.choice(ids, size=n // 10, replace=False)
    for start in range(0, len(fam_members) - 2, 3):
        group = [int(x) for x in fam_members[start : start + int(rng.integers(2, 4))]]
        for pid in group:
            db.get(Product, pid).group_id = group[0]
        for a, b in zip(group, group[1:], strict=False):
            db.add(ProductRelation(a_id=a, b_id=b, relation="shared_photos", score=0.9, signals={}))
    actions = [("like", []), ("love", []), ("like", ["not_now"]), ("dislike", []), ("dislike", ["colour"]),
               ("dislike", ["too_expensive"]), ("dislike", ["wrong_size"]), ("dislike", ["already_own"]),
               ("maybe", []), ("purchased", []), ("skip", [])]  # fmt: skip
    for k in rng.choice(len(ids), size=decisions, replace=False):
        y = rng.random() < 1 / (1 + np.exp(-logit[int(k)]))
        action, reasons = actions[int(rng.integers(0, 3))] if y else actions[int(rng.integers(3, len(actions)))]
        db.add(Feedback(product_id=ids[int(k)], action=action, reasons=reasons))
    db.commit()
    calibration.recalibrate(db, force=True)
    db.close()
    return client, ids


REQUESTS = {
    "feed_balanced": "/api/feed?limit=24&mode=balanced",
    "feed_high": "/api/feed?limit=24&mode=high",
    "learning": "/api/feed/learning?limit=24",
    "browse": "/api/products?page_size=36",
    "product_page": None,  # filled with a product id
    "saved": "/api/saved",
    "insights": "/api/insights",
    "selective_status": "/api/selective/status",
}


def run_size(n: int) -> dict:
    from sqlalchemy import event

    t0 = time.perf_counter()
    client, ids = build(n)
    out: dict = {"products": n, "build_s": round(time.perf_counter() - t0, 1), "requests": {}}
    from app.db import get_engine

    statements = {"n": 0}
    event.listen(
        get_engine(), "before_cursor_execute", lambda *a, **k: statements.__setitem__("n", statements["n"] + 1)
    )
    urls = {k: (v or f"/api/products/{ids[len(ids) // 2]}") for k, v in REQUESTS.items()}
    undecided = iter(ids[::-1])

    def get(url):
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
        return r

    for name, url in urls.items():
        # cold: one more decision first, so every cache keyed by your data is stale
        client.post("/api/feedback", json={"product_id": next(undecided), "action": "skip"})
        t = time.perf_counter()
        get(url)
        cold = time.perf_counter() - t
        warm = []
        for _ in range(20 if name != "insights" else 6):
            t = time.perf_counter()
            get(url)
            warm.append(time.perf_counter() - t)
        statements["n"] = 0
        size = len(get(url).content)
        out["requests"][name] = {
            "cold_ms": round(1000 * cold, 1), "warm_p50_ms": round(1000 * st.median(warm), 1),
            "warm_p95_ms": round(1000 * _pct(warm, 0.95), 1), "sql_statements": statements["n"],
            "response_kb": round(size / 1024, 1),
        }  # fmt: skip
    out["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    client.__exit__(None, None, None)
    return out


def profile(n: int, url: str = "/api/feed?limit=24&mode=balanced", top: int = 30) -> str:
    client, ids = build(n)
    client.post("/api/feedback", json={"product_id": ids[-1], "action": "skip"})  # cold
    pr = cProfile.Profile()
    pr.enable()
    client.get(url)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(top)
    return s.getvalue()


def main(argv: list[str]) -> None:
    if argv and argv[0] == "--one":
        print(json.dumps(run_size(int(argv[1]))), flush=True)
        return
    if argv and argv[0] == "--profile":
        print(profile(int(argv[1]) if len(argv) > 1 else 20000))
        return
    for n in argv[0].split(",") if argv else ["1000", "5000", "20000"]:
        proc = subprocess.run([sys.executable, "-m", "bench.feed_latency", "--one", n], capture_output=True, text=True)
        line = next((x for x in proc.stdout.splitlines() if x.startswith("{")), None)
        print(line or json.dumps({"products": int(n), "error": proc.stderr[-1500:]}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
