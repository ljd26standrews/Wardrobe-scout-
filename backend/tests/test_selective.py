"""The selective gate, its feeds and its log (selective recommendation, M4).

SYNTHETIC FIXTURE: the decisions below are drawn from known approval probabilities per colour and garment type.
The tests check the gate's rules (thresholds never relax, exclusions keep their reasons, Learning picks stay
separate and carry their inclusion probabilities, failures degrade honestly). They say nothing about how well the
app predicts a real person's taste.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH

# Known approval probabilities (synthetic).
TRUTH = {("navy", "overcoat"): 0.97, ("navy", "trousers"): 0.92, ("black", "overcoat"): 0.85,
         ("grey", "trousers"): 0.45, ("red", "hoodie"): 0.04, ("red", "trousers"): 0.10}  # fmt: skip


@pytest.fixture()
def client(env, monkeypatch):
    from app.services import jobs

    monkeypatch.setattr(jobs, "enqueue", lambda kind, params=None: jobs.run_sync(kind, params))
    monkeypatch.setenv("WS_DOWNLOAD_IMAGES", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def world(db, per=40, rated=32, seed=0, calibrate=True):
    """Six kinds of garment with known approval probabilities; ``rated`` of each kind get a decision."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, Seller
    from app.services import calibration
    from app.services import embeddings as emb

    rng = np.random.default_rng(seed)
    sellers = [Seller(name=f"sel{i}", slug=f"sel{i}", platform="manual") for i in range(4)]
    db.add_all(sellers)
    db.flush()
    backend = emb.get_image_backend().name
    out, i = [], 0
    for (colour, cat), pt in TRUTH.items():
        for j in range(per):
            raw = RawListing(f"g{i}", f"https://shop.example/g{i}", f"{colour} wool {cat} {i}", price=300.0)
            p = upsert_listing(db, sellers[i % 4], raw).product
            p.image_embedding = emb.to_bytes(emb.normalise(rng.standard_normal(192).astype(np.float32)))
            p.embedding_backend = backend
            db.flush()
            if j < rated:
                db.add(Feedback(product_id=p.id, action="like" if rng.random() < pt else "dislike", reasons=[]))
            out.append(p)
            i += 1
    db.commit()
    if calibrate:
        assert calibration.recalibrate(db)["status"] == "calibrated"
    return out


def _set(client, **values):
    cur = client.get("/api/settings").json()["selective"]
    r = client.put("/api/settings/selective", json={**cur, **values})
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------- without a calibration
def test_without_calibration_balanced_is_the_labelled_ranking_and_strict_modes_are_empty(client, db):
    world(db, per=5, rated=2, calibrate=False)
    f = client.get("/api/feed", params={"limit": 100}).json()
    g = f["gate"]
    assert g["state"] == "uncalibrated" and g["mode"] == "balanced" and g["calibration"] is None
    assert g["message"].startswith("Not yet calibrated") and g["progress"]["labels"] == 12
    assert f["items"] and all("estimate" not in i["confidence"] for i in f["items"])  # no invented percentages
    assert not any(i["recommendation"]["explore"] for i in f["items"])  # exploration lives in Learning picks
    for mode in ("high", "extreme"):
        s = client.get("/api/feed", params={"mode": mode}).json()
        assert s["items"] == [] and s["gate"]["state"] == "uncalibrated"
        assert "nothing is shown as" in s["gate"]["message"] and "more rated pieces" in s["gate"]["message"]


def test_before_calibration_balanced_includes_a_few_labelled_learning_picks(client, db):
    """While Balanced is only a taste ranking, a small share of labelled Learning picks keeps a taste far from the
    written profile findable. Never in the strict modes, and never once calibrated."""
    from app.models import FeedRequest

    world(db, per=8, rated=2, calibrate=False)  # 36 undecided candidates
    f = client.get("/api/feed", params={"limit": 20}).json()
    picks = [i for i in f["items"] if i["recommendation"]["explore"]]
    assert 1 <= len(picks) <= 6 and all(i["confidence"]["learning"] for i in picks)
    assert all(i["recommendation"]["reasons"][0].startswith("Learning pick") for i in picks)
    rows = db.get(FeedRequest, f["feed_id"]).items
    assert all(0 < r["propensity"] <= 1 for r in rows if r["explore"]) and f["gate"]["sampling"]["pool"] > 0
    assert not any(
        i["recommendation"]["explore"] for i in client.get("/api/feed", params={"mode": "high"}).json()["items"]
    )


