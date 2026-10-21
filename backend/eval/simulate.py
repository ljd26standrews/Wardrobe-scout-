"""Offline recommender evaluation with simulated users.

Usage (from backend/):
    .venv/bin/python -m eval.simulate [personas] [methods] [seeds] > results.log
    .venv/bin/python -m eval.report results.log
e.g. .venv/bin/python -m eval.simulate stated,hidden,contrarian random,rules_only,legacy,bayes 2,3,4,5,6,7,8,9

Methods: random | rules_only (legacy engine, learning disabled) | legacy (pre-audit engine) | bayes (default).

Protocol: a synthetic catalogue of 360 items whose attributes (category, colour, material, fit, logo)
appear in the titles - so the real parser extracts them - and in drawn product images - so the real
image embedding sees them. Each persona has a hidden utility function. Every round the user is shown the
10-item Discover feed and loves / likes / skips / dislikes each item, giving the reason a real person
would give (their worst attribute). Metric: precision@10 = share of shown items the persona would want
(utility in the top band).

Caveats: images are simple drawings (optimistic for visual similarity), utility is attribute-based, and
seed 1 was used for calibration and seeds 2-9 are held out (see eval/RESULTS.md for paired confidence
intervals). These numbers measure learning dynamics, not real-world accuracy.
"""

from __future__ import annotations

import io
import json
import os
import random
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw

CATS = ["t-shirt", "hoodie", "knitwear", "cardigan", "trousers", "overcoat", "zip jacket", "shirt", "shorts", "jeans"]
COLS = {
    "black": (20, 20, 20), "charcoal": (55, 55, 58), "grey": (140, 140, 138), "cream": (238, 230, 214),
    "white": (250, 250, 247), "navy": (30, 40, 70), "olive": (90, 95, 55), "camel": (180, 138, 90),
    "red": (170, 35, 35), "yellow": (225, 185, 50), "blue": (60, 100, 170),
}  # fmt: skip
MATS = ["cashmere", "wool", "cotton", "polyester", "linen"]
FITS = ["relaxed", "slim", "oversized"]
LOGOS = ["no logo", "large logo print", "small logo"]
SHAPE = {
    "t-shirt": "tee", "shirt": "tee", "hoodie": "hoodie", "knitwear": "knit", "cardigan": "knit",
    "trousers": "trousers", "jeans": "trousers", "shorts": "shorts", "overcoat": "coat", "zip jacket": "jacket",
}  # fmt: skip
RELEVANT = 0.7


def draw(col: tuple[int, int, int], shape: str, rng: np.random.Generator) -> bytes:
    bg = tuple(int(x) for x in np.clip(np.array([235, 232, 226]) + rng.integers(-12, 12, 3), 0, 255))
    img = Image.new("RGB", (300, 375), bg)
    d = ImageDraw.Draw(img)
    w, h = img.size

    def jitter(v: float) -> float:
        return v + rng.uniform(-0.03, 0.03)

    c = tuple(int(x) for x in np.clip(np.array(col) + rng.integers(-15, 15, 3), 0, 255))
    if shape in ("hoodie", "knit"):
        d.rectangle([w * jitter(0.22), h * jitter(0.25), w * jitter(0.78), h * jitter(0.88)], fill=c)
        d.rectangle([w * 0.06, h * 0.28, w * 0.22, h * 0.78], fill=c)
        d.rectangle([w * 0.78, h * 0.28, w * 0.94, h * 0.78], fill=c)
        if shape == "hoodie":
            d.ellipse([w * 0.36, h * 0.08, w * 0.64, h * 0.34], fill=c)
    elif shape in ("jacket", "coat"):
        b = 0.95 if shape == "coat" else 0.78
        d.rectangle([w * jitter(0.2), h * 0.15, w * jitter(0.8), h * b], fill=c)
        d.rectangle([w * 0.05, h * 0.18, w * 0.2, h * 0.75], fill=c)
        d.rectangle([w * 0.8, h * 0.18, w * 0.95, h * 0.75], fill=c)
        d.line([w * 0.5, h * 0.15, w * 0.5, h * b], fill=(200, 200, 200), width=3)
    elif shape in ("trousers", "shorts"):
        b = 0.55 if shape == "shorts" else 0.96
        d.rectangle([w * 0.28, h * 0.05, w * 0.72, h * 0.18], fill=c)
        d.polygon([(w * 0.28, h * 0.18), (w * 0.49, h * 0.18), (w * 0.46, h * b), (w * 0.26, h * b)], fill=c)
        d.polygon([(w * 0.51, h * 0.18), (w * 0.72, h * 0.18), (w * 0.74, h * b), (w * 0.54, h * b)], fill=c)
    else:
        outline = [(0.25, 0.15), (0.75, 0.15), (0.95, 0.33), (0.8, 0.4), (0.75, 0.33), (0.75, 0.9), (0.25, 0.9),
                   (0.25, 0.33), (0.2, 0.4), (0.05, 0.33)]  # fmt: skip
        d.polygon([(w * x, h * y) for x, y in outline], fill=c, outline=(190, 190, 190))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def catalogue(seed: int = 0, n: int = 360) -> list[dict]:
    rng = np.random.default_rng(seed)
    r = random.Random(seed)
    items = []
    for i in range(n):
        it = {
            "cat": r.choice(CATS), "col": r.choice(list(COLS)), "mat": r.choice(MATS), "fit": r.choice(FITS),
            "logo": r.choices(LOGOS, weights=[5, 2, 3])[0], "price": float(r.randint(15, 160) * 10),
            "id": f"s{i}", "seller": f"seller{r.randint(1, 6)}",
        }  # fmt: skip
        it["img"] = draw(COLS[it["col"]], SHAPE[it["cat"]], rng)
        items.append(it)
    return items


