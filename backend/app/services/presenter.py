"""Builds the product view model consumed by the frontend."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Evidence, Product, SavedItem, Seller
from . import dedup
from .landed_cost import estimate_landed_cost
from .quality import assess, category_price_stats, evidence_view
from .recommender import Ranked
from .settings_store import get_setting

RELATION_LABEL = {
    "confirmed_identical": "Confirmed identical (you confirmed)",
    "same_purchase_link": "Same purchase link — same underlying listing",
    "shared_photos": "Several identical photographs — likely the same source photos (not proof of the same batch)",
    "similar_photo": "One similar photograph — may well be a different product",
    "visually_similar": "Visually similar — may be a different product",
}


class Context:
    """Per-request caches so list endpoints don't repeat expensive lookups."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.rate = get_setting(db, "currency")["cny_to_gbp"]
        self.landed_cfg = get_setting(db, "landed_cost")
        self.price_stats = category_price_stats(db)
        self.sellers = {s.id: s for s in db.query(Seller).all()}
        self.saved = {s.product_id: s for s in db.query(SavedItem).all()}
        self._seller_ev: dict[int, list[Evidence]] = {}
        self._quality: dict[int, dict] = {}
        self._strong: dict[int, list[int]] | None = None

    def seller_evidence(self, seller_id: int | None) -> list[Evidence]:
        if not seller_id:
            return []
        if seller_id not in self._seller_ev:
            self._seller_ev[seller_id] = (
                self.db.query(Evidence).filter(Evidence.seller_id == seller_id, Evidence.product_id.is_(None)).all()
            )
        return self._seller_ev[seller_id]

    def _strong_neighbours(self, product_id: int) -> list[int]:
        # Loaded once per request instead of one query per product (feeds rank many products).
        if self._strong is None:
            from ..models import ProductRelation

            self._strong = {}
            for r in self.db.query(ProductRelation).filter(ProductRelation.relation.in_(dedup.STRONG_RELATIONS)):
                self._strong.setdefault(r.a_id, []).append(r.b_id)
                self._strong.setdefault(r.b_id, []).append(r.a_id)
        return self._strong.get(product_id, [])

    def cheaper_equivalent(self, p: Product) -> dict | None:
        """A cheaper listing that is very likely the same item (purchase link, confirmed, or several shared photos)."""
        if not p.price:
            return None
        best = None
        for other_id in self._strong_neighbours(p.id):
            o = self.db.get(Product, other_id)
            if o and o.price and o.status == "active" and o.currency == p.currency and o.price < p.price * 0.9:
                if best is None or o.price < best.price:
                    best = o
        if best is None:
            return None
        return {
            "product_id": best.id,
            "price": best.price,
            "seller": self.sellers[best.seller_id].name if best.seller_id in self.sellers else "another seller",
            "saving_pct": (1 - best.price / p.price) * 100,
        }

    def quality(self, p: Product, design: float | None = None, design_conf: float | None = None) -> dict:
        key = p.id
        if key not in self._quality or design is not None:
            self._quality[key] = assess(
                self.db,
                p,
                seller_evidence=self.seller_evidence(p.seller_id),
                price_stats=self.price_stats,
                cheaper_equivalent=self.cheaper_equivalent(p),
                design_score=design,
                design_confidence=design_conf,
            )
        return self._quality[key]


def image_view(img) -> dict:
    # Photos not downloaded by the scan are fetched on demand when first shown (see /api/images/{id}).
    viewable = img.local_path or (img.fetch_status != "failed" and not img.url.startswith("upload:"))
    return {
        "id": img.id,
        "src": f"/api/images/{img.id}" if viewable else None,
        "remote_url": img.url if not img.url.startswith("upload:") else None,
        "status": img.fetch_status,
        "role": img.role,
        "selected": bool(img.selected),
    }


def gallery(p: Product) -> tuple[list, list]:
    """(garment photos in the seller's order, size charts). Text graphics and banners are left out."""
    from .image_select import NON_AESTHETIC_ROLES, SIZE_CHART

    imgs = sorted(p.images, key=lambda i: i.position)
    return [i for i in imgs if i.role not in NON_AESTHETIC_ROLES], [i for i in imgs if i.role == SIZE_CHART]


