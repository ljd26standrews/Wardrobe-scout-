"""Regression tests for defects found in the phase-1 audit (each failed before the fix)."""

from fastapi.testclient import TestClient

from .conftest import AUTH, make_image

BASE = "https://testseller.x.yupoo.com"


def _catalogue(web, pages=3, per_page=2):
    web.add(f"{BASE}/robots.txt", "", status=404)
    n = 0
    for page in range(1, pages + 1):
        cards = ""
        for _ in range(per_page):
            n += 1
            cards += f'<a class="album__main" title="hoodie {n} ¥{100 + n}" href="/albums/{n}?uid=1"><div class="album__title">hoodie {n} ¥{100 + n}</div></a>'
            web.add(
                f"{BASE}/albums/{n}?uid=1",
                f"<span class='showalbumheader__gallerytitle'>hoodie {n} ¥{100 + n}</span>"
                f"<div class='showalbum__children'><img data-origin-src='//photo.yupoo.com/t/{n}/big.jpg'></div>",
            )
            web.add(
                f"https://photo.yupoo.com/t/{n}/big.jpg",
                make_image((10 * n, 30, 30), "hoodie", seed=n),
                ctype="image/jpeg",
            )
        web.add(
            f"{BASE}/albums?page={page}",
            f"<div>{cards}</div><div class='pagination__main'><form class='pagination__jumpwrap'><input name='page' max='{pages}'></form></div>",
        )


def test_P1_full_scan_capped_by_max_pages_must_not_mark_unseen_unavailable(db, web, monkeypatch):
    from app.config import get_settings
    from app.ingestion.scan import scan_seller
    from app.models import Product, Seller

    _catalogue(web, pages=3)
    s = Seller(name="T", slug="testseller", platform="yupoo", base_url=BASE)
    db.add(s)
    db.commit()
    with web.client() as c:
        scan_seller(db, s, mode="full", client=c)
    assert db.query(Product).filter_by(status="active").count() == 6
    monkeypatch.setattr(get_settings(), "max_pages_per_scan", 1)  # scan only page 1 of 3
    with web.client() as c:
        scan_seller(db, s, mode="full", client=c)
    unavailable = db.query(Product).filter_by(status="unavailable").count()
    assert unavailable == 0, f"{unavailable} products wrongly marked unavailable after a page-capped 'full' scan"


def test_P15_seed_examples_create_seller_preference(env):
    """Seeds are 'not quality endorsements' yet they teach a seller affinity that boosts every product of that seller."""
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        ins = c.get("/api/insights").json()
    sellers = {a["label"]: a["affinity"] for a in ins["attributes"].get("seller", [])}
    assert not any(v > 0 for v in sellers.values()), sellers


def test_P18_undo_purchased_removes_wardrobe_item(env, monkeypatch):
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        pid = c.post("/api/import/manual", data={"title": "Wool trousers", "seller_name": "X"}).json()["product_id"]
        fb = c.post("/api/feedback", json={"product_id": pid, "action": "purchased"}).json()
        c.delete(f"/api/feedback/{fb['feedback_id']}")
        assert c.get("/api/wardrobe").json()["items"] == [], "wardrobe item survives undo of 'purchased'"


def test_P12_files_endpoint_prefix_traversal(env):
    from app.main import create_app

    sibling = env.parent / (env.name + "-evil")
    sibling.mkdir(exist_ok=True)
    (sibling / "secret.jpg").write_bytes(make_image())
    with TestClient(create_app(), headers=AUTH) as c:
        r = c.get(f"/api/files/../{sibling.name}/secret.jpg")
        r2 = c.get(f"/api/files/%2E%2E/{sibling.name}/secret.jpg")
    assert r.status_code == 404 and r2.status_code == 404, (r.status_code, r2.status_code)


def test_P27_any_website_can_read_backup_cross_origin(env):
    """Fixed by the API token: a malicious page cannot authenticate, so it gets 401."""
    from app.main import create_app

    with TestClient(create_app()) as c:  # no token, like a random website
        r = c.get("/api/backup/export", headers={"Origin": "https://evil.example"})
    assert r.status_code == 401


def test_P11_ssrf_import_accepts_internal_addresses(env, monkeypatch):
    from app.main import create_app
    from app.services import jobs

    seen = {}
    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: seen.setdefault("p", params) and 1)
    with TestClient(create_app(), headers=AUTH) as c:
        r = c.post("/api/import/url", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code in (400, 422), f"internal URL accepted for server-side fetch: {r.status_code}"


def test_P14_value_component_prefers_cheap_unverified(db):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller
    from app.services.recommender import rank_products

    s = Seller(name="S", slug="s", platform="manual")
    db.add(s)
    db.commit()
    ps = [
        upsert_listing(db, s, RawListing(f"x{i}", f"https://e/{i}", f"black wool trousers ¥{p}", price=p)).product
        for i, p in enumerate([150, 300, 400, 500, 900, 1600])
    ]
    db.commit()
    r = {x.product.price: x.components["value"] for x in rank_products(db, ps)}
    # With NO quality evidence, price alone should not be rewarded as "value"
    assert abs(r[150] - r[1600]) < 0.05, r
