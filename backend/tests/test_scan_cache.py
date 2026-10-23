"""Scan updates reuse unchanged quality assessments without changing the answers."""

from .test_caches import _listings


def test_scan_reassesses_changed_inputs_and_medians_only(db, monkeypatch):
    from app.models import Evidence, Product
    from app.services import presenter, revisions
    from app.services.recommender import active_products

    ps = _listings(db)
    for p in ps:
        p.price = 500
    db.commit()
    calls = []
    original = presenter.assess

    def counted(db, p, **kwargs):
        calls.append(p.id)
        return original(db, p, **kwargs)

    monkeypatch.setattr(presenter, "assess", counted)

    def answers():
        ctx = presenter.Context(db)
        products = active_products(db)
        ctx.prime_quality(products)
        return {p.id: ctx.quality(p) for p in products}

    answers()
    assert len(calls) == 6
    calls.clear()
    ps[0].price = 600  # unchanged category median: only this assessment changes
    db.commit()
    incremental = answers()
    assert calls == [ps[0].id]
    revisions.renew_instance(db.get_bind())
    assert answers() == incremental

    calls.clear()
    added = Product(seller_id=ps[0].seller_id, external_id="new", title_original="new",
                    source_url="https://cache.example/new", category=ps[0].category,
                    price=500, currency="CNY", data_origin="manual")
    db.add(added)
    db.flush()
    db.add(Evidence(product_id=added.id, kind="seller_claim", aspect="material", claim="wool"))
    db.commit()
    incremental = answers()
    assert len(calls) == 1  # inserting a listing doesn't reassess the other six
    revisions.renew_instance(db.get_bind())
    assert answers() == incremental

    for p in ps:
        p.price = 900  # now the median changes, including for the unchanged new listing
    db.commit()
    calls.clear()
    incremental = answers()
    assert len(calls) == 7
    revisions.renew_instance(db.get_bind())
    assert answers() == incremental


def test_changed_equivalent_offer_and_seller_name_refresh_value(db):
    from app.models import ProductRelation
    from app.services import presenter, revisions

    ps = _listings(db)
    db.add(ProductRelation(a_id=ps[0].id, b_id=ps[1].id, relation="confirmed_identical",
                           user_confirmed=True, score=1, signals={}))
    db.commit()
    before = presenter.Context(db).quality(ps[1])
    ps[0].price = 200
    ps[0].seller.name = "Renamed seller"
    db.commit()
    after = presenter.Context(db).quality(ps[1])
    assert after != before
    revisions.renew_instance(db.get_bind())
    assert presenter.Context(db).quality(ps[1]) == after
