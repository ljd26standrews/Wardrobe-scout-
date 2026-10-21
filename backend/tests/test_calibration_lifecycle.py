"""The calibration lifecycle, through the API: approval estimates exist only while the decisions they were fitted on
still stand and still suffice, under the taste model, photo representation, written profile and reading of your
decisions they were fitted for. The feed, the product page, Insights and Settings always report the same state.

SYNTHETIC FIXTURE: decisions are made by the test (approve the navy overcoats, reject the red hoodies). These tests
check lifecycle rules only. They say nothing about how well the app predicts a real person's taste.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH

KINDS = (("navy", "overcoat"), ("red", "hoodie"), ("grey", "trousers"))


def _app(monkeypatch, enqueue):
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setenv("WS_DOWNLOAD_IMAGES", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), headers=AUTH)


@pytest.fixture()
def client(env, monkeypatch):
    """Background jobs run at once, as soon as they are queued."""
    from app.services import jobs

    with _app(monkeypatch, lambda kind, params=None: jobs.run_sync(kind, params)) as c:
        yield c


@pytest.fixture()
def held(env, monkeypatch):
    """Background jobs are only recorded: what every surface says while a refit is still to come.
    ``held.run()`` then runs what was queued."""
    from app.services import jobs

    queue: list[tuple[str, dict]] = []

    def enqueue(kind, params=None):
        queue.append((kind, params or {}))
        return len(queue)

    with _app(monkeypatch, enqueue) as c:
        c.queue = queue

        def run():
            while queue:
                jobs.run_sync(*queue.pop(0))

        c.run = run
        yield c


def catalogue(db, n=48, seed=0):
    """Products of three kinds (a third each), with photo vectors; no decisions."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller
    from app.services import embeddings as emb

    rng = np.random.default_rng(seed)
    sellers = [Seller(name=f"lc{i}", slug=f"lc{i}", platform="manual") for i in range(3)]
    db.add_all(sellers)
    db.flush()
    backend = emb.get_image_backend().name
    out = []
    for i in range(n):
        colour, cat = KINDS[i % 3]
        raw = RawListing(f"lc{i}", f"https://shop.example/lc{i}", f"{colour} wool {cat} {i}", price=300.0)
        p = upsert_listing(db, sellers[i % 3], raw).product
        p.image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
        p.embedding_backend = backend
        out.append(p)
    db.commit()
    return [p.id for p in out]


def decide(c, pid: int, action: str, reasons=()) -> int:
    r = c.post("/api/feedback", json={"product_id": pid, "action": action, "reasons": list(reasons)})
    assert r.status_code == 200, r.text
    return r.json()["feedback_id"]


def rate(c, ids: list[int], approvals: int) -> list[int]:
    """The first ``approvals`` pieces are liked and the rest disliked, through the API."""
    return [decide(c, pid, "like" if i < approvals else "dislike") for i, pid in enumerate(ids)]


def surfaces(c, pid: int) -> dict:
    """The calibration state as the feed, a product page, Insights and Settings each report it, and whether any of
    them shows an approval estimate."""
    feed = c.get("/api/feed", params={"limit": 60}).json()
    product = c.get(f"/api/products/{pid}").json()
    insights = c.get("/api/insights").json()
    settings = c.get("/api/selective/status").json()
    shown = any("estimate" in i["confidence"] for i in feed["items"]) or "estimate" in product["confidence"]
    return {
        "feed": feed["gate"]["state"],
        "product": product["confidence"]["state"],
        "insights": insights["top_gate"]["state"],
        "settings": settings["calibration"]["state"],
        "estimates_shown": shown,
    }


def everywhere(state: str) -> dict:
    return {"feed": state, "product": state, "insights": state, "settings": state,
            "estimates_shown": state in ("calibrated", "stale")}  # fmt: skip


def calibration_status(c) -> dict:
    return c.get("/api/selective/status").json()["calibration"]


