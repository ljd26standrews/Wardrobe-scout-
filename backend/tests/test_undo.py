"""Undo (DELETE /api/feedback/{id}, and an Undo of a move in Saved): it puts a piece's saved details back exactly as
they were, never overwrites a newer decision or a newer change in Saved (it is refused instead), keeps the meaning
of your decisions as taste labels, and is safe to repeat or retry. Disposable database, synthetic listings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_approval_labels import _product, _seller

DETAILS = {"list_name": "maybe", "priority": 1, "note": "Measure the sleeves first", "target_size": "M"}


@pytest.fixture()
def client(env, monkeypatch):
    from app.main import create_app
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: 0)
    with TestClient(create_app(), headers=AUTH, raise_server_exceptions=False) as c:
        yield c


def saved(c, pid: int) -> dict | None:
    item = next((i for i in c.get("/api/saved").json()["items"] if i["id"] == pid), None)
    if item is None:
        return None
    s = item["saved"]
    return {"list_name": s["list"], "priority": s["priority"], "note": s["note"], "target_size": s["target_size"],
            "added_at": s["added_at"]}  # fmt: skip


def decide(c, pid: int, action: str, reasons=()) -> dict:
    r = c.post("/api/feedback", json={"product_id": pid, "action": action, "reasons": list(reasons)})
    assert r.status_code == 200, r.text
    return r.json()


def undo(c, d: dict):
    return c.delete(f"/api/feedback/{d['feedback_id']}", params={"token": d["undo_token"]})


def _piece(db, ext="u1"):
    return _product(db, _seller(db, f"s-{ext}"), ext)


@pytest.mark.parametrize(
    ("action", "reasons"),
    [("dislike", ()), ("dislike", ("colour",)), ("purchased", ()), ("love", ()), ("like", ("not_now",)),
     ("maybe", ()), ("unsave", ())],  # fmt: skip
)
def test_undo_puts_saved_details_back_exactly(client, db, action, reasons):
    p = _piece(db)
    details = {**DETAILS, "list_name": "liked"} if action == "maybe" else DETAILS  # so that the action changes it
    assert client.patch(f"/api/saved/{p.id}", json=details).status_code == 200
    before = saved(client, p.id)
    if action == "unsave":
        r = client.delete(f"/api/saved/{p.id}")
        d = r.json()
    else:
        d = decide(client, p.id, action, reasons)
    assert saved(client, p.id) != before  # the action changed it
    r = undo(client, d)
    assert r.status_code == 200, r.text
    assert saved(client, p.id) == before
    if action == "purchased":  # the wardrobe entry it created is gone again
        assert all(w.get("product_id") != p.id for w in client.get("/api/wardrobe").json()["items"])


def test_undoing_a_decision_on_an_unsaved_piece_leaves_it_unsaved(client, db):
    p = _piece(db)
    d = decide(client, p.id, "like")
    assert saved(client, p.id)["list_name"] == "liked"
    assert undo(client, d).status_code == 200
    assert saved(client, p.id) is None


def test_an_older_undo_never_overwrites_a_newer_decision(client, db):
    p = _piece(db)
    first = decide(client, p.id, "like")
    second = decide(client, p.id, "dislike")
    r = undo(client, first)
    assert r.status_code == 409 and "decided about this piece again" in r.json()["detail"]
    assert saved(client, p.id) is None  # the dislike stands
    # In order, each Undo works: back to liked, then to nothing.
    assert undo(client, second).status_code == 200 and saved(client, p.id)["list_name"] == "liked"
    assert undo(client, first).status_code == 200 and saved(client, p.id) is None


def test_an_undo_never_discards_a_change_made_in_saved_since(client, db):
    p = _piece(db)
    d = decide(client, p.id, "like")
    assert client.patch(f"/api/saved/{p.id}", json={"note": "Ask for a photo of the label"}).status_code == 200
    r = undo(client, d)
    assert r.status_code == 409 and "changed in Saved since" in r.json()["detail"]
    assert saved(client, p.id)["note"] == "Ask for a photo of the label"


def test_undoing_a_move_in_saved_respects_a_newer_change(client, db):
    p = _piece(db)
    client.patch(f"/api/saved/{p.id}", json=DETAILS)
    before = saved(client, p.id)
    moved = client.patch(f"/api/saved/{p.id}", json={"list_name": "wishlist"}).json()["saved"]
    back = {k: before[k] for k in ("list_name", "priority", "note", "target_size")}
    expect = {k: moved[k] for k in ("list_name", "priority", "note", "target_size")}
    client.patch(f"/api/saved/{p.id}", json={"priority": 3})  # a newer change
    r = client.patch(f"/api/saved/{p.id}", json={**back, "expect": expect})
    assert r.status_code == 409
    assert saved(client, p.id)["priority"] == 3 and saved(client, p.id)["list_name"] == "wishlist"
    current = {k: v for k, v in saved(client, p.id).items() if k != "added_at"}
    assert client.patch(f"/api/saved/{p.id}", json={**back, "expect": current}).status_code == 200
    assert saved(client, p.id) == before


def test_undo_is_idempotent_even_when_the_id_is_reused(client, db):
    from app.models import Feedback

    p, q = _piece(db, "a"), _piece(db, "b")
    d = decide(client, p.id, "like")
    assert undo(client, d).status_code == 200
    again = undo(client, d)  # a retry after a lost response, or a second click
    assert again.status_code == 200 and again.json().get("already_undone") is True
    newer = decide(client, q.id, "love")
    assert newer["feedback_id"] == d["feedback_id"]  # SQLite gave the freed id to the newer decision
    late = undo(client, d)  # the earlier Undo, retried once more
    assert late.status_code == 200 and late.json().get("already_undone") is True
    db.expire_all()
    assert db.get(Feedback, newer["feedback_id"]).product_id == q.id and saved(client, q.id)["list_name"] == "loved"


def test_a_failed_undo_changes_nothing_and_can_be_retried(client, db, monkeypatch):
    from app.api import feedback as api
    from app.models import Feedback

    p = _piece(db)
    client.patch(f"/api/saved/{p.id}", json=DETAILS)
    before = saved(client, p.id)
    d = decide(client, p.id, "dislike")
    real, calls = api._restore_saved, []

    def failing_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        return real(*a, **k)

    monkeypatch.setattr(api, "_restore_saved", failing_once)
    assert undo(client, d).status_code == 500
    db.expire_all()
    assert db.get(Feedback, d["feedback_id"]) is not None and saved(client, p.id) is None  # nothing changed
    assert undo(client, d).status_code == 200
    assert saved(client, p.id) == before


def test_undo_keeps_what_your_decisions_say_about_taste(client, db):
    from app.services.approval_data import rated_outcomes

    def label():
        db.expire_all()
        return {o.product_id: o.y for o in rated_outcomes(db)}.get(p.id)

    p = _piece(db)
    client.patch(f"/api/saved/{p.id}", json={"list_name": "wishlist"})  # saving says you like the look
    assert label() == 1
    d = decide(client, p.id, "dislike")
    assert label() == 0
    undo(client, d)
    assert label() == 1 and saved(client, p.id)["list_name"] == "wishlist"
    removed = client.delete(f"/api/saved/{p.id}").json()
    assert label() is None  # an unsaved approval is no longer a label
    undo(client, removed)
    assert label() == 1 and saved(client, p.id)["list_name"] == "wishlist"


def test_undoing_a_grade_is_idempotent_even_when_the_id_is_reused(client, db):
    from app.models import Judgement

    from .test_evaluation import _catalogue

    ps = _catalogue(db, n_per_seller=4)
    first = client.post("/api/eval/judgements", json={"product_id": ps[0].id, "taste": 2})
    if first.status_code == 409:  # not in the evaluation pool in this fixture
        pytest.skip(first.text)
    jid = first.json()["id"]
    assert client.delete(f"/api/eval/judgements/{jid}", params={"product_id": ps[0].id}).status_code == 200
    second = client.post("/api/eval/judgements", json={"product_id": ps[1].id, "taste": 3}).json()["id"]
    assert second == jid  # the freed id, given to a grade of another piece
    late = client.delete(f"/api/eval/judgements/{jid}", params={"product_id": ps[0].id})
    assert late.status_code == 200 and late.json()["already_undone"] is True
    db.expire_all()
    assert db.get(Judgement, jid).product_id == ps[1].id