# ----------------------------------------------------------------- with a calibration
def test_calibrated_modes_show_only_pieces_whose_bound_clears_the_target(client, db):
    from app.models import FeedRequest

    world(db)
    shown = {}
    for mode, t in (("balanced", 0.80), ("high", 0.90), ("extreme", 0.95)):
        f = client.get("/api/feed", params={"mode": mode, "limit": 100}).json()
        g = f["gate"]
        assert g["state"] == "calibrated" and g["target"] == t and g["targets_validated"] is False
        assert len(f["items"]) == g["qualified"][mode] == g["shown"]  # every qualifier, and nothing else
        for it in f["items"]:
            c = it["confidence"]
            assert c["bound"] >= t and c["estimate"] >= c["bound"] - 1e-9 and mode in c["passes"]
        shown[mode] = [i["id"] for i in f["items"]]
        log = db.get(FeedRequest, f["feed_id"])
        assert (log.policy, log.mode, log.calibration_id) == ("selective", mode, g["calibration"]["id"])
        assert [r["product_id"] for r in log.items] == shown[mode]
        assert all(r["bound"] >= t and r["s"] > 0 and mode in r["passes"] for r in log.items)
    assert shown["balanced"] and shown["high"]
    assert set(shown["extreme"]) <= set(shown["high"]) < set(shown["balanced"])  # stricter modes are subsets
    feed = client.get("/api/feed", params={"limit": 10}).json()["items"]
    assert not any(i["recommendation"]["explore"] for i in feed)  # a calibrated feed is never mixed
    bounds = [i["confidence"]["bound"] for i in client.get("/api/feed", params={"limit": 5}).json()["items"]]
    assert len(bounds) == 5  # a smaller page keeps the best pieces by conservative bound


def test_an_empty_mode_explains_itself_and_is_never_padded(client, db):
    world(db)
    _set(client, extreme_target=0.99, extreme_confidence=0.99)
    f = client.get("/api/feed", params={"mode": "extreme", "limit": 50}).json()
    g = f["gate"]
    assert f["items"] == [] and g["qualified"]["extreme"] == 0 and g["qualified"]["balanced"] > 0
    assert g["message"].startswith("No sufficiently strong matches are currently available in Extremely selective")
    assert f"{g['qualified']['balanced']} under Balanced" in g["message"]


def test_a_stricter_target_never_admits_more(client, db):
    world(db)
    counts = []
    for t in (0.80, 0.85, 0.90, 0.95):
        _set(client, balanced_target=t, high_target=max(t, 0.9), extreme_target=max(t, 0.95))
        counts.append(len(client.get("/api/feed", params={"limit": 100}).json()["items"]))
    assert counts == sorted(counts, reverse=True) and counts[0] > counts[-1]


# ----------------------------------------------------------------- eligibility
def test_eligibility_exclusions_keep_their_reasons(client, db):
    from app.models import Evidence, Feedback, Product, Seller
    from app.services import selective
    from app.services.presenter import Context
    from app.services.recommender import build_profile, collapse_families, feed_candidates, rank_products

    ps = world(db, calibrate=False)
    navy = [p for p in ps if p.title_en.startswith("navy wool overcoat")][-6:]  # undecided
    poor, concern, no_photo, pricey, owned, dup = navy
    db.add(Evidence(product=poor, kind="verified", aspect="construction", claim="seams failed", polarity=-1))
    s2 = Seller(name="worrying", slug="worrying", platform="manual", reputation_status="concerns")
    db.add(s2)
    db.flush()
    concern.seller_id = s2.id
    no_photo.image_embedding = None
    pricey.price, pricey.category = 2000.0, "parka"
    rejected = Product(seller_id=poor.seller_id, title_original="x", title_en="navy parka", category="parka",
                       price=1500.0, source_url="https://shop.example/rp", external_id="rp", status="sold_out")  # fmt: skip
    db.add(rejected)
    db.flush()
    db.add(Feedback(product_id=rejected.id, action="dislike", reasons=["too_expensive"]))
    owned.category = "blazer"  # a navy blazer, like the navy blazers you said you own (scoped, not the category)
    for k in range(2):
        b = Product(seller_id=poor.seller_id, title_original="b", title_en=f"navy blazer {k}", category="blazer",
                    colours=["navy"], source_url=f"https://shop.example/b{k}", external_id=f"b{k}",
                    status="sold_out")  # fmt: skip
        db.add(b)
        db.flush()
        db.add(Feedback(product_id=b.id, action="dislike", reasons=["already_own"]))
    db.add(Feedback(product_id=ps[0].id, action="dislike", reasons=["specific_item"]))
    dup.image_embedding = ps[0].image_embedding  # a near-identical copy of a piece rejected as itself
    db.commit()

    prof = build_profile(db)
    ctx = Context(db)
    cands = feed_candidates(db, prof)
    ranked, _ = collapse_families(rank_products(db, cands, prof))
    gate = selective.evaluate(db, ranked, prof, "high", quality_lookup={p.id: ctx.quality(p) for p in cands})
    why = {c.product.id: c.excluded for c in gate.candidates}
    assert why[poor.id] == dict.fromkeys(selective.MODES, "poor_quality")
    assert why[dup.id] == dict.fromkeys(selective.MODES, "near_duplicate")
    for p, reason in ((concern, "seller_concerns"), (no_photo, "no_photo_analysis"), (pricey, "price_ceiling"),
                      (owned, "already_own")):  # fmt: skip
        assert why[p.id] == {"balanced": None, "high": reason, "extreme": reason}, (p.title_en, why[p.id])
    assert gate.exclusions()["seller_concerns"] == 1
    db.add(Evidence(product=pricey, kind="verified", aspect="material", claim="lab tested", polarity=1))
    db.commit()  # quality evidence that supports the price makes it eligible again
    gate = selective.evaluate(db, ranked, prof, "high", quality_lookup={pricey.id: Context(db).quality(pricey)})
    assert next(c for c in gate.candidates if c.product.id == pricey.id).excluded["high"] is None