# ----------------------------------------------------------------- personas
def u_stated(it: dict) -> float:
    """Matches the written profile."""
    s = 0.3 if it["col"] in ("black", "charcoal", "grey", "cream", "white", "navy", "camel") else -0.2
    s += {"cashmere": 0.3, "wool": 0.25, "cotton": 0.05, "linen": -0.1, "polyester": -0.3}[it["mat"]]
    s += {"no logo": 0.2, "small logo": 0, "large logo print": -0.4}[it["logo"]]
    s += {"relaxed": 0.15, "oversized": 0.05, "slim": -0.15}[it["fit"]]
    good = ("hoodie", "knitwear", "trousers", "overcoat", "t-shirt", "cardigan", "zip jacket")
    return s + (0.15 if it["cat"] in good else -0.2)


def u_hidden(it: dict) -> float:
    """Earth tones, cardigans and coats, no hoodies or tees: partly contradicts the written profile."""
    s = {"olive": 0.4, "camel": 0.4, "navy": 0.25, "cream": 0.1, "charcoal": 0, "black": -0.05}.get(it["col"], -0.3)
    s += {"cardigan": 0.35, "overcoat": 0.35, "knitwear": 0.15, "trousers": 0.05, "hoodie": -0.4, "t-shirt": -0.3}.get(
        it["cat"], -0.1
    )
    s += {"cashmere": 0.2, "wool": 0.2, "cotton": 0, "linen": -0.05, "polyester": -0.3}[it["mat"]]
    return s + {"no logo": 0.1, "small logo": 0, "large logo print": -0.4}[it["logo"]]


def u_contra(it: dict) -> float:
    """Summer, light colours, linen and cotton, shirts and shorts: the opposite of the written profile."""
    s = {"white": 0.3, "cream": 0.3, "blue": 0.3, "yellow": 0.2}.get(it["col"], -0.2)
    s += {"linen": 0.35, "cotton": 0.25, "polyester": -0.1, "wool": -0.2, "cashmere": -0.15}[it["mat"]]
    s += {"shirt": 0.3, "shorts": 0.3, "t-shirt": 0.15}.get(it["cat"], -0.2)
    return s + {"no logo": 0.05, "small logo": 0, "large logo print": -0.2}[it["logo"]]


PERSONAS = {"stated": u_stated, "hidden": u_hidden, "contrarian": u_contra}


def normalise(us: dict[str, float]) -> dict[str, float]:
    lo, hi = min(us.values()), max(us.values())
    return {k: (v - lo) / (hi - lo) for k, v in us.items()}


def worst_reason(it: dict, fn) -> list[str]:
    """The rejection reason a person would give: the attribute whose change would help most."""
    base = fn(it)
    best: tuple[float, str | None] | None = None
    options = (("col", list(COLS), "colour"), ("mat", MATS, "fabric"), ("logo", LOGOS, "logo"),
               ("fit", FITS, "silhouette"), ("cat", CATS, None))  # fmt: skip
    for attr, alts, reason in options:
        gain = max(fn({**it, attr: a}) for a in alts) - base
        if best is None or gain > best[0]:
            best = (gain, reason)
    return [best[1]] if best and best[1] else []


