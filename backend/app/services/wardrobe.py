"""Wardrobe analysis: gaps, redundancy, outfit combinations and purchase fit."""

from __future__ import annotations

import itertools
from collections import Counter

from sqlalchemy.orm import Session

from ..ingestion.vocab import CATEGORY_GROUP, NEUTRAL_COLOURS
from ..models import Product, SavedItem, WardrobeItem
from .settings_store import get_setting

COLOUR_FAMILY = {
    "black": "dark",
    "charcoal": "dark",
    "navy": "dark",
    "brown": "earth",
    "camel": "earth",
    "taupe": "earth",
    "beige": "light",
    "cream": "light",
    "white": "light",
    "grey": "mid",
    "khaki": "earth",
    "olive": "earth",
}

SEASON_SETS = {"autumn/winter": {"autumn", "winter"}, "spring/summer": {"spring", "summer"}}


def colour_family(colours: list[str] | None) -> str:
    for c in colours or []:
        if c in COLOUR_FAMILY:
            return COLOUR_FAMILY[c]
    return "accent" if colours else "unknown"


def _matches(item_colours: list[str], rule_colours: list[str]) -> bool:
    return not rule_colours or bool(set(item_colours or []) & set(rule_colours))


def colour_harmony(a: list[str], b: list[str]) -> float:
    """Simple rules for a neutral-led wardrobe: tonal and neutral contrasts score high."""
    if not a or not b:
        return 0.6
    ca, cb = a[0], b[0]
    if ca == cb:
        return 0.85  # tonal
    if ca in NEUTRAL_COLOURS and cb in NEUTRAL_COLOURS:
        clash = {frozenset({"black", "navy"}), frozenset({"black", "brown"})}
        return 0.6 if frozenset({ca, cb}) in clash else 0.9
    if ca in NEUTRAL_COLOURS or cb in NEUTRAL_COLOURS:
        return 0.7
    return 0.35


def _item_dict(w: WardrobeItem) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "category": w.category,
        "colours": w.colours or [],
        "seasons": w.seasons or [],
        "image": f"/api/files/{w.image_path}" if w.image_path else None,
        "kind": "owned",
    }


def analyse(db: Session, season: str | None = None, include_candidates: bool = True) -> dict:
    items = db.query(WardrobeItem).all()
    essentials = get_setting(db, "wardrobe_essentials")
    threshold = get_setting(db, "redundancy_threshold") or 3
    seasons_to_check = [season] if season in essentials else list(essentials)

    gaps = []
    for s in seasons_to_check:
        sset = SEASON_SETS.get(s, set())
        for rule in essentials[s]:
            have = [
                w
                for w in items
                if w.category == rule["category"]
                and _matches(w.colours, rule.get("colours", []))
                and (not w.seasons or set(w.seasons) & sset)
            ]
            if len(have) < rule.get("min", 1):
                gaps.append(
                    {
                        "season": s,
                        "category": rule["category"],
                        "colours": rule.get("colours", []),
                        "have": len(have),
                        "want": rule.get("min", 1),
                    }
                )

    combo = Counter((w.category or "uncategorised", colour_family(w.colours)) for w in items)
    redundancy = [
        {
            "category": c,
            "colour_family": f,
            "count": n,
            "advice": f"You own {n} {f} {c}s — consider one exceptional piece instead of another similar one.",
        }
        for (c, f), n in combo.items()
        if n >= threshold
    ]

    by_cat = Counter(w.category or "uncategorised" for w in items)
    outfits = suggest_outfits(db, items, season, include_candidates=include_candidates)
    return {
        "count": len(items),
        "by_category": dict(by_cat),
        "by_group": dict(Counter(CATEGORY_GROUP.get(w.category or "", "other") for w in items)),
        "gaps": gaps,
        "redundancy": redundancy,
        "outfits": outfits,
    }


