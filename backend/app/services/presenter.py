"""Builds the product view model consumed by the frontend."""

from __future__ import annotations

import threading
from collections import namedtuple

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..models import Evidence, Product, ProductImage, SavedItem, Seller
from . import dedup, revisions
from .landed_cost import estimate_landed_cost
from .money import code, rates
from .quality import assess, category_price_stats, evidence_view
from .recommender import LISTING_COLUMNS, Ranked, active_products
from .settings_store import get_setting

RELATION_LABEL = {
    "confirmed_identical": "Confirmed identical (you confirmed)",
    "same_purchase_link": "Same purchase link — same underlying listing",
    "shared_photos": "Several identical photographs — likely the same source photos (not proof of the same batch)",
    "similar_photo": "One similar photograph — may well be a different product",
    "visually_similar": "Visually similar — may be a different product",
}


# Quality assessments use per-garment signatures (including evidence, median price and equivalent offers).
# Settings, relations, seller metadata, rates and database replacement invalidate the enclosing cache.
# A scan can add a listing and its evidence without throwing away every other garment assessment.
_quality_lock = threading.Lock()
_quality_cache: dict = {"key": None, "items": {}}


def _shared_quality(key: tuple) -> dict[int, tuple]:
    with _quality_lock:
        if _quality_cache["key"] != key:
            _quality_cache.update(key=key, items={})
        return _quality_cache["items"]


# Immutable scalar evidence records, grouped once per database revision. A newly scanned seller claim must not
# invalidate the assessment of every other garment. Include every column in each assessment's signature.
_EVIDENCE_COLUMNS = tuple(c.key for c in sa_inspect(Evidence).column_attrs)
_EvidenceRow = namedtuple("EvidenceRow", _EVIDENCE_COLUMNS)
_evidence_lock = threading.Lock()
_evidence_cache: dict = {"key": None, "products": {}, "sellers": {}}


def _evidence_snapshot(db: Session) -> tuple[dict, dict]:
    key = revisions.current(db, "evidence")
    with _evidence_lock:
        if _evidence_cache["key"] != key:
            products, sellers = {}, {}
            query = select(*[c.columns[0] for c in sa_inspect(Evidence).column_attrs]).order_by(Evidence.id)
            for row in db.execute(query):
                e = _EvidenceRow(*row)
                if e.product_id is not None:
                    products.setdefault(e.product_id, []).append(e)
                elif e.seller_id is not None:
                    sellers.setdefault(e.seller_id, []).append(e)
            _evidence_cache.update(key=key, products={k: tuple(v) for k, v in products.items()},
                                   sellers={k: tuple(v) for k, v in sellers.items()})
        return _evidence_cache["products"], _evidence_cache["sellers"]


def _load_children(db: Session, products: list[Product], attr: str, model, order) -> None:
    """Loads one collection (photos, evidence) for many pieces in a few queries instead of one per piece. Pieces
    whose collection is already loaded are left as they are."""
    todo = [p for p in products if attr in sa_inspect(p).unloaded]
    for i in range(0, len(todo), 900):
        chunk = todo[i : i + 900]
        found: dict[int, list] = {p.id: [] for p in chunk}
        for row in db.query(model).filter(model.product_id.in_(list(found))).order_by(order, model.id):
            found[row.product_id].append(row)
        for p in chunk:
            set_committed_value(p, attr, found[p.id])


def load_images(db: Session, products: list[Product]) -> None:
    """Photos for the cards about to be shown (ranking itself needs none)."""
    _load_children(db, products, "images", ProductImage, ProductImage.position)


def rebind(db: Session, items: list, offers: dict[int, list]) -> tuple[list, dict[int, list[Product]]]:
    """The items about to be shown (Ranked, or selective Candidates), with their listings and offers replaced by
    copies from this session, so a card may load photos, the seller and evidence. The originals (shared, read-only
    listings: recommender.Listing) are left untouched."""
    import dataclasses

    def product_of(it):
        return it.ranked.product if hasattr(it, "ranked") else it.product

    shown = [product_of(it).id for it in items]
    originals = {p.id: p for p in [product_of(it) for it in items] + [o for pid in shown for o in offers.get(pid, [])]}
    fresh = {p.id: p for p in db.query(Product).filter(Product.id.in_(originals))} if originals else {}
    for pid in originals.keys() - fresh.keys():  # deleted since it was chosen: shown as it was, with no photos
        fresh[pid] = Product(**{k: getattr(originals[pid], k) for k in LISTING_COLUMNS})
    out = []
    for it in items:
        if hasattr(it, "ranked"):
            out.append(
                dataclasses.replace(it, ranked=dataclasses.replace(it.ranked, product=fresh[it.ranked.product.id]))
            )
        else:
            out.append(dataclasses.replace(it, product=fresh[it.product.id]))
    return out, {pid: [fresh[o.id] for o in offers.get(pid, [])] for pid in shown}


