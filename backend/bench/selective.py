"""Latency and memory of the selective feeds at several catalogue sizes (SYNTHETIC catalogue).

Usage (from backend/):
    .venv/bin/python -m bench.selective 1000,5000,20000 > bench/results/selective.jsonl

Each size runs in its own process, so memory figures do not carry over. Per size:
- a synthetic catalogue (attributes in the fields, a random photo embedding per piece) and 400 decisions drawn from
  known approval probabilities, then one calibration (timed: the recalibration job's work);
- requests through the real app (median of 5 after one warm-up): the main feed in each mode, Learning picks, the
  earlier ranked feed (selective off) for comparison, a product page, Insights, the selective status;
- peak resident memory of the process.
Timings depend on this machine; compare sizes and feeds with each other, not with another computer's numbers.
"""

from __future__ import annotations

import json
import os
import resource
import statistics as st
import subprocess
import sys
import tempfile
import time

COLOURS = ["black", "navy", "grey", "cream", "white", "olive", "camel", "red", "brown", "blue"]
CATEGORIES = ["overcoat", "trousers", "hoodie", "knitwear", "shirt", "t-shirt", "cardigan", "jeans"]
FIBRES = ["wool", "cashmere", "cotton", "linen", "polyester"]


def _timed(fn, repeat: int = 5) -> dict:
    fn()  # warm-up (caches, first fit)
    times = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return {"median_ms": round(1000 * st.median(times), 1), "max_ms": round(1000 * max(times), 1)}


def run_size(n: int, decisions: int = 400, seed: int = 0) -> dict:
    tmp = tempfile.mkdtemp(prefix="wsbench")
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
    from app.models import Feedback, Product, Seller
    from app.services import calibration, jobs

    jobs.enqueue = lambda kind, params=None: 0  # no background work during timing
    rng = np.random.default_rng(seed)
    out: dict = {"products": n, "decisions": decisions}
    with TestClient(create_app(), headers={"X-WS-Token": "bench"}) as c:
        db = SessionLocal()
        sellers = [Seller(name=f"b{i}", slug=f"b{i}", platform="manual") for i in range(12)]
        db.add_all(sellers)
        db.flush()
        backend = emb.get_image_backend().name
        t0 = time.perf_counter()
        logit = {}
        for i in range(n):
            col, cat, fib = COLOURS[i % 10], CATEGORIES[(i // 10) % 8], FIBRES[(i // 80) % 5]
            p = Product(seller_id=sellers[i % 12].id, external_id=f"x{i}", title_original=f"{col} {fib} {cat} {i}",
                        title_en=f"{col} {fib} {cat} {i}", category=cat, colours=[col],
                        materials=[{"fibre": fib, "pct": 100.0}], price=float(rng.integers(20, 200) * 10),
                        source_url=f"https://shop.example/x{i}", data_origin="manual", logo_level="none",
                        image_embedding=emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32))),
                        embedding_backend=backend)  # fmt: skip
            db.add(p)
            # a clear taste, so that the gate has pieces to pass (the heavier path is the one measured)
            colour_term = 2.2 if col in ("navy", "black", "camel") else -1.5
            logit[i] = colour_term + (1.2 if cat in ("overcoat", "knitwear") else -0.6)
        db.flush()
        ids = [pid for (pid,) in db.query(Product.id).filter(Product.data_origin == "manual").order_by(Product.id)]
        for k in rng.choice(len(ids), size=decisions, replace=False):
            y = rng.random() < 1 / (1 + np.exp(-logit[int(k)]))
            db.add(Feedback(product_id=ids[int(k)], action="like" if y else "dislike"))
        db.commit()
        out["seed_s"] = round(time.perf_counter() - t0, 1)
        t = time.perf_counter()
        res = calibration.recalibrate(db, force=True)
        out["recalibrate_s"] = round(time.perf_counter() - t, 2)
        out["calibration"] = {k: res["diagnostics"][k] for k in ("map", "auc_out_of_fold", "ece_chosen")}
        db.close()

        def get(url: str):
            r = c.get(url)
            assert r.status_code == 200, (url, r.status_code, r.text[:200])
            return r.json()

        sel = get("/api/settings")["selective"]
        out["feeds"] = {}
        for mode in ("balanced", "high", "extreme"):
            out["feeds"][mode] = _timed(lambda m=mode: get(f"/api/feed?limit=24&mode={m}"))
            out["feeds"][mode]["shown"] = len(get(f"/api/feed?limit=24&mode={mode}")["items"])
        out["feeds"]["learning"] = _timed(lambda: get("/api/feed/learning?limit=24"))
        out["feeds"]["product_page"] = _timed(lambda: get(f"/api/products/{ids[0]}"))
        out["feeds"]["selective_status"] = _timed(lambda: get("/api/selective/status"))
        out["feeds"]["insights"] = _timed(lambda: get("/api/insights"), repeat=3)
        c.put("/api/settings/selective", json={**sel, "enabled": False})
        out["feeds"]["earlier_ranked_feed"] = _timed(lambda: get("/api/feed?limit=24"))
    out["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    return out


def main(argv: list[str]) -> None:
    if argv and argv[0] == "--one":
        print(json.dumps(run_size(int(argv[1]))), flush=True)
        return
    for n in argv[0].split(",") if argv else ["1000", "5000", "20000"]:
        proc = subprocess.run([sys.executable, "-m", "bench.selective", "--one", n], capture_output=True, text=True)
        line = next((x for x in proc.stdout.splitlines() if x.startswith("{")), None)
        print(line or json.dumps({"products": int(n), "error": proc.stderr[-1500:]}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
