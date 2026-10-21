"""API endpoint tests (feedback persistence, saved lists, imports, evidence, backup)."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH, make_image


@pytest.fixture()
def client(env, monkeypatch):
    # Jobs run synchronously in tests so results are deterministic and offline.
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: jobs.run_sync(kind, params))
    monkeypatch.setenv("WS_DOWNLOAD_IMAGES", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def _manual(client, title, **fields):
    files = fields.pop("files", None)
    r = client.post("/api/import/manual", data={"title": title, **fields}, files=files)
    assert r.status_code == 200, r.text
    return r.json()["product_id"]


def test_seed_data_is_labelled_and_never_scraped(client):
    items = client.get("/api/products", params={"sort": "newest", "status": "all"}).json()["items"]
    assert len(items) == 5
    assert all(i["data_origin"] == "seed" and i["completeness"] == "seed_unverified" for i in items)
    assert all(i["images"] == [] for i in items)  # no invented photos
    sellers = client.get("/api/sellers").json()
    assert {s["reputation_status"] for s in sellers} == {"unassessed"}
    # Seed examples are excluded from the discovery feed by default
    assert client.get("/api/feed").json()["items"] == []


def test_white_tee_seed_sets_value_ceiling_not_dislike(client):
    ins = client.get("/api/insights").json()
    assert ins["price_ceilings"].get("t-shirt") == 510
    colour_affinities = {a["label"]: a["affinity"] for a in ins["attributes"].get("colour", [])}
    assert colour_affinities.get("white", 0) >= 0


def test_manual_import_with_upload_and_feedback_cycle(client):
    pid = _manual(
        client,
        "Cashmere hoodie 黑色",
        price="880",
        seller_name="Manual Seller",
        material_text="90% wool 10% cashmere",
        category="hoodie",
        files=[("images", ("a.jpg", make_image(), "image/jpeg"))],
    )
    p = client.get(f"/api/products/{pid}").json()
    assert p["data_origin"] == "manual" and p["images"][0]["src"].startswith("/api/images/")
    assert p["materials"][0] == {"fibre": "wool", "pct": 90.0}
    assert client.get(p["images"][0]["src"]).status_code == 200
    assert p["landed_cost"]["total_gbp"] > p["price_gbp"]
    assert len(p["pros"]) <= 3 and len(p["cons"]) <= 3

    feed = client.get("/api/feed").json()["items"]
    assert [i["id"] for i in feed] == [pid]

    r = client.post("/api/feedback", json={"product_id": pid, "action": "love"})
    assert r.json()["saved"] == "loved"
    saved = client.get("/api/saved", params={"list": "loved"}).json()["items"]
    assert [s["id"] for s in saved] == [pid]
    assert client.get("/api/feed").json()["items"] == []  # decided items leave the feed

    fb_id = r.json()["feedback_id"]
    client.delete(f"/api/feedback/{fb_id}")
    assert client.get("/api/saved", params={"list": "loved"}).json()["items"] == []

    r = client.post("/api/feedback", json={"product_id": pid, "action": "purchased"})
    assert r.json()["wardrobe_item_id"]
    assert client.get("/api/wardrobe").json()["items"][0]["category"] == "hoodie"


def test_feedback_validation(client):
    pid = _manual(client, "Tee")
    r = client.post("/api/feedback", json={"product_id": pid, "action": "dislike", "reasons": ["bogus"]})
    assert r.status_code == 422
    r = client.post("/api/feedback", json={"product_id": pid, "action": "dislike", "reasons": ["colour", "logo"]})
    assert r.status_code == 200
    hist = client.get("/api/feedback").json()
    assert hist[0]["reasons"] == ["Dislike the colour", "Dislike the logo"]


def test_csv_and_json_import(client):
    csv_text = (
        "seller,title,url,price,material_text,category,colours,sizes,image_urls\n"
        "CSV Shop,Wool trousers,https://csv.example.com/1,399,100% virgin wool,trousers,charcoal,46/48/50,\n"
        ",,,,,,,,\n"
    )
    r = client.post("/api/import/csv", files={"file": ("p.csv", csv_text, "text/csv")}).json()
    assert r["imported"] == 1 and r["failed"] == 1
    data = {"products": [{"title": "Merino cardigan", "seller": "JSON Shop", "price": 300, "category": "cardigan"}]}
    r = client.post("/api/import/json", files={"file": ("p.json", json.dumps(data), "application/json")}).json()
    assert r["imported"] == 1
    origins = {i["data_origin"] for i in client.get("/api/products", params={"origin": "csv"}).json()["items"]}
    assert origins == {"csv"}


def test_evidence_changes_quality_label(client):
    pid = _manual(client, "Heavy tee 260g", price="150")
    assert client.get(f"/api/products/{pid}").json()["quality"]["status"] == "Unverified"
    for _ in range(2):
        r = client.post(
            f"/api/products/{pid}/evidence",
            json={
                "kind": "buyer_report",
                "aspect": "construction",
                "claim": "QC shows clean double-needle hems",
                "polarity": 1,
                "strength": 0.9,
                "source_url": "https://reddit.com/r/x/1",
            },
        )
        assert r.status_code == 200
    q = client.get(f"/api/products/{pid}").json()["quality"]
    assert q["dimensions"]["construction"]["score"] >= 60
    assert q["status"] != "Unverified"
    ev = client.get(f"/api/products/{pid}").json()["evidence"]
    assert any(e["kind_label"] == "Independent buyer report" and e["captured_at"] for e in ev)


def test_compare_endpoint(client):
    img = make_image()
    a = _manual(client, "MM zip jacket", price="528", seller_name="A", files=[("images", ("a.jpg", img, "image/jpeg"))])
    b = _manual(client, "MM zip jacket", price="420", seller_name="B", files=[("images", ("b.jpg", img, "image/jpeg"))])
    cmp = client.get(f"/api/products/{a}/compare").json()
    alt = cmp["alternatives"][0]
    assert alt["product"]["id"] == b and alt["relation"] == "shared_photos"
    assert "unconfirmed" in alt["relation_label"]
    assert cmp["verdict"]["recommendation"] == "cheaper_equivalent_available"
    r = client.post(f"/api/products/{a}/match", json={"other_id": b, "same": True}).json()
    assert r["relation"] == "confirmed_identical"


def test_settings_validation_and_backup_roundtrip(client):
    assert client.put("/api/settings/ranking_weights", json={"visual": 9}).status_code == 422
    assert client.put("/api/settings/ranking_weights", json={"visual": 2.0}).json()["visual"] == 2.0
    pid = _manual(client, "Tee", seller_name="Backup Shop")
    client.post("/api/feedback", json={"product_id": pid, "action": "like"})
    client.post(
        "/api/wardrobe",
        data={"name": "White Vince tee", "category": "t-shirt", "colours": "white"},
        files={"image": ("w.jpg", make_image((250, 250, 250), "tee"), "image/jpeg")},
    )
    backup = client.get("/api/backup/export").content
    data = json.loads(backup)
    assert data["format"] == "wardrobe-scout-backup" and data["wardrobe"][0]["image_b64"]
    assert any(f["action"] == "like" for f in data["feedback"])
    r = client.post("/api/backup/import", files={"file": ("b.json", io.BytesIO(backup), "application/json")}).json()
    assert r["feedback"] == 0  # idempotent: existing feedback is not duplicated


def test_bookmarklet_html_import(client):
    from .conftest import fixture_text

    r = client.post(
        "/api/import/bookmarklet",
        json={"url": "https://testseller.x.yupoo.com/albums/1001?uid=1", "html": fixture_text("yupoo_album_1001.html")},
    )
    body = r.json()
    assert body["ok"] and body["images"] == 2
    p = client.get(f"/api/products/{body['product_id']}").json()
    assert p["data_origin"] == "bookmarklet" and p["price"] == 919
    assert p["links"]["weidian"].endswith("7707075268")


def test_wardrobe_analysis(client):
    for n in range(3):
        client.post("/api/wardrobe", data={"name": f"Black tee {n}", "category": "t-shirt", "colours": "black"})
    a = client.get("/api/wardrobe").json()["analysis"]
    assert any(r["category"] == "t-shirt" for r in a["redundancy"])
    assert any(g["category"] == "trousers" for g in a["gaps"])
