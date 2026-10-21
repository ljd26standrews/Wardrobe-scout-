"""Score a scanned database against the benchmark catalogue's ground truth."""

from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np

from .render import label_of

STRONG = {"shared_photos", "same_purchase_link", "confirmed_identical"}


def key_of(url: str) -> str | None:
    parts = url.split("photo.yupoo.com/", 1)
    if len(parts) != 2:
        return None
    bits = parts[1].split("/")
    return bits[1] if len(bits) >= 2 else None


def contributing(img) -> bool:
    """Did this image feed the product's aesthetic vector?"""
    if img.embedding is None:
        return False
    sel = getattr(img, "selected", None)
    return True if sel is None else bool(sel)


def score(db, spec: dict) -> dict:
    from app.models import Product, ProductRelation, Seller

    kinds = {k: v["kind"] for k, v in spec["images"].items()}
    sellers = {s.id: s.slug for s in db.query(Seller) if s.slug in spec["albums"]}
    truth = {(s, a["id"]): a for s, al in spec["albums"].items() for a in al}
    prods = [
        p
        for p in db.query(Product).filter(Product.seller_id.in_(sellers))
        if (sellers[p.seller_id], p.external_id) in truth
    ]
    by_key = {(sellers[p.seller_id], p.external_id): p for p in prods}
    gone = [
        p
        for p in db.query(Product).filter(Product.seller_id.in_(sellers), Product.data_origin == "scraped")
        if (sellers[p.seller_id], p.external_id) not in truth
    ]
    out: dict = {
        "catalogue_products": len(truth),
        "products_found": len(prods),
        "products_active": sum(p.status == "active" for p in prods),
        "products_with_image_vector": sum(p.image_embedding is not None for p in prods),
        "removed_from_catalogue": len(gone),
        "removed_still_active": sum(p.status == "active" for p in gone),
    }
    states = Counter(getattr(p, "analysis_state", None) or "n/a" for p in prods)
    out["analysis_states"] = dict(states)

    # ---- image use and contamination
    contrib_kinds: Counter = Counter()
    contaminated = cover_bad = no_garment = 0
    n_contrib = []
    stored_images = 0
    for p in prods:
        imgs = list(p.images)
        stored_images += sum(1 for i in imgs if i.local_path)
        c = [i for i in imgs if contributing(i)]
        if p.image_embedding is None:
            continue
        n_contrib.append(len(c))
        labs = [label_of(kinds.get(key_of(i.url) or "", "hero")) for i in c]
        for i in c:
            contrib_kinds[kinds.get(key_of(i.url) or "", "?")] += 1
        if any(lab != "garment" for lab in labs):
            contaminated += 1
        if c and labs[0] != "garment":
            cover_bad += 1
        if not any(lab == "garment" for lab in labs):
            no_garment += 1
    nv = max(1, len(n_contrib))
    total_contrib = sum(contrib_kinds.values())
    non_g = sum(v for k, v in contrib_kinds.items() if label_of(k) != "garment")
    out["vector_images_per_product"] = round(float(np.mean(n_contrib)) if n_contrib else 0, 2)
    out["products_vector_contaminated_pct"] = round(100 * contaminated / nv, 1)
    out["products_vector_first_image_not_garment_pct"] = round(100 * cover_bad / nv, 1)
    out["products_vector_without_any_garment_pct"] = round(100 * no_garment / nv, 1)
    out["vector_images_non_garment_pct"] = round(100 * non_g / max(1, total_contrib), 1)
    out["vector_images_by_kind"] = dict(contrib_kinds.most_common())
    out["stored_image_files"] = stored_images

    # ---- size-chart classification (if the scanner labels image roles)
    roles = Counter()
    conf = defaultdict(Counter)
    for p in prods:
        for i in p.images:
            role = getattr(i, "role", None)
            if role is None:
                continue
            truth_lab = label_of(kinds.get(key_of(i.url) or "", "hero"))
            conf[truth_lab][role] += 1
            roles[role] += 1
    if roles:
        out["role_confusion"] = {k: dict(v) for k, v in conf.items()}

    # ---- deduplication against ground truth
    rels = {}
    for r in db.query(ProductRelation):
        rels[(min(r.a_id, r.b_id), max(r.a_id, r.b_id))] = r.relation
    repost_pairs, colour_pairs = set(), set()
    for (s, aid), a in truth.items():
        p = by_key.get((s, aid))
        if p is None:
            continue
        for field, bucket in (("repost_of", repost_pairs), ("colourway_of", colour_pairs)):
            if a.get(field):
                q = by_key.get(tuple(a[field]))
                if q is not None:
                    bucket.add((min(p.id, q.id), max(p.id, q.id)))
    prod_ids = {p.id for p in prods}
    photo_rel = {
        k: v for k, v in rels.items() if v in STRONG | {"similar_photo"} and k[0] in prod_ids and k[1] in prod_ids
    }
    out["dedup"] = {
        "true_duplicate_pairs": len(repost_pairs),
        "found_strong": sum(rels.get(k) in STRONG for k in repost_pairs),
        "found_any_photo_match": sum(k in photo_rel for k in repost_pairs),
        "colourway_pairs": len(colour_pairs),
        "colourway_wrongly_photo_matched": sum(k in photo_rel for k in colour_pairs),
        "colourway_wrongly_strong": sum(rels.get(k) in STRONG for k in colour_pairs),
        "false_strong_other": sum(1 for k, v in photo_rel.items() if v in STRONG and k not in repost_pairs),
        "false_photo_match_other": sum(1 for k in photo_rel if k not in repost_pairs),
    }

    # ---- visual neighbourhood quality: leave-one-out 5-NN class agreement (twins excluded)
    vec_prods = [p for p in prods if p.image_embedding is not None]
    if len(vec_prods) > 10:
        M = np.vstack([np.frombuffer(p.image_embedding, dtype=np.float32) for p in vec_prods])
        M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
        cls = [truth[(sellers[p.seller_id], p.external_id)]["cls"] for p in vec_prods]
        photo0 = [truth[(sellers[p.seller_id], p.external_id)]["photos"][0] for p in vec_prods]
        S = M @ M.T
        hits = 0
        for i in range(len(vec_prods)):
            order = np.argsort(-S[i])
            nn = [j for j in order if j != i and photo0[j] != photo0[i]][:5]
            hits += Counter(cls[j] for j in nn).most_common(1)[0][0] == cls[i]
        out["visual_5nn_class_accuracy"] = round(hits / len(vec_prods), 3)
    return out