# ----------------------------------------------------------------- withdrawal when evidence no longer suffices
def test_undo_from_30_to_29_rated_pieces_withdraws_the_estimates_everywhere(client, db):
    """The reproduced problem: calibrated on 30 rated families (the minimum), then one decision undone through the
    feedback API. The app kept reporting a calibration resting on 29."""
    ids = catalogue(db)
    fids = rate(client, ids[:30], approvals=15)
    assert surfaces(client, ids[40]) == everywhere("calibrated")
    assert calibration_status(client)["labels"] == 30

    assert client.delete(f"/api/feedback/{fids[-1]}").status_code == 200
    assert surfaces(client, ids[40]) == everywhere("uncalibrated")
    st = calibration_status(client)
    assert st["labels"] == 29 and st["sufficient"] is False and st["calibration"] is None
    assert st["withdrawn"]["reason"] == "insufficient"
    from app.services import calibration

    db.expire_all()
    assert calibration.active(db) is None and calibration.active_map(db) is None


def test_unsave_that_leaves_too_few_approvals_withdraws_the_estimates(client, db):
    """Five approvals is the minimum for each outcome; unsaving one of them leaves four."""
    ids = catalogue(db)
    rate(client, ids[:34], approvals=5)
    assert surfaces(client, ids[40]) == everywhere("calibrated")

    assert client.delete(f"/api/saved/{ids[0]}").status_code == 200
    assert surfaces(client, ids[40]) == everywhere("uncalibrated")
    st = calibration_status(client)
    assert st["approvals"] == 4 and st["labels"] == 33 and st["withdrawn"]["reason"] == "insufficient"


def test_raising_the_minimum_in_settings_withdraws_the_estimates(client, db):
    ids = catalogue(db)
    rate(client, ids[:30], approvals=15)
    cfg = client.get("/api/settings").json()["selective"]
    assert client.put("/api/settings/selective", json={**cfg, "min_labels": 31}).status_code == 200
    assert surfaces(client, ids[40]) == everywhere("uncalibrated")
    assert calibration_status(client)["withdrawn"]["reason"] == "insufficient"


# ----------------------------------------------------------------- withdrawn and corrected evidence
def test_undoing_a_decision_the_fit_used_withdraws_it_then_a_refit_restores_estimates(held, db):
    ids = catalogue(db)
    fids = rate(held, ids[:36], approvals=18)
    held.run()
    assert surfaces(held, ids[40]) == everywhere("calibrated")
    first = calibration_status(held)["calibration"]["id"]

    assert held.delete(f"/api/feedback/{fids[3]}").status_code == 200  # still 35: enough, but the fit used it
    assert surfaces(held, ids[40]) == everywhere("pending")
    st = calibration_status(held)
    assert st["withdrawn"]["reason"] == "withdrawn" and st["calibration"] is None
    assert ("recalibrate", {}) in held.queue

    held.run()
    assert surfaces(held, ids[40]) == everywhere("calibrated")
    st = calibration_status(held)
    assert st["calibration"]["id"] != first and st["calibration"]["n_labels"] == 35 and "withdrawn" not in st


def test_reversing_a_decision_the_fit_used_withdraws_it(held, db):
    ids = catalogue(db)
    rate(held, ids[:36], approvals=18)
    held.run()
    decide(held, ids[0], "dislike")  # liked before: the label the fit used is now the opposite
    assert surfaces(held, ids[40]) == everywhere("pending")
    assert calibration_status(held)["withdrawn"]["reason"] == "corrected"