def suggest_outfits(
    db: Session, items: list[WardrobeItem], season: str | None, limit: int = 12, include_candidates: bool = True
) -> list[dict]:
    pool = [_item_dict(w) for w in items]
    if include_candidates:
        saved = db.query(SavedItem).filter(SavedItem.list_name.in_(["loved", "liked", "wishlist"])).all()
        for s in saved:
            p = db.get(Product, s.product_id)
            if p:
                pool.append(
                    {
                        "id": p.id,
                        "name": p.title_en or p.title_original,
                        "category": p.category,
                        "colours": p.colours or [],
                        "seasons": p.seasons or [],
                        "kind": "candidate",
                        "image": next((f"/api/images/{i.id}" for i in p.images if i.local_path), None),
                    }
                )
    sset = SEASON_SETS.get(season or "", set())
    if sset:
        pool = [x for x in pool if not x["seasons"] or set(x["seasons"]) & sset]
    base = [x for x in pool if CATEGORY_GROUP.get(x["category"] or "") == "tops"]
    mid = [x for x in pool if CATEGORY_GROUP.get(x["category"] or "") == "mid-layers"]
    bottoms = [x for x in pool if CATEGORY_GROUP.get(x["category"] or "") == "bottoms"]
    outer = [x for x in pool if CATEGORY_GROUP.get(x["category"] or "") == "outerwear"]
    cold = not sset or "winter" in sset
    results = []
    tops = base + mid
    for top, bottom in itertools.product(tops, bottoms):
        layers = [[top, bottom]]
        if cold:
            for o in outer:
                layers.append([top, bottom, o])
            if top in base:
                for m in mid:
                    for o in outer[:3] or [None]:
                        layers.append([x for x in (top, m, bottom, o) if x])
        for combo in layers:
            pairs = list(itertools.combinations(combo, 2))
            score = sum(colour_harmony(a["colours"], b["colours"]) for a, b in pairs) / len(pairs)
            if cold and len(combo) >= 3:
                score += 0.05
            n_new = sum(1 for x in combo if x["kind"] == "candidate")
            results.append({"pieces": combo, "score": round(score, 3), "uses_candidates": n_new})
    results.sort(key=lambda r: (-r["score"], r["uses_candidates"]))
    # Diversity: don't repeat the same top more than twice
    seen: Counter = Counter()
    out = []
    for r in results:
        key = (r["pieces"][0]["kind"], r["pieces"][0]["id"])
        if seen[key] >= 2:
            continue
        seen[key] += 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def complement_scores(db: Session, products: list[Product]) -> dict[int, dict]:
    """Per-product wardrobe usefulness in [0,1] with an explanation."""
    items = db.query(WardrobeItem).all()
    if not items:
        return {}
    threshold = get_setting(db, "redundancy_threshold") or 3
    essentials = get_setting(db, "wardrobe_essentials")
    combo = Counter((w.category, colour_family(w.colours)) for w in items)
    by_cat = Counter(w.category for w in items)
    bottoms = [w for w in items if CATEGORY_GROUP.get(w.category or "") == "bottoms"]
    tops = [w for w in items if CATEGORY_GROUP.get(w.category or "") in ("tops", "mid-layers")]
    out = {}
    for p in products:
        fam = colour_family(p.colours)
        n_same = combo.get((p.category, fam), 0)
        gap_rule = None
        for s, rules in essentials.items():
            for rule in rules:
                if rule["category"] == p.category and _matches(p.colours, rule.get("colours", [])):
                    have = sum(
                        1
                        for w in items
                        if w.category == rule["category"] and _matches(w.colours, rule.get("colours", []))
                    )
                    if have < rule.get("min", 1):
                        gap_rule = (s, rule, have)
        score, note = 0.5, None
        if gap_rule:
            score, note = (
                0.8,
                f"Fills a gap: you have {gap_rule[2]} of {gap_rule[1]['min']} {p.category}s suggested for {gap_rule[0]}",
            )
        elif n_same >= threshold:
            score, note = 0.2, f"You already own {n_same} {fam} {p.category}s"
        elif n_same:
            score = 0.45
        group = CATEGORY_GROUP.get(p.category or "")
        partners = bottoms if group in ("tops", "mid-layers") else tops if group == "bottoms" else []
        if partners and p.colours:
            good = sum(1 for w in partners if colour_harmony(p.colours, w.colours) >= 0.8)
            if good >= 2 and score >= 0.45:
                score = min(1.0, score + 0.1)
                note = note or f"Pairs well with {good} pieces you own"
        if not by_cat.get(p.category) and not gap_rule and score == 0.5:
            score = 0.55
        out[p.id] = {"score": score, "note": note}
    return out