# ----------------------------------------------------------------- Learning picks
def test_learning_picks_are_separate_labelled_and_carry_their_inclusion_probability(client, db):
    from app.models import FeedRequest

    world(db)
    main = {i["id"] for i in client.get("/api/feed", params={"limit": 24}).json()["items"]}
    f = client.get("/api/feed/learning", params={"limit": 8}).json()
    picks = f["items"]
    assert picks and not {i["id"] for i in picks} & main  # never the main feed's pieces
    assert all(i["recommendation"]["explore"] and i["recommendation"]["reasons"][0].startswith("Learning pick")
               for i in picks)  # fmt: skip
    assert all(i["confidence"]["learning"] and "balanced" not in i["confidence"]["passes"] for i in picks)
    log = db.get(FeedRequest, f["feed_id"])
    assert log.policy == "learning" and log.gate["sampling"]["pool"] >= len(picks)
    assert all(0 < r["propensity"] <= 1 and r["explore"] for r in log.items)
    assert abs(log.gate["sampling"]["expected"] - min(8, log.gate["sampling"]["pool"])) < 1e-6
    again = client.get("/api/feed/learning", params={"limit": 8}).json()["items"]
    assert [i["id"] for i in again] == [i["id"] for i in picks]  # a refresh does not reshuffle
    for it in picks:  # rated picks leave, and the next request draws afresh
        client.post("/api/feedback", json={"product_id": it["id"], "action": "dislike", "context": {"explore": True}})
    f2 = client.get("/api/feed/learning", params={"limit": 8}).json()
    assert f2["gate"]["sampling"]["salt"] != f["gate"]["sampling"]["salt"]
    assert not {i["id"] for i in f2["items"]} & {i["id"] for i in picks}
    extreme = client.get("/api/feed", params={"mode": "extreme", "limit": 100}).json()["items"]
    assert not any(i["recommendation"]["explore"] for i in extreme)  # never forced on the strictest mode


def test_learning_picks_try_what_the_model_knows_little_about_and_never_stall(env, db):
    """Plausibility is judged optimistically (mean + 1 s.d.): a piece the model is unsure about can be tried even if
    its plain estimate is low, while a piece it is sure you'd reject is not. Too few plausible pieces widen the
    pool instead of stalling learning."""
    from types import SimpleNamespace

    from app.services.selective import LEARNING_POOL_MIN, Candidate, Gate, learning_feed
    from app.services.settings_store import get_setting

    def cand(i, m, s):
        r = SimpleNamespace(product=SimpleNamespace(id=i, category="coat", seller_id=1), score=-i, suppressed=False)
        return Candidate(r, m, sd=s, excluded=dict.fromkeys(("balanced", "high", "extreme")))

    top = [cand(i, 3.0, 0.1) for i in range(24)]  # the ranking's top: the main feed, never a learning pick
    sure_no = [cand(100 + i, -3.0, 0.1) for i in range(10)]
    unsure = [cand(200 + i, -1.2, 1.5) for i in range(10)]  # σ(-1.2) = 0.23, but σ(-1.2 + 1.5) = 0.57
    gate = Gate("balanced", "uncalibrated", get_setting(db, "selective"), top + sure_no + unsure)
    picks, sampling = learning_feed(db, gate, 4, salt="t")
    assert sampling["pool"] == LEARNING_POOL_MIN * 4  # 10 plausible, widened by the 2 next-best
    assert picks and all(200 <= c.product.id < 210 for c in picks)


