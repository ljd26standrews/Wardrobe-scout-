"""Side-by-side table of benchmark results (baseline vs optimised).

Usage: .venv/bin/python -m bench.compare base300.json opt300.json [...]
"""

from __future__ import annotations

import json
import sys


def get(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


ROWS = [
    ("Products in catalogue", "accuracy.catalogue_products"),
    ("Products found", "accuracy.products_found"),
    ("Products with a photo vector", "accuracy.products_with_image_vector"),
    ("HTTP requests", "scan1.requests_total"),
    ("  per product", "scan1.requests_per_product"),
    ("  catalogue/product pages", "scan1.page_requests"),
    ("  photos", "scan1.image_requests"),
    ("Photo MB downloaded", "scan1.image_mb"),
    ("Min. time at real 2 s/host (s)", "_floor"),
    ("Measured wall time at scale (s)", "scan1.wall_s"),
    ("CPU time (s)", "scan1.cpu_s"),
    ("Peak memory (MB)", "peak_rss_mb"),
    ("Dedup time (s)", "timers_s.dedup_match"),
    ("Photos feeding vectors / product", "accuracy.vector_images_per_product"),
    ("Vectors with a size chart/text (%)", "accuracy.products_vector_contaminated_pct"),
    ("Vector photos not garments (%)", "accuracy.vector_images_non_garment_pct"),
    ("True duplicates found (strong)", "_dups"),
    ("Colourways wrongly matched", "accuracy.dedup.colourway_wrongly_photo_matched"),
    ("False strong matches (unrelated)", "accuracy.dedup.false_strong_other"),
    ("False weak matches (unrelated)", "accuracy.dedup.false_photo_match_other"),
    ("Visual 5-NN class accuracy", "accuracy.visual_5nn_class_accuracy"),
    ("Shopper P@10 (mean of 2)", "recommendation.p10_mean_all"),
    ("Repeat scan: requests", "scan2.requests_total"),
    ("Repeat scan: photo MB", "scan2.image_mb"),
    ("Repeat scan: min. time at 2 s/host (s)", "_floor2"),
    ("Repeat scan: products found", "accuracy2.products_found"),
    ("Repeat scan: catalogue products", "accuracy2.catalogue_products"),
    ("Repeat scan: removed products still active", "_removed_active"),
]


def value(r: dict, path: str):
    if path == "_floor":
        f = get(r, "scan1.politeness_floor_s")
        if not f:
            return None
        return f["if_hosts_serial"] if r["label"] == "baseline" else f["if_hosts_parallel"]
    if path == "_floor2":
        f = get(r, "scan2.politeness_floor_s")
        if not f:
            return None
        return f["if_hosts_serial"] if r["label"] == "baseline" else f["if_hosts_parallel"]
    if path == "_dups":
        d = get(r, "accuracy.dedup")
        return f"{d['found_strong']}/{d['true_duplicate_pairs']}" if d else None
    if path == "_removed_active":
        return get(r, "accuracy2.removed_still_active")
    return get(r, path)


def main(paths: list[str]) -> None:
    rs = [json.load(open(p)) for p in paths]
    head = ["metric"] + [f"{r['label']} {r['catalogue']['products']}" for r in rs]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for label, path in ROWS:
        vals = [value(r, path) for r in rs]
        if all(v is None for v in vals):
            continue
        print("| " + " | ".join([label] + ["–" if v is None else str(v) for v in vals]) + " |")


if __name__ == "__main__":
    main(sys.argv[1:])
