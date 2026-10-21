"""The staged, concurrent scanner (app/ingestion/scanner.py) and the politeness layer underneath it."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from bench import render
from bench.server import album_html, listing_html
from tests.conftest import make_image

SELLER = "shop1"
BASE = f"https://{SELLER}.x.yupoo.com"


def _album(aid: int, title: str, keys: list[str]) -> dict:
    return {"id": str(aid), "title": title, "images": [{"key": k} for k in keys]}


def _chart() -> bytes:
    img = render._fit(render.render({"kind": "size_table_cn", "photos": [], "seed": 3, "seller": "x"}), 640)
    return render.jpeg(img, 80)


def serve(web, albums: list[dict], images: dict[str, bytes], per_page: int = 40) -> None:
    """Yupoo-like catalogue (same markup as the benchmark server) on the FakeWeb."""
    import bench.server as srv

    srv.PER_PAGE = per_page
    web.add(f"{BASE}/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain")
    pages = max(1, (len(albums) + per_page - 1) // per_page)
    for page in range(1, pages + 1):
        web.add(f"{BASE}/albums?page={page}", listing_html(SELLER, albums, page))
    for a in albums:
        web.add(f"{BASE}/albums/{a['id']}?uid=1", album_html(SELLER, a))
    for key, data in images.items():
        for variant in ("medium", "big"):
            web.add(f"https://photo.yupoo.com/{SELLER}/{key}/{variant}.jpg", data, ctype="image/jpeg")


PHOTOS = sorted((Path(__file__).parent / "fixtures" / "photos").glob("*.jpg"))


def _catalogue(n: int, *, chart_first: bool = False) -> tuple[list[dict], dict[str, bytes]]:
    """Albums of real, distinct garment photos (two views each) plus a size chart."""
    albums, images = [], {}
    for i in range(n):
        keys = [f"g{i}a", f"g{i}b", f"c{i}"]
        images[f"g{i}a"] = PHOTOS[i % 12].read_bytes()
        images[f"g{i}b"] = PHOTOS[12 + i % 12].read_bytes()
        images[f"c{i}"] = _chart()
        if chart_first:
            keys = [keys[2], keys[0], keys[1]]
        albums.append(_album(5000 - i, f"【{1000 + i}】羊毛连帽卫衣 ¥{300 + i}", keys))
    return albums, images


def _seller(db):
    from app.models import Seller

    s = Seller(name="Shop", slug=SELLER, platform="yupoo", base_url=BASE)
    db.add(s)
    db.commit()
    return s


def _image_requests(web) -> list[str]:
    return [str(r.url) for r in web.requests if r.url.host == "photo.yupoo.com"]


def test_discovery_makes_products_browsable_before_analysis_and_resume_finishes(db, web):
    from app.ingestion.scan import drain_pending, scan_seller
    from app.models import Product

    albums, images = _catalogue(6)
    serve(web, albums, images)
    seller = _seller(db)
    with web.client() as c:
        run = scan_seller(db, seller, mode="incremental", client=c, max_products=2)
    prods = db.query(Product).filter(Product.seller_id == seller.id).all()
    assert len(prods) == 6 and run.discovery_complete
    by_state = {p.external_id: p.analysis_state for p in prods}
    assert sorted(by_state.values()).count("analysed") == 2  # the per-scan analysis cap
    waiting = [p for p in prods if p.analysis_state != "analysed"]
    # Listing data alone already gives title, price and category
    assert all(p.title_original and p.price and p.category == "hoodie" for p in waiting)
    with web.client() as c:
        drain_pending(db, client=c)
    states = {p.analysis_state for p in db.query(Product).filter(Product.seller_id == seller.id)}
    assert states <= {"analysed", "deep"}, states  # look-alike test images also earn a deep duplicate check


def test_size_chart_cover_is_excluded_and_kept_for_sizing(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Product
    from app.services.presenter import gallery

    albums, images = _catalogue(2, chart_first=True)
    serve(web, albums, images)
    seller = _seller(db)
    with web.client() as c:
        scan_seller(db, seller, client=c)
    p = db.query(Product).filter_by(external_id=albums[0]["id"]).one()
    first = sorted(p.images, key=lambda i: i.position)[0]
    assert first.role == "size_chart" and not first.selected
    assert p.image_embedding is not None
    assert [i.role for i in p.images if i.selected] == ["garment"]
    garments, sizing = gallery(p)
    assert sizing == [first] and first not in garments


def test_progressive_policy_downloads_one_useful_photo_per_product(db, web):
    from app.ingestion.scan import scan_seller

    albums, images = _catalogue(5)
    serve(web, albums, images)
    with web.client() as c:
        scan_seller(db, _seller(db), client=c)
    reqs = _image_requests(web)
    assert len(reqs) == 5, reqs  # one photo each: the first garment view is enough
    assert all(u.endswith("/medium.jpg") for u in reqs)  # the display-size variant, not full resolution


def test_repeated_banner_across_listings_is_excluded(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import ProductImage

    albums, images = _catalogue(4)
    banner = render.jpeg(render._fit(render.render({"kind": "text_card", "photos": [], "seed": 1, "seller": "x"}), 640))
    for i, a in enumerate(albums):
        images[f"banner{i}"] = banner  # re-uploaded under a new URL each time
        a["images"].insert(0, {"key": f"banner{i}"})
    serve(web, albums, images)
    with web.client() as c:
        scan_seller(db, _seller(db), client=c)
    roles = {i.role for i in db.query(ProductImage).filter(ProductImage.url.like("%banner%"))}
    assert roles <= {"text_graphic", "size_chart", "repeated"}, roles
    assert not db.query(ProductImage).filter(ProductImage.url.like("%banner%"), ProductImage.selected.is_(True)).count()


def test_repeat_scan_fetches_only_new_and_changed_including_mid_catalogue_inserts(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Product

    albums, images = _catalogue(60)
    serve(web, albums, images, per_page=20)
    seller = _seller(db)
    with web.client() as c:
        scan_seller(db, seller, client=c)
    # A new album inserted deep in the listing (not chronological) and a price change on another.
    new = _album(9999, "【7777】新款外套 ¥900", ["n1"])
    images["n1"] = make_image((200, 30, 30), "tee")
    albums.insert(45, new)
    albums[10]["title"] = albums[10]["title"].replace("¥", "¥1")
    serve(web, albums, images, per_page=20)
    before = len(web.requests)
    with web.client() as c:
        run = scan_seller(db, seller, mode="incremental", client=c)
    later = [str(r.url) for r in web.requests[before:]]
    album_pages = [u for u in later if "/albums/" in u]
    assert sorted(album_pages) == sorted([f"{BASE}/albums/9999?uid=1", f"{BASE}/albums/{albums[10]['id']}?uid=1"])
    assert db.query(Product).filter_by(external_id="9999").one().analysis_state in ("analysed", "deep")
    assert run.products_new == 1 and run.products_updated == 1


def test_conditional_refresh_uses_etag(db):
    from app.ingestion.http import PoliteClient
    from app.ingestion.scan import scan_seller
    from tests.conftest import FakeWeb

    albums, images = _catalogue(2)
    seen_conditional = []
    web = FakeWeb()
    serve(web, albums, images)

    def handler(req: httpx.Request) -> httpx.Response:
        resp = web.handler(req)
        if "/albums/" in req.url.path:
            if req.headers.get("if-none-match") == '"v1"':
                seen_conditional.append(str(req.url))
                return httpx.Response(304, headers={"etag": '"v1"'})
            resp.headers["etag"] = '"v1"'
        return resp

    seller = _seller(db)
    with PoliteClient(delay=0, transport=httpx.MockTransport(handler)) as c:
        scan_seller(db, seller, client=c)
        scan_seller(db, seller, mode="full", client=c)  # full rescan re-reads every page, conditionally
    assert len(seen_conditional) == 2


def test_cancel_stops_scan_and_leaves_products_resumable(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Job, Product

    albums, images = _catalogue(5)
    serve(web, albums, images)
    seller = _seller(db)
    job = Job(kind="scan_seller", status="running", control="cancel")
    db.add(job)
    db.commit()
    with web.client() as c:
        run = scan_seller(db, seller, client=c, job_id=job.id)
    assert run.status == "cancelled" and not run.discovery_complete
    assert db.query(Product).filter(Product.seller_id == seller.id).count() == 0
    # Nothing was retired, and a later scan picks everything up.
    with web.client() as c:
        run2 = scan_seller(db, seller, client=c)
    assert run2.status == "succeeded" and db.query(Product).filter(Product.seller_id == seller.id).count() == 5


def test_pause_then_resume(db, web):
    from app.ingestion.scan import scan_seller
    from app.models import Job

    albums, images = _catalogue(3)
    serve(web, albums, images)
    seller = _seller(db)
    job = Job(kind="scan_seller", status="running", control="pause")
    db.add(job)
    db.commit()
    job_id = job.id

    def resume_later():
        time.sleep(1.5)
        from app.db import session_scope

        with session_scope() as s:
            s.get(Job, job_id).control = None

    t = threading.Thread(target=resume_later)
    t.start()
    t0 = time.monotonic()
    with web.client() as c:
        run = scan_seller(db, seller, client=c, job_id=job_id)
    t.join()
    assert run.status == "succeeded"
    assert time.monotonic() - t0 >= 1.0  # it waited while paused
    assert db.get(Job, job_id).stats["products_analysed"] == 3


def test_hosts_do_not_block_each_other():
    """A request waiting for one host's politeness slot must not delay a different host."""
    from app.ingestion.http import PoliteClient

    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    with PoliteClient(delay=0.6, respect_robots=False, transport=transport) as c:
        c.get("https://a.example/1")
        waiter = threading.Thread(target=lambda: c.get("https://a.example/2"))
        waiter.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        c.get("https://b.example/1")
        other_host = time.monotonic() - t0
        waiter.join()
    assert other_host < 0.3, other_host


