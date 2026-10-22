"""UK landed-cost estimate.

This is an *estimate* built from editable assumptions (Settings -> Landed
cost). It shows every component so you can see what drives the total. UK
rules summarised (check gov.uk for your situation):

* Import VAT (20%) applies to goods + shipping.
* Customs duty applies when the consignment's goods value exceeds £135;
  clothing typically falls in the 12% band.
* Carriers may add a handling fee when collecting VAT/duty.
"""

from __future__ import annotations

from .money import code


def estimate_landed_cost(
    price: float | None, currency: str, category: str | None, rate_cny_gbp: float, cfg: dict
) -> dict | None:
    """The estimate for a price in yuan bought through an agent. None for any other price: a pound or dollar price
    is bought another way (its shipping and charges are unknown here), and an unstated currency is never assumed.
    Such prices are converted (money.py), not landed."""
    if price is None or price <= 0 or code(currency) != "CNY":
        return None
    goods_gbp = price * rate_cny_gbp

    domestic = cfg.get("domestic_shipping_cny", 0) * rate_cny_gbp
    agent_fee = goods_gbp * cfg.get("agent_service_fee_pct", 0) / 100
    weight = cfg.get("category_weight_kg", {}).get(category or "", 0.8)
    intl = cfg.get("intl_base_gbp", 0) + weight * cfg.get("intl_per_kg_gbp", 0)
    duty = goods_gbp * cfg.get("duty_pct", 0) / 100 if goods_gbp > cfg.get("duty_threshold_gbp", 135) else 0.0
    vat = (goods_gbp + intl + duty) * cfg.get("vat_pct", 0) / 100
    handling = cfg.get("carrier_handling_fee_gbp", 0) if (vat or duty) else 0.0
    total = goods_gbp + domestic + agent_fee + intl + duty + vat + handling
    return {
        "goods_gbp": round(goods_gbp, 2),
        "domestic_shipping_gbp": round(domestic, 2),
        "agent_fee_gbp": round(agent_fee, 2),
        "international_shipping_gbp": round(intl, 2),
        "estimated_weight_kg": weight,
        "duty_gbp": round(duty, 2),
        "vat_gbp": round(vat, 2),
        "handling_gbp": round(handling, 2),
        "total_gbp": round(total, 2),
        "is_estimate": True,
        "assumptions": "Shipping assumes the item is sent alone; consolidating several items usually lowers per-item "
        "shipping but may push the parcel over the £135 duty threshold.",
    }