def product_card(ctx: Context, p: Product, ranked: Ranked | None = None, detail: bool = False) -> dict:
    seller = ctx.sellers.get(p.seller_id) if p.seller_id else None
    design = ranked.design_score if ranked else None
    q = ctx.quality(p, design, None if ranked is None else max(0.15, ranked.strength))
    price_gbp = (
        round(p.price * ctx.rate, 2)
        if p.price and p.currency in ("CNY", "")
        else (p.price if p.currency == "GBP" else None)
    )
    landed = estimate_landed_cost(p.price, p.currency, p.category, ctx.rate, ctx.landed_cfg)
    saved = ctx.saved.get(p.id)
    pros, cons = pros_and_cons(p, q, ranked)
    links = dict(p.links or {})
    if seller and seller.platform == "yupoo" and "yupoo" not in links and p.data_origin == "scraped":
        links["yupoo"] = p.source_url
    data = {
        "id": p.id,
        "title": p.title_en or p.title_original,
        "title_original": p.title_original,
        "seller": {
            "id": seller.id,
            "name": seller.name,
            "platform": seller.platform,
            "reputation": seller.reputation_status,
        }
        if seller
        else None,
        "seller_item_code": p.seller_item_code,
        "brand": p.brand,
        "category": p.category,
        "colours": p.colours or [],
        "materials": p.materials or [],
        "material_text": p.material_text,
        "gsm": p.gsm,
        "sizes": p.sizes or [],
        "seasons": p.seasons or [],
        "fit_tags": p.fit_tags or [],
        "logo_level": p.logo_level,
        "price": p.price,
        "currency": p.currency,
        "price_gbp": price_gbp,
        "landed_cost": landed,
        "source_url": p.source_url if not p.source_url.startswith("manual://") else None,
        "links": links,
        "images": [image_view(i) for i in gallery(p)[0]],
        "sizing_images": [image_view(i) for i in gallery(p)[1]],
        "analysis_state": p.analysis_state,
        "status": p.status,
        "data_origin": p.data_origin,
        "completeness": p.completeness,
        "is_new": p.is_new,
        "first_seen_at": p.first_seen_at.isoformat() if p.first_seen_at else None,
        "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
        "group_id": p.group_id,
        "quality": q,
        "pros": pros,
        "cons": cons,
        "saved": {"list": saved.list_name, "priority": saved.priority, "note": saved.note} if saved else None,
    }
    if ranked is not None:
        data["recommendation"] = {
            "score": round(ranked.score, 4),
            "design_score": round(ranked.design_score, 3),
            "components": {k: round(v, 3) for k, v in ranked.components.items()},
            "contributions": {k: round(v, 4) for k, v in ranked.contributions.items()},
            "reasons": ranked.reasons,
            "cautions": ranked.cautions,
            "nearest_positive": ranked.nearest_positive,
            "explore": ranked.explore,
        }
    if detail:
        data["description"] = p.description_en
        data["description_original"] = p.description_original
        data["translation_method"] = p.translation_method
        data["evidence"] = [evidence_view(e) for e in p.evidence] + [
            {**evidence_view(e), "scope": "seller"} for e in ctx.seller_evidence(p.seller_id)
        ]
        data["observations"] = [
            {
                "observed_at": o.observed_at.isoformat(),
                "price": o.price,
                "sizes": o.sizes,
                "status": o.status,
                "change": o.change,
                "source_url": o.source_url,
            }
            for o in p.observations
        ]
        data["missing"] = missing_information(p, q)
    return data


def missing_information(p: Product, q: dict) -> list[str]:
    miss = []
    if not p.materials:
        miss.append("Material composition not stated")
    elif q["dimensions"]["material"]["score"] is None:
        miss.append("Material composition is a seller claim — no independent verification")
    if not p.gsm and p.category in ("t-shirt", "sweatshirt", "hoodie"):
        miss.append("Fabric weight (GSM) not stated")
    if q["dimensions"]["construction"]["score"] is None:
        miss.append("No independent QC / in-hand evidence of construction")
    if q["dimensions"]["seller_reliability"]["score"] is None:
        miss.append("Seller reliability not yet assessed")
    if not p.sizes:
        miss.append("Available sizes not listed")
    if not p.price:
        miss.append("Price not found on the listing")
    if not p.images:
        miss.append("No photographs collected")
    return miss