class Context:
    """Per-request caches so list endpoints don't repeat expensive lookups."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.fx = rates(db)
        self.rate = self.fx.to_gbp.get("CNY")
        self.landed_cfg = get_setting(db, "landed_cost")
        self.price_stats = category_price_stats(db, self.fx)
        self.sellers = {s.id: s for s in db.query(Seller).all()}
        self.saved = {s.product_id: s for s in db.query(SavedItem).all()}
        self._quality: dict[int, dict] = {}
        self._strong: dict[int, list[int]] | None = None
        self.price_filter: dict | None = None  # the feed's price filter as applied (api/products.price_filter)
        rev = revisions.current(db, "relations", "settings", "sellers")
        self._shared = _shared_quality((rev, tuple(sorted(self.fx.to_gbp.items()))))
        self._offers: dict | None = None
        self._quality_inputs: dict[int, tuple] = {}
        self._evidence: tuple[dict, dict] | None = None
        self.hidden: dict | None = None  # categories you hid from Discover, and how many pieces that left out

    def load_evidence(self, products: list[Product]) -> None:
        """Evidence for the pieces whose quality assessment is not cached yet; a cached assessment needs none."""
        cold = [p for p in products if p.id not in self._quality]
        _load_children(self.db, cold, "evidence", Evidence, Evidence.id)

    def _quality_input(self, p) -> tuple:
        # Only fields read by assess(), including the current category median and other offers.
        # Each garment and seller evidence record is included; scans invalidate just changed inputs.
        if p.id not in self._quality_inputs:
            if self._evidence is None:
                self._evidence = _evidence_snapshot(self.db)
            cheaper = self.cheaper_equivalent(p)
            signature = (p.seller_id, p.category, p.price, p.currency,
                         self.price_stats.get(p.category or "other"),
                         tuple(cheaper.items()) if cheaper else None,
                         self._evidence[0].get(p.id, ()), self._evidence[1].get(p.seller_id, ()))
            self._quality_inputs[p.id] = (signature, cheaper)
        return self._quality_inputs[p.id]

    def prime_quality(self, products: list) -> None:
        """Assess changed inputs only, using bulk evidence and plain listing records, without constructing
        thousands of ORM products. Unchanged assessments survive a scan; changed medians affect their category."""
        cold = []
        for p in products:
            signature, _ = self._quality_input(p)
            hit = self._shared.get(p.id)
            if hit is not None and hit[0] == signature:
                self._quality[p.id] = hit[1]
            else:
                cold.append(p)
        for p in cold:
            self.quality(p)

    def seller_evidence(self, seller_id: int | None) -> list[Evidence]:
        if self._evidence is None:
            self._evidence = _evidence_snapshot(self.db)
        return list(self._evidence[1].get(seller_id, ()))

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
        """A cheaper listing that is very likely the same item (purchase link, confirmed, or several shared photos),
        still for sale, in the same stated currency: a price in another currency is bought another way (shipping,
        fees and import charges differ), so converting it would not say which costs less."""
        if not p.price or code(p.currency) is None:
            return None
        best = None
        for other_id in self._strong_neighbours(p.id):
            if self._offers is None:
                self._offers = {o.id: o for o in active_products(self.db)}
            o = self._offers.get(other_id)
            if (
                o
                and o.price
                and o.status == "active"
                and code(o.currency) == code(p.currency)
                and o.price < p.price * 0.9
            ):
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
        """Shared assessment with this request's design compatibility. Results are never mutated."""
        base = self._quality.get(p.id)
        if base is None:
            signature, cheaper = self._quality_input(p)
            hit = self._shared.get(p.id)
            base = hit[1] if hit is not None and hit[0] == signature else None
            if base is None:
                evidence = list(self._evidence[0].get(p.id, ()))
                base = assess(
                    self.db, p, evidence=evidence,
                    seller_evidence=self.seller_evidence(p.seller_id), price_stats=self.price_stats,
                    cheaper_equivalent=cheaper, fx=self.fx,
                )
                with _quality_lock:
                    self._shared[p.id] = (signature, base)
                    if len(self._shared) > 50000:
                        self._shared.pop(next(iter(self._shared)))
            self._quality[p.id] = base
        if design is None:
            return base
        dims = {**base["dimensions"], "design_compatibility": {
            "score": round(design * 100), "label": "Personal fit with your taste", "confidence": round(design_conf or 0.0, 2),
        }}  # fmt: skip
        return {**base, "dimensions": dims}


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
    gbp = ctx.fx.gbp(p.price, p.currency)
    price_gbp = None if gbp is None else round(gbp, 2)
    conversion = None
    if price_gbp is not None and code(p.currency) != "GBP":  # how the pound figure was obtained
        conversion = {"gbp": price_gbp, "rate": ctx.fx.to_gbp[code(p.currency)], "from": code(p.currency),
                      "source": ctx.fx.source, "updated_at": ctx.fx.updated_at}  # fmt: skip
    landed = estimate_landed_cost(p.price, p.currency, p.category, ctx.rate, ctx.landed_cfg) if ctx.rate else None
    saved = ctx.saved.get(p.id)
    pros, cons = pros_and_cons(p, q, ranked)
    links = dict(p.links or {})
    shown, sizing = gallery(p)
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
        "conversion": conversion,
        "landed_cost": landed,
        "source_url": p.source_url if not p.source_url.startswith("manual://") else None,
        "links": links,
        "images": [image_view(i) for i in shown],
        "sizing_images": [image_view(i) for i in sizing],
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
        "saved": {
            "list": saved.list_name,
            "priority": saved.priority,
            "note": saved.note,
            "target_size": saved.target_size,
        }
        if saved
        else None,
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


