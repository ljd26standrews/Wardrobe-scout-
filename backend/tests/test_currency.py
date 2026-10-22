"""Prices mean the same thing everywhere: medians, value, price judgements, filters and comparisons work on prices
converted into one currency at your stated rate; a price in an unstated currency stays unknown; landed cost is
an estimate for buying in yuan through an agent, separate from conversion.

SYNTHETIC catalogue: made-up listings and prices.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


def test_currency_codes_and_aliases():
    from app.services.money import code

    assert code("RMB") == code("rmb") == code(" cny ") == code("CNH") == "CNY"
    assert code("GBP") == "GBP" and code("US$") == "USD"
    assert code("") is None and code(None) is None
    assert code("¥") is None  # yuan or yen: not assumed


def test_conversion_only_at_a_stated_rate():
    from app.services.money import Rates

    r = Rates.from_settings({"cny_to_gbp": 0.1})
    assert r.gbp(510, "RMB") == pytest.approx(51.0) and r.gbp(510, "CNY") == pytest.approx(51.0)
    assert r.gbp(40, "GBP") == 40
    assert r.gbp(40, "USD") is None and r.gbp(40, "") is None and r.gbp(None, "CNY") is None
    r = Rates.from_settings({"cny_to_gbp": 0.1, "other_to_gbp": {"usd": 0.8}})
    assert r.gbp(40, "USD") == pytest.approx(32.0)
    assert r.convert(32.0, "GBP", "USD") == pytest.approx(40.0) and r.convert(10, "GBP", "JPY") is None


@pytest.fixture()
def client(env, monkeypatch):
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: 0)
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def tees(db):
    """Polo shirts priced in yuan (stated as CNY or RMB), in pounds, and in an unstated currency (a category the
    app's seed examples do not use)."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller

    s = Seller(name="cur", slug="cur", platform="manual")
    db.add(s)
    db.flush()
    out = {}
    for name, price, currency in (("cny300", 300, "CNY"), ("cny400", 400, "CNY"), ("cny500", 500, "CNY"),
                                  ("rmb450", 450, "RMB"), ("gbp40", 40, "GBP"), ("unknown", 999, "")):  # fmt: skip
        raw = RawListing(name, f"https://shop.example/{name}", f"white cotton polo shirt {name}", price=price,
                         currency=currency)  # fmt: skip
        out[name] = upsert_listing(db, s, raw).product
        out[name].category = "polo"
    db.commit()
    return out


def _rate(client, rate: float):
    cur = client.get("/api/settings").json()["currency"]
    assert client.put("/api/settings/currency", json={**cur, "cny_to_gbp": rate}).status_code == 200


# ----------------------------------------------------------------- medians and value
def test_the_category_median_compares_prices_in_one_currency(client, db):
    ps = tees(db)
    _rate(client, 0.1)
    by_id = {p["id"]: p for p in client.get("/api/products", params={"page_size": 50}).json()["items"]}
    # Convertible prices in pounds: 30, 40, 50, 45 (RMB is yuan) and 40 (GBP): median £40.
    assert by_id[ps["gbp40"].id]["quality"]["dimensions"]["value"]["price_position"] == pytest.approx(1.0)
    assert by_id[ps["cny500"].id]["quality"]["dimensions"]["value"]["price_position"] == pytest.approx(1.25)
    unknown = by_id[ps["unknown"].id]
    assert unknown["quality"]["dimensions"]["value"]["price_position"] is None  # not compared with anything
    assert unknown["price_gbp"] is None and unknown["landed_cost"] is None


def test_a_rate_change_moves_conversions_but_not_original_prices(client, db):
    ps = tees(db)
    _rate(client, 0.1)
    p = client.get(f"/api/products/{ps['rmb450'].id}").json()
    assert (p["price"], p["currency"], p["price_gbp"]) == (450, "RMB", 45.0)
    assert p["conversion"] == {"gbp": 45.0, "rate": 0.1, "from": "CNY", "source": p["conversion"]["source"],
                               "updated_at": p["conversion"]["updated_at"]}  # fmt: skip
    _rate(client, 0.2)
    p = client.get(f"/api/products/{ps['rmb450'].id}").json()
    assert (p["price"], p["price_gbp"]) == (450, 90.0)
    assert p["landed_cost"]["goods_gbp"] == 90.0  # landed cost builds on the conversion, and adds to it
    assert p["landed_cost"]["total_gbp"] > p["price_gbp"]


def test_landed_cost_is_estimated_only_for_buying_in_yuan(client, db):
    ps = tees(db)
    gbp = client.get(f"/api/products/{ps['gbp40'].id}").json()
    assert gbp["price_gbp"] == 40 and gbp["landed_cost"] is None  # a pound price: no agent route assumed
    assert client.get(f"/api/products/{ps['rmb450'].id}").json()["landed_cost"] is not None


# ----------------------------------------------------------------- filters
def test_price_filters_state_their_currency_and_convert(client, db):
    ps = tees(db)
    _rate(client, 0.1)

    def ids(**params):
        r = client.get("/api/products", params={"page_size": 50, "category": "polo", **params}).json()
        return {p["id"] for p in r["items"]}, r["price_filter"]

    got, flt = ids(max_price=45, price_currency="GBP")
    assert got == {ps[n].id for n in ("cny300", "cny400", "rmb450", "gbp40")}
    assert flt == {"currency": "GBP", "min": None, "max": 45.0, "unknown_currency_left_out": 1}
    got, flt = ids(max_price=420)  # no currency given: yuan, as the filter has always meant
    assert got == {ps[n].id for n in ("cny300", "cny400", "gbp40")} and flt["currency"] == "CNY"
    _rate(client, 0.2)  # the same filter in pounds now admits fewer yuan prices
    got, _ = ids(max_price=45, price_currency="GBP")
    assert got == {ps["gbp40"].id}


def test_a_price_filter_in_an_unknown_currency_is_refused(client, db):
    tees(db)
    assert client.get("/api/products", params={"max_price": 10, "price_currency": "XYZ"}).status_code == 422


# ----------------------------------------------------------------- "too expensive" judgements
def test_too_expensive_is_judged_in_one_currency_and_rates_are_applied_when_used(client, db):
    from app.services.recommender import build_profile

    ps = tees(db)
    _rate(client, 0.1)
    client.post(
        "/api/feedback", json={"product_id": ps["rmb450"].id, "action": "dislike", "reasons": ["too_expensive"]}
    )
    client.post(
        "/api/feedback", json={"product_id": ps["unknown"].id, "action": "dislike", "reasons": ["too_expensive"]}
    )
    prof = build_profile(db)
    assert prof.price_ceiling["polo"] == pytest.approx(45.0)  # the unknown-currency judgement cannot count
    assert prof.price_ceiling_source["polo"] == (450, "RMB")
    ins = client.get("/api/insights").json()
    assert ins["price_ceilings"]["polo"] == {"gbp": 45.0, "price": 450, "currency": "RMB"}

    def caution(name):
        p = client.get(f"/api/products/{ps[name].id}").json()
        return any("too expensive" in c for c in p["cons"] + (p.get("recommendation") or {}).get("cautions", []))

    assert caution("cny500")  # £50 against £45
    assert caution("gbp40") is False  # £40 is below the price you judged too much
    _rate(client, 0.05)  # yuan got cheaper: £25 judged too much; the £40 tee is now above it
    assert caution("gbp40") and not caution("cny300")


def test_too_expensive_is_a_value_judgement_not_a_budget(client, db):
    """Several judgements in one category do not ratchet down to the cheapest: only pieces above every price you
    judged too much for an unproven piece of that kind are affected, and nothing leaves Browse because of it."""
    from app.services.recommender import build_profile

    ps = tees(db)
    _rate(client, 0.1)
    for name in ("cny300", "rmb450"):
        client.post(
            "/api/feedback", json={"product_id": ps[name].id, "action": "dislike", "reasons": ["too_expensive"]}
        )
    assert build_profile(db).price_ceiling["polo"] == pytest.approx(45.0)
    listed = {p["id"] for p in client.get("/api/products", params={"page_size": 50}).json()["items"]}
    assert ps["cny500"].id in listed


# ----------------------------------------------------------------- comparisons
def test_comparisons_never_judge_across_currencies_or_with_listings_no_longer_for_sale(client, db):
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller
    from app.services import dedup

    s = Seller(name="alt", slug="alt", platform="manual")
    db.add(s)
    db.flush()

    def listing(ext, price, currency, status="active"):
        p = upsert_listing(db, s, RawListing(ext, f"https://alt.example/{ext}", "navy wool overcoat", price=price,
                                             currency=currency)).product  # fmt: skip
        p.status = status
        return p

    base = listing("base", 1000, "CNY")
    sold_out = listing("sold", 500, "CNY", status="sold_out")
    dollars = listing("usd", 60, "USD")
    unstated = listing("unk", 300, "")
    db.commit()
    for other in (sold_out, dollars, unstated):
        dedup.set_user_decision(db, base.id, other.id, True)
    _rate(client, 0.1)
    cmp = client.get(f"/api/products/{base.id}/compare").json()
    v = cmp["verdict"]
    assert v["recommendation"] == "insufficient_data"  # nothing comparable: no cheaper or dearer claim
    notes = " ".join(v["notes"])
    assert "no longer for sale" in notes and "currency is not stated" in notes and "USD" in notes
    p = client.get(f"/api/products/{base.id}").json()
    assert not any("cheaper" in n for n in p["quality"]["dimensions"]["value"]["notes"])  # value score unmoved

    cur = client.get("/api/settings").json()["currency"]
    client.put("/api/settings/currency", json={**cur, "other_to_gbp": {"USD": 0.8}})
    notes = " ".join(client.get(f"/api/products/{base.id}/compare").json()["verdict"]["notes"])
    assert "£48" in notes  # converted for reference, still not judged against a yuan price
