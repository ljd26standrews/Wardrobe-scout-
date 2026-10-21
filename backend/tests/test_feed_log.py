"""Discover feed: impression log (what was served and shown, under which model) and one slot per garment."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


@pytest.fixture()
def client(env, monkeypatch):
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: jobs.run_sync(kind, params))
    monkeypatch.setenv("WS_DOWNLOAD_IMAGES", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def _manual(client, title, **fields):
    r = client.post("/api/import/manual", data={"title": title, **fields})
    assert r.status_code == 200, r.text
    return r.json()["product_id"]


def _family(pids):
    from app.db import SessionLocal
    from app.models import Product

    with SessionLocal() as s:
        for pid in pids:
            s.get(Product, pid).group_id = min(pids)
        s.commit()


def test_feed_is_logged_with_model_version_and_served_order(client):
    from app.db import SessionLocal
    from app.models import FeedRequest
    from app.services.preference_model import ENGINE_VERSION

    pids = [_manual(client, f"Navy wool cardigan {i}", price="300", category="cardigan") for i in range(3)]
    feed = client.get("/api/feed").json()
    assert feed["feed_id"] and {i["id"] for i in feed["items"]} == set(pids)
    with SessionLocal() as s:
        fr = s.get(FeedRequest, feed["feed_id"])
        assert fr.engine == "bayes" and fr.engine_version == ENGINE_VERSION
        assert [it["product_id"] for it in fr.items] == [i["id"] for i in feed["items"]]
        assert [it["position"] for it in fr.items] == list(range(len(pids)))
        assert fr.candidates == feed["total_candidates"] == 3 and fr.decisions == feed["decisions"]


def test_only_served_products_are_logged_as_shown_at_the_served_position(client):
    from app.db import SessionLocal
    from app.models import Impression

    pids = [_manual(client, f"Grey wool trousers {i}", price="400", category="trousers") for i in range(2)]
    feed = client.get("/api/feed").json()
    first = feed["items"][0]["id"]
    body = {"surface": "card", "items": [{"product_id": first, "position": 7}, {"product_id": 999999}]}
    assert client.post(f"/api/feed/{feed['feed_id']}/viewed", json=body).json() == {"logged": 1}
    assert client.post(f"/api/feed/{feed['feed_id']}/viewed", json=body).json() == {"logged": 0}  # once per feed
    assert client.post("/api/feed/nope/viewed", json=body).status_code == 404
    with SessionLocal() as s:
        imp = s.query(Impression).one()
        assert (imp.product_id, imp.position, imp.surface) == (first, 0, "card")  # server's position, not 7
    r = client.post("/api/feedback", json={"product_id": first, "action": "like",
                                           "context": {"feed_id": feed["feed_id"], "position": 0}})  # fmt: skip
    assert r.status_code == 200
    hist = client.get("/api/feedback").json()
    assert hist[0]["context"]["feed_id"] == feed["feed_id"] and set(pids) >= {first}


def test_one_card_per_garment_and_a_decided_garment_leaves_the_feed(client):
    family = [_manual(client, f"Olive wool overcoat listing {i}", price=str(500 + 50 * i), category="overcoat")
              for i in range(3)]  # fmt: skip
    other = _manual(client, "Charcoal cashmere jumper", price="450", category="knitwear")
    _family(family)
    items = client.get("/api/feed").json()["items"]
    shown = [i for i in items if i["id"] in family]
    assert len(shown) == 1 and shown[0]["family"]["size"] == 3
    assert {o["product_id"] for o in shown[0]["family"]["offers"]} == set(family) - {shown[0]["id"]}
    assert other in {i["id"] for i in items}

    client.post("/api/feedback", json={"product_id": family[1], "action": "love"})
    ids = {i["id"] for i in client.get("/api/feed").json()["items"]}
    assert not ids & set(family) and other in ids  # the garment is decided; its other listings are not new


def test_a_price_only_dislike_keeps_other_offers_of_the_garment(client):
    family = [_manual(client, f"Camel wool coat listing {i}", price=str(900 - 200 * i), category="overcoat")
              for i in range(2)]  # fmt: skip
    _family(family)
    client.post("/api/feedback", json={"product_id": family[0], "action": "dislike", "reasons": ["too_expensive"]})
    ids = {i["id"] for i in client.get("/api/feed").json()["items"]}
    assert family[1] in ids  # a cheaper offer of the same piece is exactly what you would want to see
