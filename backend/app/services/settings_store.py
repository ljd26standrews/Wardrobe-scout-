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
    "currency": {
        "cny_to_gbp": 0.105,
        "rate_source": "default assumption - update in Settings or refresh from ECB",
        "rate_updated_at": None,
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
    return _merge(default, row.value) if default is not None else row.value


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
