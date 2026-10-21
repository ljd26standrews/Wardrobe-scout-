"""Recommendation quality on a scanned benchmark database.

Simulated shoppers whose taste depends only on what is visible in the photos (garment class and how
dark the garment photo is), because benchmark titles are deliberately opaque. Precision@10 over 8 rounds
of the real Discover feed therefore measures how useful the scanner's image vectors are for learning
taste. Each shopper starts from a copy of the scanned database.
"""

from __future__ import annotations

import os
import shutil
import statistics as st
from functools import cache
from pathlib import Path

import numpy as np
from PIL import Image

PERSONAS = {
    "dark_outerwear": ({"outwear": 1.0, "longsleeve": 0.7, "pants": 0.6, "shirt": 0.35}, +0.8),
    "light_summer": ({"t-shirt": 1.0, "shorts": 1.0, "skirt": 0.7, "dress": 0.7, "shirt": 0.5}, -0.8),
}


@cache
def darkness(path: str) -> float:
    im = Image.open(path).convert("L")
    w, h = im.size
    c = np.asarray(im.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4)), dtype=np.float32) / 255
    return float(1 - c.mean())


def utilities(spec: dict, persona: str) -> dict[tuple[str, str], float]:
    weights, dark_w = PERSONAS[persona]
    out = {}
    for s, albums in spec["albums"].items():
        for a in albums:
            d = darkness(a["photos"][0])
            out[(s, a["id"])] = weights.get(a["cls"], 0.0) + dark_w * (d - 0.5)
    return out


def run_persona(data_dir: Path, spec: dict, persona: str, rounds: int = 8, k: int = 10) -> list[float]:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.db import SessionLocal, init_engine
    from app.main import create_app
    from app.models import Product, Seller

    src = data_dir / "bench.db"
    dst = data_dir / f"persona_{persona}.db"
    shutil.copy(src, dst)
    os.environ["WS_DATABASE_URL"] = f"sqlite:///{dst}"
    get_settings.cache_clear()
    init_engine(f"sqlite:///{dst}")
    util = utilities(spec, persona)
    cut = np.quantile(list(util.values()), 0.8)
    db = SessionLocal()
    slug = {s.id: s.slug for s in db.query(Seller)}
    key = {p.id: (slug.get(p.seller_id), p.external_id) for p in db.query(Product)}
    db.close()
    prec = []
    with TestClient(create_app(), headers={"X-WS-Token": "bench"}) as c:
        for _ in range(rounds):
            items = c.get(f"/api/feed?limit={k}").json()["items"]
            hits = 0
            for it in items:
                u = util.get(key.get(it["id"]))
                if u is None:
                    action = "skip"
                elif u >= cut:
                    hits += 1
                    action = "love" if u >= cut + 0.3 else "like"
                elif u < np.quantile(list(util.values()), 0.5):
                    action = "dislike"
                else:
                    action = "skip"
                c.post("/api/feedback", json={"product_id": it["id"], "action": action, "reasons": []})
            prec.append(hits / k)
    return prec


def evaluate(data_dir: Path, spec: dict) -> dict:
    out = {}
    for persona in PERSONAS:
        p = run_persona(data_dir, spec, persona)
        out[persona] = {"p10_by_round": p, "p10_mean": round(st.mean(p), 3), "p10_rounds_2_8": round(st.mean(p[1:]), 3)}
    out["p10_mean_all"] = round(st.mean(v["p10_mean"] for v in out.values() if isinstance(v, dict)), 3)
    return out