def decide(it: dict, u: float, fn) -> tuple[str, list[str]]:
    if u >= 0.85:
        return "love", []
    if u >= RELEVANT:
        return "like", []
    if u >= 0.5:
        return "skip", []
    return "dislike", worst_reason(it, fn)


# --------------------------------------------------------------- harness
def run(persona: str, method: str, seed: int, rounds: int = 8, k: int = 10) -> dict:
    tmp = tempfile.mkdtemp(prefix="wseval")
    os.environ.update(
        WS_DATA_DIR=tmp, WS_DATABASE_URL=f"sqlite:///{tmp}/e.db", WS_ENABLE_SCHEDULER="false",
        WS_EMBEDDING_BACKEND="lite", WS_DOWNLOAD_IMAGES="false", WS_API_TOKEN="eval",
    )  # fmt: skip
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services import embeddings

    embeddings.reset_backend()
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import process_images, upsert_listing
    from app.main import create_app
    from app.models import Product, ProductImage, Seller
    from app.services.images import phash_of, store_image_bytes
    from app.services.settings_store import get_setting, set_setting

    items = catalogue(seed)
    fn = PERSONAS[persona]
    util = normalise({it["id"]: fn(it) for it in items})
    relevant = {i for i, u in util.items() if u >= RELEVANT}
    by_ext = {it["id"]: it for it in items}
    results = []
    with TestClient(create_app(), headers={"X-WS-Token": "eval"}) as c:
        db = SessionLocal()
        sellers: dict[str, Seller] = {}
        for it in items:
            if it["seller"] not in sellers:
                sellers[it["seller"]] = Seller(name=it["seller"], slug=it["seller"], platform="manual")
                db.add(sellers[it["seller"]])
                db.flush()
            title = f"{it['col']} {it['mat']} {it['fit']} {it['cat']} {it['logo']}"
            raw = RawListing(it["id"], f"https://shop.example/{it['id']}", title, price=it["price"])
            p = upsert_listing(db, sellers[it["seller"]], raw).product
            rel, pil = store_image_bytes(it["img"], "uploads")
            p.images.append(
                ProductImage(
                    url=f"upload:{rel}", local_path=rel, fetch_status="uploaded", position=0, phash=phash_of(pil)
                )
            )
            db.flush()
            process_images(db, p, None)
        db.commit()
        cfg = get_setting(db, "recommender")
        cfg["engine"] = "bayes" if method == "bayes" else "legacy"
        if method == "rules_only":
            cfg["cold_start_k"] = 10**9
        set_setting(db, "recommender", cfg)
        ext_of = {p.id: p.external_id for p in db.query(Product)}
        db.close()
        rng = random.Random(seed + 99)
        decided: set[int] = set()
        found = 0
        for rnd in range(rounds):
            if method == "random":
                pool = [pid for pid, e in ext_of.items() if e in by_ext and pid not in decided]
                shown = rng.sample(pool, min(k, len(pool)))
            else:
                shown = [x["id"] for x in c.get(f"/api/feed?limit={k}").json()["items"]]
            hits = sum(1 for pid in shown if ext_of[pid] in relevant)
            found += hits
            results.append({"round": rnd + 1, "precision": hits / k, "found": found})
            for pid in shown:
                it = by_ext[ext_of[pid]]
                action, reasons = decide(it, util[it["id"]], fn)
                c.post("/api/feedback", json={"product_id": pid, "action": action, "reasons": reasons})
                decided.add(pid)
    return {"persona": persona, "method": method, "seed": seed, "n_relevant": len(relevant), "rounds": results}


def main(argv: list[str]) -> None:
    personas = argv[0].split(",") if argv else list(PERSONAS)
    methods = argv[1].split(",") if len(argv) > 1 else ["random", "rules_only", "legacy", "bayes"]
    seeds = [int(s) for s in (argv[2].split(",") if len(argv) > 2 else ["2", "3"])]
    for persona in personas:
        for method in methods:
            for seed in seeds:
                r = run(persona, method, seed)
                prec = [x["precision"] for x in r["rounds"]]
                label = os.environ.get("METHOD_LABEL", method)
                print(json.dumps({"persona": persona, "method": label, "seed": seed, "rel": r["n_relevant"], "prec": prec}),
                      flush=True)  # fmt: skip


if __name__ == "__main__":
    main(sys.argv[1:])
