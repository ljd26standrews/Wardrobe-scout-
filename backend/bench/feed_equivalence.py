"""Exact synthetic ranking/feature/feed fingerprints before and after optimisations.

Run this same script with PYTHONPATH pointing at each checkout's backend, in separate processes:
    PYTHONPATH=/path/to/backend python /path/to/feed_equivalence.py /tmp/result.json
Only the calibration's wall-clock creation timestamp is excluded; scores, reasons, gates and ordering are kept.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

from bench.feed_latency import build


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def run(n: int = 3000) -> dict:
    client, ids = build(n)
    from app.api.products import feed_filters, ranked_candidates
    from app.db import SessionLocal
    from app.models import Product
    from app.services.embeddings import get_image_backend
    from app.services.preference_model import build_feature_space
    from app.services.presenter import Context
    from app.services.recommender import build_profile
    from app.services.settings_store import get_setting

    filters = feed_filters(brand=None, seller_id=None, category=None, material=None, price_currency="CNY")
    results = {}
    try:
        for stage in ("initial", "decision", "price", "deleted"):
            if stage == "decision":
                response = client.post("/api/feedback", json={"product_id": ids[-1], "action": "like"})
                response.raise_for_status()
            with SessionLocal() as db:
                if stage == "price":
                    db.get(Product, ids[-2]).price = 100
                    db.commit()
                if stage == "deleted":
                    db.delete(db.get(Product, ids[-3]))
                    db.commit()
                ranks, _, _, _ = ranked_candidates(db, build_profile(db), Context(db), filters)
                values = []
                for ranked in ranks:
                    ranked._explain()
                    values.append({f.name: ranked.product.id if f.name == "product" else getattr(ranked, f.name)
                                   for f in dataclasses.fields(ranked) if f.name != "explainer"})
                space = build_feature_space(db, get_setting(db, "taste_profile"), get_image_backend().name)
                result = {"ranked": len(values), "rankings_sha256": digest(values),
                          "features_sha256": hashlib.sha256(space.X.tobytes()).hexdigest()}
            for path in ("/api/feed", "/api/feed/learning"):
                response = client.get(path, params={"limit": 100})
                response.raise_for_status()
                answer = response.json()
                calibration = answer["gate"].get("calibration")
                if calibration:
                    calibration.pop("created_at", None)
                selected = {"gate": answer["gate"], "items": [
                    (i["id"], i["recommendation"], i.get("confidence"), i["family"]) for i in answer["items"]]}
                result[path] = digest(selected)
            results[stage] = result
    finally:
        client.__exit__(None, None, None)
    return {"synthetic": True, "products": n, "stages": results}


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(run(), indent=2) + "\n", encoding="utf-8")