def test_poisson_sampling_includes_each_piece_with_its_logged_probability():
    from app.services.selective import inclusion_probabilities, permanent_random_number

    w = np.array([5.0, 3.0, 1.0, 1.0, 0.5, 0.2, 0.1, 0.1, 0.05, 0.05])
    pi = inclusion_probabilities(w, 4)
    assert abs(pi.sum() - 4) < 1e-9 and pi.max() <= 1 and pi[0] == 1.0  # capped, the rest proportional
    free = pi < 1
    assert np.allclose(pi[free] / w[free], (pi[free] / w[free])[0])
    days = 4000
    hits = np.array([[permanent_random_number(f"day{d}", pid) < p for pid, p in enumerate(pi)] for d in range(days)])
    se = np.sqrt(pi * (1 - pi) / days) + 1e-9
    assert np.all(np.abs(hits.mean(0) - pi) <= 4 * se)  # empirical inclusion frequency matches the logged π
    assert inclusion_probabilities(np.ones(3), 5).tolist() == [1.0, 1.0, 1.0]


def test_rating_learning_picks_brings_fresh_ones():
    """Regression: with draws fixed for the whole day, once the day's picks were rated almost none followed (the
    pieces left had all lost their draw). Fresh draws after each decision keep the expected size at k."""
    from app.services.selective import inclusion_probabilities, permanent_random_number

    w, k = np.linspace(1.0, 2.0, 40), 5
    fixed, fresh = [], []
    for day in range(300):
        pi = inclusion_probabilities(w, k)
        first = {i for i in range(len(w)) if permanent_random_number(f"d{day}:0", i) < pi[i]}
        rest = [i for i in range(len(w)) if i not in first]
        pi2 = dict(zip(rest, inclusion_probabilities(w[rest], k), strict=True))
        fixed.append(sum(permanent_random_number(f"d{day}:0", i) < pi2[i] for i in rest))
        fresh.append(sum(permanent_random_number(f"d{day}:{len(first)}", i) < pi2[i] for i in rest))
    assert np.mean(fresh) == pytest.approx(k, abs=0.4) and np.mean(fixed) < 1.5


def test_diversity_reorders_but_never_changes_membership():
    from types import SimpleNamespace

    from app.services.selective import Candidate, diversify

    def cand(i, cat, seller):
        return Candidate(SimpleNamespace(product=SimpleNamespace(id=i, category=cat, seller_id=seller)), 0.0)

    items = [cand(0, "coat", 1), cand(1, "coat", 2), cand(2, "coat", 3), cand(3, "coat", 4), cand(4, "shirt", 1),
             cand(5, "shirt", 2)]  # fmt: skip
    out = diversify(items)
    assert sorted(c.product.id for c in out) == list(range(6))
    cats = [c.product.category for c in out]
    assert not any(cats[i] == cats[i + 1] == cats[i + 2] for i in range(len(cats) - 2))
    assert [c.product.id for c in diversify(items[:3])] == [0, 1, 2]  # nothing to alternate with: order kept


# ----------------------------------------------------------------- failure and rollout
def test_a_failing_calibration_degrades_without_claiming_confidence(client, db, monkeypatch):
    from app.services import calibration

    world(db)

    def broken(_db, _ev=None):
        raise RuntimeError("calibration store unreadable")

    monkeypatch.setattr(calibration, "serving", broken)
    f = client.get("/api/feed", params={"limit": 100}).json()
    assert f["gate"]["state"] == "unavailable" and f["items"]
    assert all("estimate" not in i["confidence"] for i in f["items"])
    h = client.get("/api/feed", params={"mode": "high"}).json()
    assert h["items"] == [] and "unavailable" in h["gate"]["message"]
    assert client.get("/api/feed/learning").json()["items"] == []


def test_switching_the_gate_off_restores_the_earlier_feed(client, db):
    from app.models import FeedRequest

    world(db, per=8, rated=2, calibrate=False)
    _set(client, enabled=False)
    f = client.get("/api/feed", params={"limit": 20}).json()
    assert f["gate"]["state"] == "off" and f["gate"]["policy"] == "ranked"
    assert any(i["recommendation"]["explore"] for i in f["items"])  # the earlier feed's exploration slots
    assert db.get(FeedRequest, f["feed_id"]).policy == "ranked"
    assert client.get("/api/feed/learning").json()["items"] == []


