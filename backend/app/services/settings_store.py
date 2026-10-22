"""User-editable settings persisted in the `app_settings` table.

Defaults below encode the *initial* taste profile from the brief. They are a
cold-start prior only: as feedback accumulates the recommender shifts weight
from these rules to learned preferences.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.orm import Session

from ..models import AppSetting

DEFAULTS: dict[str, Any] = {
    "taste_profile": {
        "preferred_colours": [
            "black",
            "charcoal",
            "grey",
            "cream",
            "white",
            "beige",
            "taupe",
            "brown",
            "navy",
            "camel",
        ],
        "preferred_fibres": [
            "cashmere",
            "merino wool",
            "virgin wool",
            "wool",
            "cotton",
            "camel hair",
            "silk",
            "alpaca",
        ],
        "avoid_fibres": ["acrylic", "polyester"],
        "preferred_categories": [
            "t-shirt",
            "trousers",
            "hoodie",
            "sweatshirt",
            "knitwear",
            "cardigan",
            "zip jacket",
            "jacket",
            "overcoat",
            "down jacket",
        ],
        "preferred_fits": ["relaxed", "oversized", "straight", "wide"],
        "avoid_fits": ["slim", "skinny"],
        "logo_preference": "none_or_subtle",
        "favourite_brands": [
            "Loro Piana",
            "Brunello Cucinelli",
            "Maison Margiela",
            "Acne Studios",
            "The Row",
            "Zegna",
            "Jil Sander",
            "Lemaire",
            "Vince",
            "Our Legacy",
            "Auralee",
        ],
        "season_focus": ["autumn", "winter"],
        "notes": "Quiet luxury; substantial fabrics; relaxed but not baggy; no conspicuous logos. "
        "Favourite: plain white Vince T-shirt with a substantial cotton hand.",
    },
    "ranking_weights": {
        "taste": 3.0,  # Bayesian taste model (default engine); includes your written profile as its prior
        "rules": 1.0,  # legacy engine only: written preferences (fades as feedback grows)
        "learned_attributes": 1.2,
        "visual": 1.3,
        "semantic": 0.8,
        "quality": 0.9,
        "value": 0.6,
        "wardrobe": 0.5,
        "seller": 0.3,
    },
    "recommender": {
        "engine": "bayes",  # "bayes" (default) or "legacy" (pre-audit hand-weighted score)
        "exploration_rate": 0.18,
        "cold_start_k": 15,  # decisions needed before learned signals carry ~50% weight
        "skip_resurface_days": 10,
        "near_duplicate_threshold": 0.96,
    },
    # Selective recommendation (docs/selective-recommendation): a piece enters the main feed only when you
    # are `*_confidence` sure its approval probability is at least `*_target`. The targets are aspirations to
    # validate on your own decisions, not achieved precision.
    "selective": {
        "enabled": True,  # feature flag: off = the previous ranked feed everywhere
        "mode": "balanced",  # balanced | high | extreme
        "balanced_target": 0.80,
        "balanced_confidence": 0.50,
        "high_target": 0.90,
        "high_confidence": 0.80,
        "extreme_target": 0.95,
        "extreme_confidence": 0.90,
        "min_labels": 30,  # rated garment families needed before approval estimates exist
        "min_each_class": 5,  # ... including at least this many approvals and this many non-approvals
        "recalibrate_every": 5,  # new rated families before the calibration is refitted
        "require_quality_evidence": False,  # strict modes: only garments with independent quality evidence
        "exclude_seller_concerns": True,  # strict modes: not from sellers with recorded concerns
        "learning_min_estimate": 0.35,  # Learning picks: plausible enough to be worth your time
        "trial": False,  # opt-in prospective comparison with the previous ranking (daily switchback)
    },
    # Discover: categories you chose to hide from its feeds (Browse still lists everything). Only ever set by you:
    # "already own" judgements never hide a category, they affect pieces like the one you own.
    "discover": {"hidden_categories": []},
    "currency": {
        "cny_to_gbp": 0.105,
        "rate_source": "default assumption - update in Settings or refresh from ECB",
        "rate_updated_at": None,
        # Pounds per unit of any other currency you want converted (e.g. {"USD": 0.79}). A currency without a
        # rate stays unconverted: its prices are shown as listed and left out of medians, filters and judgements.
        "other_to_gbp": {},
    },
    "landed_cost": {
        "domestic_shipping_cny": 12.0,
        "agent_service_fee_pct": 5.0,
        "intl_base_gbp": 6.0,
        "intl_per_kg_gbp": 9.0,
        "vat_pct": 20.0,
        "duty_pct": 12.0,
        "duty_threshold_gbp": 135.0,
        "carrier_handling_fee_gbp": 0.0,
        "category_weight_kg": {
            "t-shirt": 0.3,
            "polo": 0.35,
            "shirt": 0.4,
            "sweatshirt": 0.75,
            "hoodie": 0.85,
            "knitwear": 0.7,
            "cardigan": 0.75,
            "trousers": 0.6,
            "jeans": 0.8,
            "shorts": 0.35,
            "zip jacket": 1.0,
            "jacket": 1.1,
            "blazer": 1.1,
            "overcoat": 1.8,
            "down jacket": 1.4,
            "gilet": 0.7,
            "footwear": 1.6,
            "accessories": 0.3,
            "bag": 1.0,
        },
    },
    "ai": {
        "enabled": False,
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "monthly_cap_usd": 5.0,
        "min_seconds_between_calls": 2.0,
        "price_input_per_mtok": 4.0,
        "price_output_per_mtok": 20.0,
        "use_for_translation": True,
        "use_for_visual_assessment": True,
        "use_for_research_summaries": True,
    },
    "schedule": {
        "enabled": True,
        "check_every_minutes": 60,
        "default_interval_hours": 24,
        "max_products_per_scan": 5000,  # products fully analysed per scan; the rest stay browsable and continue next scan
    },
    "community": {
        "reddit_enabled": False,
        "subreddits": ["FashionReps", "DesignerReps", "QualityReps", "RepsMenswear"],
    },
    "budget": {"monthly_gbp": None, "season_gbp": None},
    "wardrobe_essentials": {
        "autumn/winter": [
            {"category": "t-shirt", "colours": ["white", "cream"], "min": 2},
            {"category": "t-shirt", "colours": ["black", "charcoal", "grey", "navy"], "min": 2},
            {"category": "knitwear", "colours": [], "min": 2},
            {"category": "cardigan", "colours": [], "min": 1},
            {"category": "hoodie", "colours": [], "min": 1},
            {"category": "sweatshirt", "colours": [], "min": 1},
            {"category": "trousers", "colours": [], "min": 3},
            {"category": "zip jacket", "colours": [], "min": 1},
            {"category": "overcoat", "colours": [], "min": 1},
        ],
        "spring/summer": [
            {"category": "t-shirt", "colours": [], "min": 4},
            {"category": "shirt", "colours": [], "min": 2},
            {"category": "trousers", "colours": [], "min": 2},
            {"category": "shorts", "colours": [], "min": 1},
            {"category": "jacket", "colours": [], "min": 1},
        ],
    },
    "redundancy_threshold": 3,
}


# Valid ranges for numeric settings: values outside them are rejected when saved and clamped when read
# (so a bad value stored earlier cannot break ranking). None = "unset" is allowed where the default is None.
BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "recommender": {
        "exploration_rate": (0.0, 0.5),
        "cold_start_k": (1, 1000),
        "skip_resurface_days": (0, 365),
        "near_duplicate_threshold": (0.5, 1.0),
    },
    "currency": {"cny_to_gbp": (0.001, 10.0)},
    "landed_cost": {
        "domestic_shipping_cny": (0, 10000),
        "agent_service_fee_pct": (0, 100),
        "intl_base_gbp": (0, 10000),
        "intl_per_kg_gbp": (0, 10000),
        "vat_pct": (0, 100),
        "duty_pct": (0, 100),
        "duty_threshold_gbp": (0, 100000),
        "carrier_handling_fee_gbp": (0, 10000),
    },
    "ai": {
        "monthly_cap_usd": (0, 1000),
        "min_seconds_between_calls": (0, 600),
        "price_input_per_mtok": (0, 1000),
        "price_output_per_mtok": (0, 1000),
    },
    "schedule": {
        "check_every_minutes": (5, 1440),
        "default_interval_hours": (1, 24 * 90),
        "max_products_per_scan": (1, 100000),
    },
    "budget": {"monthly_gbp": (0, 1_000_000), "season_gbp": (0, 1_000_000)},
}
BOUNDS["selective"] = {
    **{f"{m}_target": (0.5, 0.99) for m in ("balanced", "high", "extreme")},
    **{f"{m}_confidence": (0.5, 0.99) for m in ("balanced", "high", "extreme")},
    "min_labels": (20, 1000),
    "min_each_class": (3, 500),
    "recalibrate_every": (1, 100),
    "learning_min_estimate": (0.0, 0.9),
}
CHOICES: dict[str, dict[str, set[str]]] = {
    "recommender": {"engine": {"bayes", "legacy"}},
    "selective": {"mode": {"balanced", "high", "extreme"}},
}
BOOLS: dict[str, set[str]] = {
    "selective": {"enabled", "require_quality_evidence", "exclude_seller_concerns", "trial"},
}


def _bounds(key: str, value: dict) -> dict[str, tuple[float, float]]:
    if key == "ranking_weights":  # every weight, including any stored by an older version
        return {k: (0.0, 5.0) for k in value}
    return BOUNDS.get(key, {})


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and v == v  # excludes bool and NaN


def validate_setting(key: str, value: Any) -> list[str]:
    """Problems with a value about to be saved (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return errors
    for field, (lo, hi) in _bounds(key, value).items():
        if field not in value:
            continue
        v = value[field]
        if v is None and field in DEFAULTS[key] and DEFAULTS[key][field] is None:
            continue
        if not _is_number(v):
            errors.append(f"{field} must be a number")
        elif not lo <= v <= hi:
            errors.append(f"{field} must be between {lo:g} and {hi:g}")
    for field, allowed in CHOICES.get(key, {}).items():
        if field in value and value[field] not in allowed:
            errors.append(f"{field} must be one of {', '.join(sorted(allowed))}")
    for field in BOOLS.get(key, set()):
        if field in value and not isinstance(value[field], bool):
            errors.append(f"{field} must be true or false")
    if key == "selective" and not errors:
        merged = {**DEFAULTS["selective"], **value}
        for part in ("target", "confidence"):  # stricter modes must stay at least as strict
            b, h, e = (merged[f"{m}_{part}"] for m in ("balanced", "high", "extreme"))
            if not b <= h <= e:
                errors.append(f"{part}s must not decrease from balanced to high to extreme")
    if key == "currency" and "other_to_gbp" in value:
        from .money import code

        rates = value["other_to_gbp"]
        if not isinstance(rates, dict):
            errors.append("other_to_gbp must map currency codes to pounds per unit")
        else:
            for cur, rate in rates.items():
                if code(cur) is None:
                    errors.append(f"other_to_gbp: {cur!r} is not a three-letter currency code")
                elif not _is_number(rate) or not 0.0001 <= rate <= 10000:
                    errors.append(f"other_to_gbp.{cur} must be between 0.0001 and 10000")
    if key == "discover" and "hidden_categories" in value:
        cats = value["hidden_categories"]
        if not isinstance(cats, list) or not all(isinstance(c, str) and 0 < len(c) <= 40 for c in cats):
            errors.append("hidden_categories must be a list of category names")
        elif len(cats) > 100:
            errors.append("hidden_categories: at most 100")
    if key == "landed_cost" and isinstance(value.get("category_weight_kg"), dict):
        for cat, w in value["category_weight_kg"].items():
            if not _is_number(w) or not 0 <= w <= 50:
                errors.append(f"category_weight_kg.{cat} must be between 0 and 50")
    return errors


def _sanitise(key: str, value: Any) -> Any:
    """Clamp stored numbers into range; replace wrong types with the default."""
    if not isinstance(value, dict):
        return value
    for field, (lo, hi) in _bounds(key, value).items():
        v = value.get(field)
        default = DEFAULTS[key].get(field, 0.0)
        if v is None and default is None:
            continue
        if not _is_number(v):
            value[field] = copy.deepcopy(default)
        elif not lo <= v <= hi:
            value[field] = type(v)(min(max(v, lo), hi))
    for field, allowed in CHOICES.get(key, {}).items():
        if value.get(field) not in allowed:
            value[field] = DEFAULTS[key][field]
    for field in BOOLS.get(key, set()):
        if not isinstance(value.get(field), bool):
            value[field] = DEFAULTS[key][field]
    return value


def _merge(default: Any, stored: Any) -> Any:
    if isinstance(default, dict) and isinstance(stored, dict):
        out = copy.deepcopy(default)
        for k, v in stored.items():
            out[k] = _merge(default.get(k), v) if k in default else v
        return out
    return copy.deepcopy(stored) if stored is not None else copy.deepcopy(default)


def get_setting(db: Session, key: str) -> Any:
    row = db.get(AppSetting, key)
    default = DEFAULTS.get(key)
    if row is None:
        return copy.deepcopy(default)
    return _sanitise(key, _merge(default, row.value)) if default is not None else row.value


def set_setting(db: Session, key: str, value: Any) -> Any:
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return get_setting(db, key)


def all_settings(db: Session) -> dict[str, Any]:
    keys = set(DEFAULTS) | {r.key for r in db.query(AppSetting).all() if not r.key.startswith("_")}
    return {k: get_setting(db, k) for k in sorted(keys)}
