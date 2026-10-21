"""Duplicate detection and cross-seller matching.

Relation types (strongest first) - the UI keeps these clearly distinct:

* ``confirmed_identical``   you confirmed two listings are the same product.
* ``same_purchase_link``    both listings point at the same Weidian/Taobao item.
* ``shared_photos``         at least TWO different photographs match (perceptual hash + embedding).
                            Strong hint of the same source photos - still NOT proof of the same batch,
                            because sellers re-use factory photos.
* ``similar_photo``         exactly one photo matches. Shown as a weak hint only; it does not affect
                            value scores, groups or verdicts.
* ``visually_similar``      embeddings + text are close; may be a different product of a similar design.
* ``not_same``              you rejected a suggested match (never re-suggested).

Only garment photographs take part in photo matching: size charts, text graphics and banners a seller
reuses across listings (``image_select`` roles) would otherwise make unrelated products look identical.

Per-photo thresholds were chosen on a synthetic benchmark (audit, Oct 2026): ``phash <= 10`` and lite
cosine ``>= 0.97`` cut false photo matches by ~85% versus the old ``<= 6 / 0.90`` rule while finding more
re-posted photos (crop/resize/watermark/recompression). They should be re-tuned on real labelled pairs.
"""

from __future__ import annotations

import re
from collections import defaultdict

import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session

from ..ingestion.parsing import weidian_item_id
from ..models import Product, ProductImage, ProductRelation
from . import embeddings as emb
from .image_select import NON_AESTHETIC_ROLES

PHASH_MAX_DISTANCE = 10
# Perceptual hashes ignore colour, so the same silhouette in another colourway collides.
# A hash hit therefore also needs the per-image embeddings (which encode colour/content) to agree.
SAME_PHOTO_MIN_COSINE = {"lite": 0.97, "clip": 0.95}
MIN_PHOTOS_FOR_SHARED = 2
STRONG_RELATIONS = {"confirmed_identical", "same_purchase_link", "shared_photos"}


def _min_cosine(backend: str | None) -> float:
    return SAME_PHOTO_MIN_COSINE["clip" if (backend or "").startswith("clip") else "lite"]


def _popcount64(x: np.ndarray) -> np.ndarray:
    return np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(1)


class PhashIndex:
    """Exact nearest-photo search over all perceptual hashes (vectorised; fine for ~100k photos)."""

    def __init__(self) -> None:
        self._hashes: list[int] = []
        self._pids: list[int] = []
        self._image_ids: list[int] = []
        self.vectors: dict[int, np.ndarray] = {}
        self._arr: np.ndarray | None = None

    def add(self, product_id: int, h: str, image_id: int = 0, vector: np.ndarray | None = None) -> None:
        self._hashes.append(int(h, 16))
        self._pids.append(product_id)
        self._image_ids.append(image_id)
        if vector is not None:
            self.vectors[image_id] = vector
        self._arr = None

    def query(
        self, h: str, max_dist: int = PHASH_MAX_DISTANCE, vector: np.ndarray | None = None, min_cos: float = 0.97
    ) -> dict[int, list[tuple[int, int]]]:
        """Return {product_id: [(distance, matched image id), ...]} for near-identical photos."""
        if not self._hashes:
            return {}
        if self._arr is None:
            self._arr = np.array(self._hashes, dtype=np.uint64)
        dist = _popcount64(self._arr ^ np.uint64(int(h, 16)))
        out: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for i in np.nonzero(dist <= max_dist)[0]:
            image_id = self._image_ids[i]
            ov = self.vectors.get(image_id)
            if vector is not None and ov is not None and len(ov) == len(vector) and float(ov @ vector) < min_cos:
                continue  # same shape, different colours/content
            out[self._pids[i]].append((int(dist[i]), image_id))
        return out


def matchable(img: ProductImage) -> bool:
    return bool(img.phash) and img.role not in NON_AESTHETIC_ROLES


def build_phash_index(db: Session) -> PhashIndex:
    idx = PhashIndex()
    rows = db.query(ProductImage.product_id, ProductImage.phash, ProductImage.id, ProductImage.embedding).filter(
        ProductImage.phash.isnot(None),
        or_(ProductImage.role.is_(None), ProductImage.role.notin_(NON_AESTHETIC_ROLES)),
    )
    for pid, h, image_id, vec in rows:
        idx.add(pid, h, image_id, emb.from_bytes(vec))
    return idx


