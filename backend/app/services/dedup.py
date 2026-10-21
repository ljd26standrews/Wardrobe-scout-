"""Duplicate detection and cross-seller matching.

Relation types (strongest first) - the UI keeps these clearly distinct:

* ``confirmed_identical``   you confirmed two listings are the same product.
* ``same_purchase_link``    both listings point at the same Weidian/Taobao item.
* ``shared_photos``         listings use near-identical photographs (perceptual
                            hash). Strong sign of the same source / batch, but
                            NOT proof: sellers re-use factory photos.
* ``visually_similar``      embeddings + text are close; may be a different
                            product of a similar design.
* ``not_same``              you rejected a suggested match (never re-suggested).
"""

from __future__ import annotations

import re
from collections import defaultdict

import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..ingestion.parsing import weidian_item_id
from ..models import Product, ProductImage, ProductRelation
from . import embeddings as emb
from .images import hamming

PHASH_MAX_DISTANCE = 6
STRONG_RELATIONS = {"confirmed_identical", "same_purchase_link", "shared_photos"}


def _chunks(h: str) -> list[str]:
    # 64-bit hash -> 8 chunks of 8 bits; distance <= 7 guarantees one exact chunk match.
    return [f"{i}:{h[i * 2 : (i + 1) * 2]}" for i in range(len(h) // 2)]


# Perceptual hashes ignore colour, so the same silhouette in another colourway collides.
# A phash hit therefore also needs the per-image embeddings (which encode colour) to agree.
SAME_PHOTO_MIN_COSINE = 0.9


class PhashIndex:
    def __init__(self) -> None:
        self.buckets: dict[str, set[tuple[int, str, int]]] = defaultdict(set)
        self.vectors: dict[int, np.ndarray] = {}

    def add(self, product_id: int, h: str, image_id: int = 0, vector: np.ndarray | None = None) -> None:
        for c in _chunks(h):
            self.buckets[c].add((product_id, h, image_id))
        if vector is not None:
            self.vectors[image_id] = vector

    def query(self, h: str, max_dist: int = PHASH_MAX_DISTANCE, vector: np.ndarray | None = None) -> dict[int, int]:
        """Return {product_id: best distance} for near-identical photos."""
        cands: set[tuple[int, str, int]] = set()
        for c in _chunks(h):
            cands |= self.buckets.get(c, set())
        out: dict[int, int] = {}
        for pid, other, image_id in cands:
            d = hamming(h, other)
            if d > max_dist or d >= out.get(pid, 99):
                continue
            ov = self.vectors.get(image_id)
            if (
                vector is not None
                and ov is not None
                and len(ov) == len(vector)
                and float(ov @ vector) < SAME_PHOTO_MIN_COSINE
            ):
                continue  # same shape, different colours/content
            out[pid] = d
        return out


def build_phash_index(db: Session) -> PhashIndex:
    idx = PhashIndex()
    rows = db.query(ProductImage.product_id, ProductImage.phash, ProductImage.id, ProductImage.embedding).filter(
        ProductImage.phash.isnot(None)
    )
    for pid, h, image_id, vec in rows:
        idx.add(pid, h, image_id, emb.from_bytes(vec))
    return idx


_STRIP = re.compile(r"[\[【(（].*?[\]】)）]|[¥￥]\s*\d+|\bp\d+\b|\d{3,}|#\w+", re.I)


def clean_title(t: str | None) -> str:
    return re.sub(r"\s+", " ", _STRIP.sub(" ", (t or "").lower())).strip()


def _purchase_ids(p: Product) -> set[str]:
    ids = set()
    for k, u in (p.links or {}).items():
        if k == "weidian" and (i := weidian_item_id(u)):
            ids.add(f"weidian:{i}")
        elif k == "taobao" and (m := re.search(r"[?&]id=(\d+)", u or "")):
            ids.add(f"taobao:{m.group(1)}")
    return ids


def _upsert_relation(db: Session, a: int, b: int, relation: str, score: float, signals: dict) -> None:
    a, b = min(a, b), max(a, b)
    rel = db.query(ProductRelation).filter_by(a_id=a, b_id=b).one_or_none()
    rank = ["visually_similar", "shared_photos", "same_purchase_link", "confirmed_identical"]
    if rel is None:
        db.add(ProductRelation(a_id=a, b_id=b, relation=relation, score=score, signals=signals))
        return
    if rel.user_confirmed is not None:  # never override your decision
        return
    if rel.relation in rank and relation in rank and rank.index(relation) < rank.index(rel.relation):
        return
    rel.relation, rel.score, rel.signals = relation, score, signals


def match_product(
    db: Session,
    p: Product,
    *,
    phash_index: PhashIndex | None = None,
    matrix: tuple[np.ndarray, list[int]] | None = None,
) -> int:
    """Find relations for one product. Returns number of relations written."""
    written = 0
    # Recompute suggestions from scratch; your confirmations/rejections are always kept.
    db.query(ProductRelation).filter(
        or_(ProductRelation.a_id == p.id, ProductRelation.b_id == p.id), ProductRelation.user_confirmed.is_(None)
    ).delete(synchronize_session=False)
    idx = phash_index or build_phash_index(db)
    hits: dict[int, list[int]] = defaultdict(list)
    for img in p.images:
        if img.phash:
            for pid, d in idx.query(img.phash, vector=emb.from_bytes(img.embedding)).items():
                if pid != p.id:
                    hits[pid].append(d)

    my_ids = _purchase_ids(p)
    if my_ids:
        for other in db.query(Product).filter(Product.id != p.id, Product.links.isnot(None)):
            shared = my_ids & _purchase_ids(other)
            if shared:
                _upsert_relation(db, p.id, other.id, "same_purchase_link", 1.0, {"purchase_ids": sorted(shared)})
                written += 1

    for pid, dists in hits.items():
        n = len(dists)
        score = min(1.0, 0.55 + 0.15 * n - 0.02 * min(dists))
        _upsert_relation(
            db, p.id, pid, "shared_photos", round(score, 3), {"matching_photos": n, "best_hamming": min(dists)}
        )
        written += 1

    # Visual + text similarity
    v = emb.from_bytes(p.image_embedding)
    if v is not None:
        if matrix is None:
            matrix = embedding_matrix(db, p.embedding_backend)
        mat, ids = matrix
        if len(ids):
            sims = mat @ v
            thr = 0.90 if (p.embedding_backend or "").startswith("clip") else 0.93
            my_title = clean_title(p.title_en)
            for i in np.argsort(-sims)[:25]:
                other_id = ids[i]
                if other_id == p.id or other_id in hits or sims[i] < thr:
                    continue
                other = db.get(Product, other_id)
                if other is None or (other.category and p.category and other.category != p.category):
                    continue
                t = fuzz.token_set_ratio(my_title, clean_title(other.title_en)) / 100
                score = 0.7 * float(sims[i]) + 0.3 * t
                _upsert_relation(
                    db,
                    p.id,
                    other_id,
                    "visually_similar",
                    round(score, 3),
                    {"visual": round(float(sims[i]), 3), "title": round(t, 2)},
                )
                written += 1
    db.flush()
    return written


def embedding_matrix(db: Session, backend: str | None) -> tuple[np.ndarray, list[int]]:
    rows = (
        db.query(Product.id, Product.image_embedding)
        .filter(Product.image_embedding.isnot(None), Product.embedding_backend == backend)
        .all()
    )
    if not rows:
        return np.zeros((0, 1), dtype=np.float32), []
    return np.vstack([emb.from_bytes(r[1]) for r in rows]), [r[0] for r in rows]


def recompute_groups(db: Session) -> int:
    """Union-find over strong relations -> Product.group_id."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for rel in db.query(ProductRelation).filter(ProductRelation.relation.in_(STRONG_RELATIONS)):
        if rel.user_confirmed is False:
            continue
        if rel.relation == "shared_photos" and rel.signals.get("matching_photos", 0) < 2 and not rel.user_confirmed:
            continue
        ra, rb = find(rel.a_id), find(rel.b_id)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    db.query(Product).update({Product.group_id: None})
    groups = 0
    for pid in list(parent):
        root = find(pid)
        db.query(Product).filter(Product.id == pid).update({Product.group_id: root})
    groups = len({find(x) for x in parent})
    db.flush()
    return groups


def match_all(db: Session) -> dict:
    idx = build_phash_index(db)
    matrices: dict[str | None, tuple] = {}
    n = 0
    for p in db.query(Product).all():
        if p.embedding_backend not in matrices:
            matrices[p.embedding_backend] = embedding_matrix(db, p.embedding_backend)
        n += match_product(db, p, phash_index=idx, matrix=matrices[p.embedding_backend])
    groups = recompute_groups(db)
    db.commit()
    return {"relations_written": n, "groups": groups}


def relations_for(db: Session, product_id: int) -> list[tuple[ProductRelation, int]]:
    rels = (
        db.query(ProductRelation)
        .filter(or_(ProductRelation.a_id == product_id, ProductRelation.b_id == product_id))
        .all()
    )
    return [(r, r.b_id if r.a_id == product_id else r.a_id) for r in rels if r.relation != "not_same"]


def set_user_decision(db: Session, a: int, b: int, same: bool) -> ProductRelation:
    a, b = min(a, b), max(a, b)
    rel = db.query(ProductRelation).filter_by(a_id=a, b_id=b).one_or_none()
    if rel is None:
        rel = ProductRelation(a_id=a, b_id=b, relation="visually_similar", score=0, signals={})
        db.add(rel)
    rel.user_confirmed = same
    rel.relation = "confirmed_identical" if same else "not_same"
    db.flush()
    recompute_groups(db)
    db.commit()
    return rel