def test_same_host_is_still_spaced_by_the_delay():
    from app.ingestion.http import PoliteClient

    stamps = []
    transport = httpx.MockTransport(lambda r: (stamps.append(time.monotonic()), httpx.Response(200, text="ok"))[1])
    with PoliteClient(delay=0.2, respect_robots=False, transport=transport) as c:
        threads = [threading.Thread(target=c.get, args=(f"https://a.example/{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:], strict=False)]
    assert min(gaps) >= 0.18, gaps


def test_server_errors_make_the_client_back_off():
    from app.ingestion.http import FetchError, PoliteClient

    transport = httpx.MockTransport(lambda r: httpx.Response(503, text="busy"))
    with PoliteClient(delay=0.0, respect_robots=False, transport=transport) as c:
        try:
            c.get("https://slow.example/x", retries=0)
        except FetchError:
            pass
        assert c.stats()["slow.example"]["delay"] >= 0.5


def test_upgrade_from_original_schema_keeps_data(tmp_path, monkeypatch):
    """A database at the original revision is migrated in place; existing products count as analysed."""
    import sqlalchemy as sa
    from alembic.config import Config

    from alembic import command

    db_url = f"sqlite:///{tmp_path / 'old.db'}"
    monkeypatch.setenv("WS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WS_DATABASE_URL", db_url)
    from app.config import get_settings

    get_settings.cache_clear()
    from app import db as dbmod

    dbmod.init_engine(db_url)
    ini = Path(dbmod.__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    command.upgrade(cfg, dbmod.INITIAL_REVISION)
    eng = dbmod.get_engine()
    with eng.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO products (seller_id, external_id, title_original, title_en, colours, materials, sizes, seasons, "
            "fit_tags, logo_level, currency, source_url, links, status, data_origin, completeness, is_new, first_seen_at, "
            "last_seen_at, updated_at) VALUES (NULL, 'x1', 't', 't', '[]', '[]', '[]', '[]', '[]', 'unknown', 'CNY', "
            "'https://e/x1', '{}', 'active', 'scraped', 'complete', 1, '2026-01-01', '2026-01-01', '2026-01-01')"
        ))  # fmt: skip
    dbmod.migrate()
    with eng.begin() as conn:
        row = conn.execute(
            sa.text("SELECT analysis_state, detail_fetched_at FROM products WHERE external_id='x1'")
        ).one()
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(product_images)"))}
    assert row[0] == "analysed" and row[1] is not None
    assert {"role", "selected", "thumb_url"} <= cols
    get_settings.cache_clear()