def test_new_decisions_make_the_calibration_stale_not_withdrawn(held, db):
    """New evidence does not contradict the fit: its estimates stay in use, marked as due for an update, until the
    refit after ``recalibrate_every`` (5) new decisions has run."""
    ids = catalogue(db)
    rate(held, ids[:36], approvals=18)
    held.run()
    rate(held, ids[36:40], approvals=2)  # four new: not yet due
    assert surfaces(held, ids[44]) == everywhere("calibrated")
    decide(held, ids[40], "like")  # the fifth
    assert surfaces(held, ids[44]) == everywhere("stale")
    st = calibration_status(held)
    assert st["since"]["new_decisions"] == 5 and st["since"]["refit_due"] is True and "withdrawn" not in st
    held.run()
    assert surfaces(held, ids[44]) == everywhere("calibrated")
    assert calibration_status(held)["calibration"]["n_labels"] == 41


# ----------------------------------------------------------------- jobs check their source before activating
def test_a_job_queued_while_enough_is_never_activated_after_undo_leaves_too_few(held, db):
    ids = catalogue(db)
    fids = rate(held, ids[:30], approvals=15)
    assert ("recalibrate", {}) in held.queue
    assert surfaces(held, ids[40]) == everywhere("pending")  # queued, not yet run
    assert held.delete(f"/api/feedback/{fids[0]}").status_code == 200
    assert surfaces(held, ids[40]) == everywhere("uncalibrated")
    held.run()  # the delayed job runs now, on 29
    assert surfaces(held, ids[40]) == everywhere("uncalibrated")
    from app.models import Calibration

    assert db.query(Calibration).count() == 0


def test_a_decision_undone_while_the_job_is_fitting_is_never_activated(env, db, monkeypatch):
    """The job reads your decisions, fits, then rechecks them before activating: one undone meanwhile sends the fit
    back to be redone on what still stands."""
    from app.db import SessionLocal
    from app.models import Feedback
    from app.services import calibration

    ids = catalogue(db)
    for i, pid in enumerate(ids[:34]):
        db.add(Feedback(product_id=pid, action="like" if i % 2 else "dislike", reasons=[]))
    db.commit()
    undone = db.query(Feedback).filter_by(product_id=ids[5]).one().id
    real_fit, calls = calibration.fit_map, []

    def fit_while_undoing(*args, **kw):
        calls.append(len(args[2]))  # the decisions this fit is given
        result = real_fit(*args, **kw)
        if len(calls) == 1:  # meanwhile, in another request: Undo
            with SessionLocal() as other:
                other.delete(other.get(Feedback, undone))
                other.commit()
        return result

    monkeypatch.setattr(calibration, "fit_map", fit_while_undoing)
    res = calibration.recalibrate(db)
    assert res["status"] == "calibrated" and calls == [34, 33]
    db.expire_all()
    cal = calibration.active(db)
    assert cal.n_labels == 33 and all(not k.endswith(f":{undone}") for k in cal.params["label_keys"])


def test_a_job_whose_decisions_keep_changing_gives_up_without_activating(env, db, monkeypatch):
    from app.db import SessionLocal
    from app.models import Feedback
    from app.services import calibration, jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: 0)
    ids = catalogue(db)
    for i, pid in enumerate(ids[:40]):
        db.add(Feedback(product_id=pid, action="like" if i % 2 else "dislike", reasons=[]))
    db.commit()
    real_fit = calibration.fit_map

    def fit_while_undoing(*args, **kw):
        result = real_fit(*args, **kw)
        with SessionLocal() as other:  # an Undo during every attempt
            fb = other.query(Feedback).order_by(Feedback.id).first()
            other.delete(fb)
            other.commit()
        return result

    monkeypatch.setattr(calibration, "fit_map", fit_while_undoing)
    assert calibration.recalibrate(db)["status"] == "superseded"
    db.expire_all()
    assert calibration.active(db) is None
    assert calibration.status(db)["state"] == "pending"