def pros_and_cons(p: Product, q: dict, ranked: Ranked | None) -> tuple[list[str], list[str]]:
    pros: list[str] = []
    cons: list[str] = []
    dims = q["dimensions"]
    for name, label in (
        ("construction", "construction"),
        ("material", "material evidence"),
        ("seller_reliability", "seller track record"),
    ):
        d = dims[name]
        if d["score"] is not None and d["score"] >= 65:
            pros.append(f"Independent evidence supports {label} ({d['label'].lower()})")
        elif d["score"] is not None and d["score"] < 45:
            cons.append(f"Evidence raises concerns about {label}")
    if ranked:
        pros += [r for r in ranked.reasons if not r.startswith(("Cold start", "Exploration"))]
        cons += ranked.cautions
    if p.materials and any(m["fibre"] in ("cashmere", "virgin wool", "merino wool", "wool") for m in p.materials):
        pros.append(f"Listing claims {p.material_text} (seller claim)")
    if p.gsm and p.gsm >= 220:
        pros.append(f"Listing claims {p.gsm} g/m² fabric (seller claim)")
    if p.logo_level == "none":
        pros.append("No visible branding mentioned")
    for v in dims["value"].get("notes", []):
        if "cheaper" in v:
            cons.append(v)
    cons += missing_information(p, q)
    dedupe = lambda xs: list(dict.fromkeys(xs))  # noqa: E731
    return dedupe(pros)[:3], dedupe(cons)[:3]


def comparison(ctx: Context, p: Product) -> dict:
    rows = []
    for rel, other_id in dedup.relations_for(ctx.db, p.id):
        o = ctx.db.get(Product, other_id)
        if o is None:
            continue
        card = product_card(ctx, o)
        rows.append(
            {
                "relation": rel.relation,
                "relation_label": RELATION_LABEL.get(rel.relation, rel.relation),
                "score": rel.score,
                "signals": rel.signals,
                "user_confirmed": rel.user_confirmed,
                "product": card,
            }
        )
    order = ["confirmed_identical", "same_purchase_link", "shared_photos", "similar_photo", "visually_similar"]
    rows.sort(key=lambda r: (order.index(r["relation"]) if r["relation"] in order else 9, -(r["score"] or 0)))
    base = product_card(ctx, p, detail=True)
    verdict = price_verdict(base, rows)
    return {"product": base, "alternatives": rows, "verdict": verdict}


def price_verdict(base: dict, rows: list[dict]) -> dict:
    same = [r for r in rows if r["relation"] in ("confirmed_identical", "same_purchase_link", "shared_photos")]
    similar = [r for r in rows if r["relation"] in ("visually_similar", "similar_photo")]
    price = base["price"]
    notes = []
    recommendation = "insufficient_data"
    if price and same:
        cheaper = [r for r in same if r["product"]["price"] and r["product"]["price"] < price * 0.95]
        if cheaper:
            best = min(cheaper, key=lambda r: r["product"]["price"])
            bq = best["product"]["quality"]["quality_score"]
            mq = base["quality"]["quality_score"]
            if mq is not None and bq is not None and mq > bq + 10:
                notes.append(
                    "A cheaper listing appears to be the same product, but this listing has stronger quality "
                    "evidence — the premium may be justified."
                )
                recommendation = "premium_possibly_justified"
            else:
                notes.append(
                    f"{best['product']['seller']['name'] if best['product']['seller'] else 'Another seller'} lists what "
                    f"appears to be the same product for {best['product']['price']:g} "
                    f"({(1 - best['product']['price'] / price) * 100:.0f}% less). No evidence shows this "
                    "listing is better, so the higher price is not demonstrated."
                )
                recommendation = "cheaper_equivalent_available"
        else:
            notes.append("This is the cheapest of the listings that appear to be the same product.")
            recommendation = "best_price_among_matches"
        if any(r["relation"] == "shared_photos" and not r["user_confirmed"] for r in same):
            notes.append(
                "Matches based on shared photographs are not proof of the same batch — sellers re-use "
                "factory photos. Check QC photos before relying on this."
            )
    elif similar:
        better = [
            r
            for r in similar
            if (r["product"]["quality"]["quality_score"] or 0) > (base["quality"]["quality_score"] or 0) + 10
        ]
        if better:
            notes.append(f"{len(better)} visually similar alternative(s) have stronger quality evidence.")
            recommendation = "better_evidenced_alternative"
        else:
            notes.append("Only visually similar (not identical) alternatives were found; compare specs carefully.")
            recommendation = "similar_only"
    else:
        notes.append("No alternatives found in your collected catalogues yet.")
    return {"recommendation": recommendation, "notes": notes}