class GrowingMatrix:
    """Product embedding matrix that grows in place (amortised O(1) appends) and supports updates."""

    def __init__(self, mat: np.ndarray, ids: list[int]) -> None:
        dim = mat.shape[1] if len(ids) else 0
        self._buf = np.zeros((max(64, 2 * len(ids)), dim), dtype=np.float32)
        self._buf[: len(ids)] = mat if len(ids) else self._buf[:0]
        self.ids = list(ids)
        self.pos = {pid: i for i, pid in enumerate(ids)}

    def upsert(self, pid: int, v: np.ndarray) -> None:
        if self._buf.shape[1] == 0:
            self._buf = np.zeros((64, len(v)), dtype=np.float32)
        if pid in self.pos:
            self._buf[self.pos[pid]] = v
            return
        if len(self.ids) == len(self._buf):
            self._buf = np.vstack([self._buf, np.zeros_like(self._buf)])
        self.pos[pid] = len(self.ids)
        self._buf[len(self.ids)] = v
        self.ids.append(pid)

    def view(self) -> tuple[np.ndarray, list[int]]:
        return self._buf[: len(self.ids)], self.ids


class MatchContext:
    """Indexes built once per scan and extended as products are analysed (instead of once per product)."""

    def __init__(self, db: Session) -> None:
        self.idx = build_phash_index(db)
        self.matrices: dict[str | None, GrowingMatrix] = {}
        self.db = db

    def matrix(self, backend: str | None) -> GrowingMatrix:
        if backend not in self.matrices:
            self.matrices[backend] = GrowingMatrix(*embedding_matrix(self.db, backend))
        return self.matrices[backend]

    def match(self, p: Product) -> int:
        m = self.matrix(p.embedding_backend)
        n = match_product(self.db, p, phash_index=self.idx, matrix=m.view())
        for img in p.images:
            if matchable(img):
                self.idx.add(p.id, img.phash, img.id, emb.from_bytes(img.embedding))
        if p.image_embedding is not None:
            m.upsert(p.id, emb.from_bytes(p.image_embedding))
        return n


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


RANK = ["visually_similar", "similar_photo", "shared_photos", "same_purchase_link", "confirmed_identical"]


def _upsert_relation(db: Session, a: int, b: int, relation: str, score: float, signals: dict) -> None:
    a, b = min(a, b), max(a, b)
    rel = db.query(ProductRelation).filter_by(a_id=a, b_id=b).one_or_none()
    if rel is None:
        db.add(ProductRelation(a_id=a, b_id=b, relation=relation, score=score, signals=signals))
        return
    if rel.user_confirmed is not None:  # never override your decision
        return
    if rel.relation in RANK and relation in RANK and RANK.index(relation) < RANK.index(rel.relation):
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
    db.flush()
    idx = phash_index or build_phash_index(db)
    min_cos = _min_cosine(p.embedding_backend)
    # For each other product: which of MY photos matched which of THEIR photos
    pairs: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for img in p.images:
        if matchable(img):
            for pid, matches in idx.query(img.phash, vector=emb.from_bytes(img.embedding), min_cos=min_cos).items():
                if pid != p.id:
                    pairs[pid].extend((d, img.id, other_img) for d, other_img in matches)

    my_ids = _purchase_ids(p)
    for pid_key in my_ids:
        raw_id = pid_key.split(":", 1)[1]
        # Narrow by text search on the stored links JSON, then verify exactly.
        for other in db.query(Product).filter(Product.id != p.id, cast(Product.links, String).contains(raw_id)):
            shared = my_ids & _purchase_ids(other)
            if shared:
                _upsert_relation(db, p.id, other.id, "same_purchase_link", 1.0, {"purchase_ids": sorted(shared)})
                written += 1

    for pid, matches in pairs.items():
        # Count DISTINCT photos on both sides: one photo matching twice is still one photo.
        distinct = min(len({m[1] for m in matches}), len({m[2] for m in matches}))
        best = min(m[0] for m in matches)
        relation = "shared_photos" if distinct >= MIN_PHOTOS_FOR_SHARED else "similar_photo"
        score = min(1.0, 0.5 + 0.15 * distinct - 0.02 * best)
        _upsert_relation(db, p.id, pid, relation, round(score, 3), {"matching_photos": distinct, "best_hamming": best})
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
                if other_id == p.id or other_id in pairs or sims[i] < thr:
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
        ra, rb = find(rel.a_id), find(rel.b_id)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    want = {pid: find(pid) for pid in parent}
    have = dict(db.query(Product.id, Product.group_id).filter(Product.group_id.isnot(None)))
    changes = [{"id": pid, "group_id": g} for pid, g in want.items() if have.get(pid) != g]
    changes += [{"id": pid, "group_id": None} for pid in have if pid not in want]
    if changes:  # write only rows whose group changed
        db.bulk_update_mappings(Product, changes)
    db.flush()
    return len(set(want.values()))


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
