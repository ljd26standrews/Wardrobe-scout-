"""Initial data from the brief.

* Five Yupoo sellers as *investigation candidates* (reputation: unassessed).
* Four style examples from MadeByKungFu / Aristide stored as `seed` records
  with `completeness="seed_unverified"`: title, price and link come from your
  historical notes, NOT from a live page. They carry no images until a scan or
  link refresh retrieves the real listing. Their Weidian URLs are added as
  watched links so the app attempts verification automatically.
* The Aristide Brunello Cucinelli white T-shirt is recorded as a rejection with
  reason "too expensive for the quality" - which only sets a value ceiling for
  T-shirts and never a negative preference for white or heavyweight tees.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .ingestion.parsing import materials_to_text
from .models import Feedback, Product, SavedItem, Seller, WatchedUrl
from .services import embeddings as emb
from .services.recommender import snapshot

SELLERS = [
    ("MadeByKungFu", "madebykungfu", "https://madebykungfu.x.yupoo.com", 2),
    ("Aristide", "aristide", "https://aristide.x.yupoo.com", 2),
    ("EthanHomme", "ethanhomme", "https://ethanhomme.x.yupoo.com", 3),
    ("TopClassic", "topclassic", "https://topclassic.x.yupoo.com", 3),
    ("Stitches", "stitches-reps-2", "https://stitches-reps-2.x.yupoo.com", 3),
]

SEED_PRODUCTS = [
    {
        "seller": "madebykungfu",
        "external_id": "7707075268",
        "code": "8009",
        "title": "Acne Studios wool/cashmere hoodie",
        "brand": "Acne Studios",
        "category": "hoodie",
        "materials": [{"fibre": "wool", "pct": None}, {"fibre": "cashmere", "pct": None}],
        "colours": [],
        "price": 919.0,
        "url": "https://weidian.com/item.html?itemID=7707075268",
        "seasons": ["autumn", "winter"],
    },
    {
        "seller": "madebykungfu",
        "external_id": "7872986192",
        "code": "9109",
        "title": "Maison Margiela black wool/cashmere double-zip jacket",
        "brand": "Maison Margiela",
        "category": "zip jacket",
        "materials": [{"fibre": "wool", "pct": None}, {"fibre": "cashmere", "pct": None}],
        "colours": ["black"],
        "price": 528.0,
        "url": "https://weidian.com/item.html?itemID=7872986192",
        "seasons": ["autumn", "winter"],
    },
    {
        "seller": "madebykungfu",
        "external_id": "7442542847",
        "code": None,
        "title": "Virgin-wool casual trousers",
        "brand": None,
        "category": "trousers",
        "materials": [{"fibre": "virgin wool", "pct": None}],
        "colours": [],
        "price": 399.0,
        "url": "https://weidian.com/item.html?itemID=7442542847",
        "seasons": ["autumn", "winter", "spring"],
    },
    {
        "seller": "aristide",
        "external_id": "7831390631",
        "code": None,
        "title": "Loro Piana-style Merano trousers",
        "brand": "Loro Piana",
        "category": "trousers",
        "materials": [],
        "colours": [],
        "price": 410.0,
        "url": "https://weidian.com/item.html?itemID=7831390631",
        "seasons": [],
    },
]

TEE = {
    "seller": "aristide",
    "external_id": "seed-bc-white-tee",
    "title": "Brunello Cucinelli plain white cotton T-shirt",
    "brand": "Brunello Cucinelli",
    "category": "t-shirt",
    "materials": [{"fibre": "cotton", "pct": None}],
    "colours": ["white"],
    "price": 510.0,
    "url": "manual://seed/aristide-bc-white-tee",
}


def _make_product(db: Session, sellers: dict[str, Seller], d: dict) -> tuple[Product, bool]:
    seller = sellers[d["seller"]]
    p = db.query(Product).filter_by(seller_id=seller.id, external_id=d["external_id"]).one_or_none()
    if p:
        return p, False
    p = Product(
        seller_id=seller.id,
        external_id=d["external_id"],
        seller_item_code=d.get("code"),
        title_original=d["title"],
        title_en=d["title"],
        brand=d.get("brand"),
        category=d.get("category"),
        materials=d.get("materials", []),
        material_text=materials_to_text(d.get("materials", [])) or None,
        colours=d.get("colours", []),
        seasons=d.get("seasons", []),
        price=d.get("price"),
        currency="CNY",
        source_url=d["url"],
        links={"weidian": d["url"]} if d["url"].startswith("http") else {},
        data_origin="seed",
        completeness="seed_unverified",
        is_new=False,
        logo_level="unknown",
        description_en="Seed record from your notes (historical price). Not yet verified against the live listing.",
    )
    db.add(p)
    db.flush()
    p.text_embedding = emb.to_bytes(emb.embed_text(emb.product_text(p)))
    return p, True


def seed(db: Session) -> dict:
    sellers: dict[str, Seller] = {}
    created_sellers = 0
    for name, slug, url, prio in SELLERS:
        s = db.query(Seller).filter_by(slug=slug).one_or_none()
        if s is None:
            s = Seller(
                name=name,
                slug=slug,
                platform="yupoo",
                base_url=url,
                priority=prio,
                notes="Initial investigation candidate from the brief — not a proven high-quality seller.",
            )
            db.add(s)
            created_sellers += 1
        sellers[slug] = s
    db.flush()

    created_products = 0
    for d in SEED_PRODUCTS:
        p, created = _make_product(db, sellers, d)
        if created:
            created_products += 1
            db.add(
                Feedback(
                    product_id=p.id,
                    action="like",
                    reasons=[],
                    context={"seed": True},
                    note="Initial style example from the brief — not a verified quality endorsement.",
                    snapshot=snapshot(p),
                )
            )
            db.add(
                SavedItem(product_id=p.id, list_name="liked", priority=2, note="Seed style example (historical price)")
            )
        if not db.query(WatchedUrl).filter_by(url=d["url"]).first():
            db.add(WatchedUrl(url=d["url"], seller_id=sellers[d["seller"]].id, product_id=p.id))

    tee, created = _make_product(db, sellers, TEE)
    if created:
        created_products += 1
        tee.description_en = (
            "Removed from your shortlist because its value was not sufficiently demonstrated. "
            "This is NOT a negative preference for heavyweight white T-shirts."
        )
        db.add(
            Feedback(
                product_id=tee.id,
                action="dislike",
                reasons=["too_expensive"],
                context={"seed": True},
                note="Value not demonstrated at ¥510 — do not infer dislike of white tees.",
                snapshot=snapshot(tee),
            )
        )
    db.commit()
    return {"sellers_created": created_sellers, "products_created": created_products}
