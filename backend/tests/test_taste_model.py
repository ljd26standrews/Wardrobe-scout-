"""Taste-model scope: what a decision may and may not teach (audit findings R1-R3, family evidence)."""

from __future__ import annotations

import numpy as np


def _seller(db, slug="tm"):
    from app.models import Seller

    s = Seller(name=slug, slug=slug, platform="manual")
    db.add(s)
    db.commit()
    return s


def _product(db, seller, ext, title, vec_seed=None):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.services import embeddings as emb

    p = upsert_listing(db, seller, RawListing(ext, f"https://shop.example/{ext}", title)).product
    if vec_seed is not None:
        v = np.random.default_rng(vec_seed).standard_normal(192).astype(np.float32)
        p.image_embedding = emb.to_bytes(emb.normalise(v))
        p.embedding_backend = emb.get_image_backend().name
    db.commit()
    return p


def _space(db):
    from app.services import embeddings as emb
    from app.services.preference_model import build_feature_space
    from app.services.settings_store import get_setting

    return build_feature_space(db, get_setting(db, "taste_profile"), emb.get_image_backend().name)


def _fit(db):
    from app.services import preference_model as pm

    space = _space(db)
    return space, pm.fit(db, space)


def _feedback(db, p, action, reasons=()):
    from app.models import Feedback
    from app.services.recommender import snapshot

    db.add(Feedback(product_id=p.id, action=action, reasons=list(reasons), snapshot=snapshot(p)))
    db.commit()


def test_small_catalogue_still_has_visual_features(env, db):
    s = _seller(db)
    for i in range(3):
        _product(db, s, f"r{i}", f"wool coat {i}", vec_seed=i)
    space = _space(db)
    assert np.abs(space.X[:, space.img_slice]).sum() > 0


def test_colour_only_dislike_moves_only_colour(env, db):
    s = _seller(db)
    red = _product(db, s, "red", "red wool coat")
    for i in range(30):
        _product(db, s, f"n{i}", f"navy cotton trousers {i}")
    _feedback(db, red, "dislike", ["colour"])
    space, post = _fit(db)
    moved = {
        space.feature_name(j): round(float(post.mean[j]), 4)
        for j in range(1, space.img_slice.start)
        if abs(post.mean[j]) > 0.01 and space.feature_name(j)[0] != "colour"
    }
    assert not moved, moved
    assert post.mean[1 + space.vocab[("colour", "red")]] < -0.05


def test_colour_dislike_alongside_a_love_still_moves_only_colour(env, db):
    """With a positive present the background sample is active: the dislike's own effect stays on colour."""
    s = _seller(db)
    loved = _product(db, s, "lv", "black cashmere hoodie")
    red = _product(db, s, "red", "red wool coat")
    for i in range(30):
        _product(db, s, f"n{i}", f"navy cotton trousers {i}")
    _feedback(db, loved, "love")
    space, before = _fit(db)
    _feedback(db, red, "dislike", ["colour"])
    space, after = _fit(db)
    attrs = range(1, space.img_slice.start)
    shift = {space.feature_name(j): float(after.mean[j] - before.mean[j]) for j in attrs}
    others = {k: round(v, 4) for k, v in shift.items() if k[0] != "colour" and abs(v) > 0.01}
    assert not others, others
    assert shift[("colour", "red")] < -0.05


def test_specific_item_rejection_teaches_no_general_style(env, db):
    s = _seller(db)
    ps = [_product(db, s, f"si{i}", f"wool coat {i}", vec_seed=i) for i in range(25)]
    _feedback(db, ps[0], "dislike", ["specific_item"])
    space, post = _fit(db)
    assert np.abs(post.mean[space.img_slice]).max() < 1e-3
    assert post.n_obs == 0  # nothing general was learned from it


def test_one_garment_listed_by_several_sellers_counts_once(env, db):
    from app.services import preference_model as pm

    s1, s2, s3 = _seller(db, "f1"), _seller(db, "f2"), _seller(db, "f3")
    a = _product(db, s1, "fa", "olive wool overcoat")
    b = _product(db, s2, "fb", "olive wool overcoat")
    c = _product(db, s3, "fc", "olive wool overcoat")  # a third listing you have not decided on
    for i in range(12):
        _product(db, s1, f"o{i}", f"navy cotton trousers {i}")
    for p in (a, b, c):
        p.group_id = a.id  # the same garment (e.g. same purchase link)
    db.commit()
    _feedback(db, a, "love")
    _feedback(db, b, "love")
    space = _space(db)
    rows, ys, ws, n_real = pm.observations(db, space)
    assert n_real == 1
    decision_weight = sum(w for w, y in zip(ws, ys, strict=True) if y == 1)
    assert abs(decision_weight - 2.0) < 1e-9  # one love, not two
    row_c = space.X[space.row[c.id]]
    assert not any(np.array_equal(r, row_c) and y == 0 for r, y in zip(rows, ys, strict=True))
