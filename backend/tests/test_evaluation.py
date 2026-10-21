"""Evaluation workflow: unbiased labelling (Evaluate page API) and the offline evaluator (eval/real.py).

Grades in these tests are synthetic fixtures standing in for a person's judgements; the app itself never
creates judgements."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH

BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.setenv("WS_DOWNLOAD_IMAGES", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def _catalogue(db, n_per_seller=10, sellers=("s1", "s2", "s3"), colours=("navy", "red", "grey", "black")):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller
    from app.services import embeddings as emb

    out = []
    backend = emb.get_image_backend().name
    for si, slug in enumerate(sellers):
        s = Seller(name=slug, slug=slug, platform="manual")
        db.add(s)
        db.flush()
        for i in range(n_per_seller):
            colour = colours[(i + si) % len(colours)]
            raw = RawListing(f"{slug}-{i}", f"https://shop.example/{slug}/{i}", f"{colour} wool coat {slug} {i}",
                             price=300 + 10 * i)  # fmt: skip
            p = upsert_listing(db, s, raw).product
            v = np.random.default_rng(si * 100 + i).standard_normal(192).astype(np.float32)
            p.image_embedding, p.embedding_backend = emb.to_bytes(emb.normalise(v)), backend
            out.append(p)
    db.commit()
    return out


def test_pieces_to_judge_are_undecided_readable_one_per_family_and_blinded(client):
    from app.db import SessionLocal
    from app.models import Feedback

    with SessionLocal() as db:
        ps = _catalogue(db, n_per_seller=3, sellers=("a1", "a2"))
        ps[0].group_id = ps[1].group_id = ps[0].id  # one garment, two listings
        ps[2].price = None  # not readable: no price
        db.add(Feedback(product_id=ps[3].id, action="love"))  # already decided in Discover
        db.commit()
        ids = [p.id for p in ps]
    first = client.get("/api/eval/next").json()["product"]
    assert client.get("/api/eval/next").json()["product"]["id"] == first["id"]  # stable, not re-drawn
    assert not {"seller", "recommendation", "quality", "pros", "cons"} & set(first)  # blinded
    judged = []
    while (item := client.get("/api/eval/next").json()["product"]) is not None:
        r = client.post("/api/eval/judgements", json={"product_id": item["id"], "taste": 2})
        assert r.status_code == 200, r.text
        judged.append(item["id"])
    assert ids[2] not in judged and ids[3] not in judged
    assert len({ids[0], ids[1]} & set(judged)) == 1  # one per family
    assert set(judged) == set(ids) - {ids[2], ids[3]} - ({ids[0], ids[1]} - set(judged))


def test_taste_is_recorded_apart_from_purchase_objections(client):
    from app.db import SessionLocal
    from app.models import Judgement

    with SessionLocal() as db:
        _catalogue(db, n_per_seller=2, sellers=("b1",))
    pid = client.get("/api/eval/next").json()["product"]["id"]
    assert client.post("/api/eval/judgements", json={"product_id": pid, "taste": 5}).status_code == 422
    r = client.post("/api/eval/judgements", json={"product_id": pid, "taste": 3, "too_expensive": True,
                                                  "owned_similar": True})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"]["judgements"] == 1
    assert client.post("/api/eval/judgements", json={"product_id": pid, "taste": 1}).status_code == 409
    with SessionLocal() as db:
        j = db.query(Judgement).one()
        assert (j.taste, j.too_expensive, j.owned_similar, j.quality_unclear) == (3, True, True, False)
    assert client.delete(f"/api/eval/judgements/{r.json()['id']}").json()["status"]["judgements"] == 0


def test_judgements_survive_backup_and_restore(client):
    from app.db import SessionLocal
    from app.models import Judgement
    from app.services.backup import export_all, import_all

    with SessionLocal() as db:
        _catalogue(db, n_per_seller=2, sellers=("c1",))
    pid = client.get("/api/eval/next").json()["product"]["id"]
    client.post("/api/eval/judgements", json={"product_id": pid, "taste": 3, "quality_unclear": True})
    with SessionLocal() as db:
        data = export_all(db)
        assert [j["taste"] for j in data["judgements"]] == [3]
        db.query(Judgement).delete()
        db.commit()
        assert import_all(db, data)["judgements"] == 1
        assert import_all(db, data)["judgements"] == 0  # idempotent
        j = db.query(Judgement).one()
        assert (j.product_id, j.taste, j.quality_unclear) == (pid, 3, True)


# ----------------------------------------------------------------- the offline evaluator
def _judge(db, products, grade_of):
    from app.models import Judgement
    from app.services.evaluation import family_key
    from app.services.recommender import snapshot

    for p in products:
        db.add(Judgement(product_id=p.id, family_key=family_key(p), taste=grade_of(p), snapshot=snapshot(p)))
    db.commit()


def _grade(p) -> int:  # the synthetic "person": likes navy, fine with grey, dislikes red
    return {"navy": 3, "grey": 2, "red": 0}.get((p.colours or ["?"])[0], 1)


def _evaluate(env, *extra):
    db_path = env / "test.db"
    r = subprocess.run([sys.executable, "-m", "eval.real", "--db", str(db_path), "--data-dir", str(env), *extra],
                       cwd=BACKEND, env=os.environ.copy(), capture_output=True, text=True, timeout=600)  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout


def test_evaluator_reports_nothing_without_enough_labels(env, db):
    ps = _catalogue(db, n_per_seller=4)
    _judge(db, ps[:10], _grade)
    out = _evaluate(env)
    assert "Not enough labels yet" in out and "| bayes |" not in out
    assert not list((env / "evaluation").glob("results-*.json"))


def test_evaluator_measures_on_a_copy_without_training_on_test_labels(env, db):
    from app.models import Feedback, Judgement

    ps = _catalogue(db, n_per_seller=20)
    train, test = ps[:12], ps[12:57]
    for p in train:  # Discover decisions: the training data
        db.add(Feedback(product_id=p.id, action="love" if _grade(p) >= 2 else "dislike"))
    db.add(Feedback(product_id=test[0].id, action="love"))  # a decision about a judged piece: must not train
    db.commit()
    _judge(db, test, _grade)
    before = (db.query(Feedback).count(), db.query(Judgement).count())

    out = _evaluate(env)
    assert "| bayes |" in out and "| random |" in out and "| profile |" in out
    assert "Decisions about judged pieces removed from training: 1" in out
    saved = json.loads(sorted((env / "evaluation").glob("results-*.json"))[-1].read_text())
    assert saved["ready"] and saved["summary"]["judgements"] == 45
    db.expire_all()
    assert (db.query(Feedback).count(), db.query(Judgement).count()) == before  # the real database is untouched

    assert "Froze 45 judgements" in _evaluate(env, "--freeze")
    _judge(db, ps[57:], _grade)  # judged after freezing: not part of the frozen test set
    out = _evaluate(env)
    assert "45 judgements" in out and "(frozen)" in out