def _not_compared(r: dict, currency: str | None) -> str | None:
    """Why a listing of the same product is not price-compared with this one, or None."""
    o = r["product"]
    if o.get("status", "active") != "active":
        return "sold"
    if not o.get("price"):
        return "unpriced"
    if code(o.get("currency")) is None:
        return "unstated"
    if code(o.get("currency")) != currency:
        return "other"
    return None


NOT_COMPARED = {
    "sold": "{n} that appear to be the same product are no longer for sale",
    "unpriced": "{n} that appear to be the same product show no price",
    "unstated": "{n} that appear to be the same product have a price whose currency is not stated",
}


def _seller_name(card: dict) -> str:
    return card["seller"]["name"] if card.get("seller") else "Another seller"


def price_verdict(base: dict, rows: list[dict]) -> dict:
    same = [r for r in rows if r["relation"] in ("confirmed_identical", "same_purchase_link", "shared_photos")]
    similar = [r for r in rows if r["relation"] in ("visually_similar", "similar_photo")]
    price = base["price"]
    notes = []
    recommendation = "insufficient_data"
    currency = code(base.get("currency"))
    # Prices are judged like for like only: the same stated currency (one way of buying) and still for sale.
    why = {id(r): _not_compared(r, currency) for r in same}
    comparable = [r for r in same if why[id(r)] is None] if price and currency else []
    if price and same and not currency:
        notes.append("This listing's currency is not stated, so its price was not compared with other listings.")
    elif price and same:
        for reason, text in NOT_COMPARED.items():
            if n := sum(1 for r in same if why[id(r)] == reason):
                notes.append(text.format(n=f"{n} listing{'s' if n > 1 else ''}") + ", so they were not compared.")
        for r in (r for r in same if why[id(r)] == "other"):
            o = r["product"]
            gbp = o.get("price_gbp")
            converted = f"; at your rate that is about £{gbp:,.0f}" if gbp is not None else ""
            if "price_gbp" in o and gbp is None:
                converted = ", a currency without a rate in Settings → Currency"
            notes.append(f"{_seller_name(o)} lists what appears to be the same product at {o['price']:g} "
                         f"{code(o.get('currency'))}{converted}. Bought in another currency, shipping, fees and import "
                         "charges differ, so the prices are not compared.")  # fmt: skip
    if price and comparable:
        same = comparable
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
    elif not same:
        notes.append("No alternatives found in your collected catalogues yet.")
    return {"recommendation": recommendation, "notes": notes}