# ----------------------------------------------------------------- the model, representation and profile
def test_a_changed_profile_retires_the_calibration_until_refitted(held, db):
    ids = catalogue(db)
    rate(held, ids[:36], approvals=18)
    held.run()
    assert surfaces(held, ids[40]) == everywhere("calibrated")
    taste = held.get("/api/settings").json()["taste_profile"]

    # Free-text notes are not part of the model: nothing changes.
    assert held.put("/api/settings/taste_profile", json={**taste, "notes": "something else"}).status_code == 200
    assert surfaces(held, ids[40]) == everywhere("calibrated")

    colours = [c for c in taste["preferred_colours"] if c != "navy"]
    assert held.put("/api/settings/taste_profile", json={**taste, "preferred_colours": colours}).status_code == 200
    assert surfaces(held, ids[40]) == everywhere("pending")
    assert calibration_status(held)["withdrawn"]["reason"] == "profile"
    held.run()
    assert surfaces(held, ids[40]) == everywhere("calibrated")


def test_a_changed_photo_representation_retires_the_calibration(held, db, monkeypatch):
    """Switching between the lightweight photo features and CLIP (or a new version of either) changes what every
    raw score means: the old calibration is never applied to the new scores."""
    from app.services import embeddings as emb

    ids = catalogue(db)
    rate(held, ids[:36], approvals=18)
    held.run()
    assert surfaces(held, ids[40]) == everywhere("calibrated")
    monkeypatch.setattr(emb.LiteImageBackend, "name", "lite-test-v2")
    emb.reset_backend()
    assert surfaces(held, ids[40]) == everywhere("pending")
    st = calibration_status(held)
    assert st["withdrawn"]["reason"] == "representation" and st["representation"] == "lite-test-v2"
    held.run()
    assert surfaces(held, ids[40]) == everywhere("calibrated")


# ----------------------------------------------------------------- rollback
def test_rollback_only_to_a_calibration_whose_decisions_still_stand(held, db):
    ids = catalogue(db)
    fids = rate(held, ids[:36], approvals=18)
    held.run()
    first = calibration_status(held)["calibration"]["id"]
    rate(held, ids[36:41], approvals=3)
    held.run()
    second = calibration_status(held)["calibration"]["id"]
    assert second != first

    # Compatible: everything the first was fitted on still stands.
    r = held.post(f"/api/selective/calibrations/{first}/activate")
    assert r.status_code == 200 and r.json()["calibration"]["calibration"]["id"] == first
    assert surfaces(held, ids[44]) == everywhere("calibrated")
    feed = held.get("/api/feed").json()
    assert feed["gate"]["calibration"]["id"] == first

    # Undo a decision both were fitted on: neither may come back.
    assert held.delete(f"/api/feedback/{fids[2]}").status_code == 200
    held.run()
    third = calibration_status(held)["calibration"]["id"]
    for old in (first, second):
        r = held.post(f"/api/selective/calibrations/{old}/activate")
        assert r.status_code == 409 and "undone" in r.json()["detail"]
    assert calibration_status(held)["calibration"]["id"] == third
    hist = {h["id"]: h for h in held.get("/api/selective/status").json()["history"]}
    assert hist[first]["usable"] is False and hist[third]["usable"] is True

    # Nor one fitted under another profile.
    taste = held.get("/api/settings").json()["taste_profile"]
    held.put("/api/settings/taste_profile", json={**taste, "preferred_fits": ["relaxed"]})
    held.run()
    r = held.post(f"/api/selective/calibrations/{third}/activate")
    assert r.status_code == 409 and "profile" in r.json()["detail"]
    assert held.post("/api/selective/calibrations/99999/activate").status_code == 404


def test_a_failed_refit_is_reported_as_failed_not_pending(held, db, monkeypatch):
    from app.services import calibration

    ids = catalogue(db)
    rate(held, ids[:30], approvals=15)

    def broken(*a, **k):
        raise RuntimeError("numerical failure")

    monkeypatch.setattr(calibration, "fit_map", broken)
    held.run()
    assert surfaces(held, ids[40]) == everywhere("failed")
    assert calibration_status(held)["last_attempt"]["status"] == "failed"
