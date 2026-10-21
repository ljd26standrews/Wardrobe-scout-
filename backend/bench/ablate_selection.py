"""Offline ablation: how many / which photos should feed a product's aesthetic vector?

For every image of a benchmark catalogue (640px "medium" variant, as a scan downloads), compute the lite
embedding and the cheap analysis features once, then compare strategies:

* ``baseline``          first 12 images, hero weighted x2 (original pipeline; includes size charts)
* ``first_k``           first k images, no filtering
* ``all_garment``       every image the classifier keeps (ideal-but-expensive reference)
* ``select_k``          diverse selection (image_select.select) over ALL images, max k
* ``progressive_k``     fetch in gallery order and stop early once k useful, non-redundant views are found
                        (or ``max_examine`` images were fetched); then select. This is what a scan does.

Metrics
* ``knn``    leave-one-out 5-NN garment-class agreement (twins sharing the photo excluded)
* ``taste``  how learnable a shopper's taste is: PCA(16) + logistic regression trained on 60 labelled
             products, ROC-AUC on the rest, mean of 30 draws x 2 shoppers (bench.quality personas)
* ``fetched`` images downloaded per product; ``contaminated`` % of vectors including a non-garment image

Usage: .venv/bin/python -m bench.ablate_selection --products 1000
"""

from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image
from sklearn.decomposition import PCA
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from app.services import image_select as sel
from app.services.embeddings import LiteImageBackend

from . import catalogue, quality, render


def image_table(spec: dict, cache: Path) -> dict:
    if cache.exists():
        d = np.load(cache, allow_pickle=True)
        return d["table"].item()
    backend = LiteImageBackend()
    table = {}
    keys = {im["key"] for al in spec["albums"].values() for a in al for im in a["images"]}
    t0 = time.time()
    for i, key in enumerate(sorted(keys)):
        data = render.variant_bytes(spec["images"][key], "medium")
        img = Image.open(io.BytesIO(data)).convert("RGB")
        f = sel.analyse(img)
        role, _ = sel.classify(f)
        table[key] = {"vec": backend.embed(img), "role": role, "f": f, "kind": spec["images"][key]["kind"]}
        if i % 500 == 0:
            print(f"  embedded {i}/{len(keys)} ({time.time() - t0:.0f}s)", flush=True)
    np.save(cache, np.array(table, dtype=object), allow_pickle=True)
    return table


def weighted(vecs: list[np.ndarray]) -> np.ndarray:
    return sel.product_vector(vecs)


def strategy(name: str, k: int, album: dict, T: dict, max_examine: int) -> tuple[np.ndarray | None, int, bool]:
    keys = [im["key"] for im in album["images"]]
    if name == "baseline":
        use = keys[:12]
        return (
            weighted([T[x]["vec"] for x in use]),
            len(use),
            any(render.label_of(T[x]["kind"]) != "garment" for x in use),
        )
    if name == "first_k":
        use = keys[:k]
        return (
            weighted([T[x]["vec"] for x in use]),
            len(use),
            any(render.label_of(T[x]["kind"]) != "garment" for x in use),
        )
    examined = keys
    if name == "progressive":
        examined, useful = [], []
        for x in keys:
            if len(examined) >= max_examine:
                break
            examined.append(x)
            if T[x]["role"] in sel.AESTHETIC_ROLES and all(float(T[x]["vec"] @ T[u]["vec"]) < 0.97 for u in useful):
                useful.append(x)
            if len(useful) >= k:
                break
    cands = [sel.Candidate(i, T[x]["role"], sel.quality(T[x]["f"], i), T[x]["vec"]) for i, x in enumerate(examined)]
    if name == "all_garment":
        idx = [c.index for c in cands if c.role in sel.AESTHETIC_ROLES]
    else:
        idx = sel.select(cands, max_k=k)
    if not idx:
        return None, len(examined), False
    use = [examined[i] for i in idx]
    return (
        weighted([T[x]["vec"] for x in use]),
        len(examined),
        any(render.label_of(T[x]["kind"]) != "garment" for x in use),
    )


def knn_acc(M: np.ndarray, cls: list[str], photo: list[str]) -> float:
    Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    S = Mn @ Mn.T
    hits = 0
    for i in range(len(cls)):
        order = np.argsort(-S[i])
        nn = [j for j in order if j != i and photo[j] != photo[i]][:5]
        vals, counts = np.unique([cls[j] for j in nn], return_counts=True)
        hits += vals[counts.argmax()] == cls[i]
    return hits / len(cls)


def taste_auc(M: np.ndarray, util: np.ndarray, draws: int = 30, n_train: int = 60) -> float:
    rng = np.random.default_rng(0)
    y = (util >= np.quantile(util, 0.8)).astype(int)
    Z = PCA(16, random_state=0).fit_transform(M)
    aucs = []
    for _ in range(draws):
        idx = rng.permutation(len(y))
        tr, te = idx[:n_train], idx[n_train:]
        if y[tr].min() == y[tr].max():
            continue
        clf = LogisticRegression(C=1.0, max_iter=500).fit(Z[tr], y[tr])
        aucs.append(roc_auc_score(y[te], clf.decision_function(Z[te])))
    return float(np.mean(aucs))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-examine", type=int, default=6)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    spec = catalogue.build(args.products, 3, args.seed)
    cache = Path(args.cache or f"/tmp/ws_ablate_{args.products}_{args.seed}.npy")
    T = image_table(spec, cache)
    albums = [a for al in spec["albums"].values() for a in al]
    cls = [a["cls"] for a in albums]
    photo = [a["photos"][0] for a in albums]
    utils = [np.array([quality.utilities(spec, p)[(a["seller"], a["id"])] for a in albums]) for p in quality.PERSONAS]
    rows = []
    plan = [("baseline", 12)] + [("first_k", k) for k in (1, 2, 3)] + [("all_garment", 99)]
    plan += [("select", k) for k in (1, 2, 3, 4, 6)] + [("progressive", k) for k in (1, 2, 3, 4)]
    for name, k in plan:
        vecs, fetched, contaminated = [], [], 0
        for a in albums:
            v, n, c = strategy(name, k, a, T, args.max_examine)
            vecs.append(v if v is not None else np.zeros(len(next(iter(T.values()))["vec"]), dtype=np.float32))
            fetched.append(n)
            contaminated += c
        M = np.vstack(vecs)
        row = {
            "strategy": name if name in ("baseline", "all_garment") else f"{name}_{k}",
            "fetched_per_product": round(float(np.mean(fetched)), 2),
            "contaminated_pct": round(100 * contaminated / len(albums), 1),
            "knn": round(knn_acc(M, cls, photo), 3),
            "taste_auc": round(float(np.mean([taste_auc(M, u) for u in utils])), 3),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