def test_insights_top_picks_pass_through_the_gate(client, db):
    world(db)
    _set(client, mode="extreme")
    ins = client.get("/api/insights").json()
    assert ins["top_explained"] == [] and ins["top_gate"]["mode"] == "extreme"
    _set(client, mode="balanced")
    ins = client.get("/api/insights").json()
    assert ins["top_explained"] and all(t["confidence"]["bound"] >= 0.8 for t in ins["top_explained"])


# ----------------------------------------------------------------- observed performance, rollback
def test_observed_performance_counts_only_rated_served_pieces(client, db):
    world(db)
    f = client.get("/api/feed", params={"limit": 100}).json()
    items = f["items"]
    assert len(items) >= 13
    for k, it in enumerate(items[:12]):
        action = "like" if k < 9 else "dislike"
        ctx = {"feed_id": f["feed_id"], "position": k}
        assert client.post("/api/feedback", json={"product_id": it["id"], "action": action, "context": ctx}).is_success
    client.post(
        f"/api/feed/{f['feed_id']}/viewed", json={"surface": "card", "items": [{"product_id": items[12]["id"]}]}
    )
    obs = client.get("/api/feed", params={"limit": 5}).json()["gate"]["observed"]
    assert obs["rated"] == 12 and obs["shortfall"] is False  # 75% [43%, 95%]: not demonstrably below 80%
    perf = client.get("/api/selective/status").json()["performance"]
    b = perf["served_in_mode"]["balanced"]
    assert (b["rated"], b["approved"], b["rate"]) == (12, 9, 0.75)  # the shown-but-unrated piece is not counted
    lo, hi = b["interval"]
    assert lo < 0.75 < hi and hi - lo > 0.3  # twelve decisions say little: the interval is wide
    assert perf["served_in_mode"]["high"]["rate"] is None  # nothing served there yet
    for it in items[12:]:  # many more rejections: the shortfall is now demonstrable, and Discover says so
        ctx = {"feed_id": f["feed_id"], "position": 0}
        client.post("/api/feedback", json={"product_id": it["id"], "action": "dislike", "context": ctx})
    assert client.get("/api/feed", params={"limit": 5}).json()["gate"]["observed"]["shortfall"] is True


def test_calibration_history_rollback_and_settings_validation(client, db):
    from app.models import Calibration

    world(db)
    first = db.query(Calibration).one().id
    client.post("/api/selective/recalibrate")  # forced refit (runs synchronously in tests)
    st = client.get("/api/selective/status").json()
    assert len(st["history"]) == 2 and st["history"][0]["active"] and st["calibration"]["state"] == "calibrated"
    assert st["reliability"] and all({"n", "mean_estimate", "observed"} <= set(b) for b in st["reliability"])
    assert all(b["interval"][0] <= b["observed"] <= b["interval"][1] for b in st["reliability"])
    st = client.post(f"/api/selective/calibrations/{first}/activate").json()
    assert next(h for h in st["history"] if h["id"] == first)["active"]
    assert client.post("/api/selective/calibrations/9999/activate").status_code == 404
    cur = client.get("/api/settings").json()["selective"]
    assert client.put("/api/settings/selective", json={**cur, "high_target": 0.7}).status_code == 422  # < balanced
    assert client.put("/api/settings/selective", json={**cur, "mode": "reckless"}).status_code == 422


# ----------------------------------------------------------------- the opt-in prospective trial
def test_the_trial_alternates_days_and_compares_both_arms(client, db, monkeypatch):
    from datetime import date

    from app.models import FeedRequest
    from app.services import selective

    cfg = {"enabled": True, "trial": True}
    arms = {selective.trial_arm(cfg, date(2026, 10, d)) for d in (20, 21)}
    assert arms == {"ranked", "selective"} and selective.trial_arm({**cfg, "trial": False}, date.today()) is None
    world(db)
    _set(client, trial=True)
    for arm in ("ranked", "selective"):
        monkeypatch.setattr(selective, "trial_arm", lambda _cfg, _day, a=arm: a)
        f = client.get("/api/feed", params={"limit": 12}).json()
        assert f["gate"]["trial"] == arm and f["gate"]["policy"] == ("ranked" if arm == "ranked" else "selective")
        assert db.get(FeedRequest, f["feed_id"]).gate["trial"] == arm
        main = [i for i in f["items"] if not i["recommendation"]["explore"]][:10]
        for k, it in enumerate(main):
            ctx = {"feed_id": f["feed_id"], "position": k}
            client.post("/api/feedback", json={"product_id": it["id"], "action": "like", "context": ctx})
    trial = client.get("/api/selective/status").json()["performance"]["trial"]
    assert trial["ranked"]["rated"] == trial["selective"]["rated"] == 10
