"""Quality evidence independence, comparison grouping and price verdicts, and model cache/history fixes."""

from __future__ import annotations

from app.models import Evidence
from app.services.quality import score_dimension


def _ev(**kw):
    return Evidence(**{"aspect": "construction", "claim": "x", "strength": 1.0, **kw})


def test_a_reposted_review_counts_once():
    one = score_dimension([_ev(kind="buyer_report", polarity=1, source_url="https://r.example/1")])
    five = score_dimension(
        [_ev(kind="buyer_report", polarity=1, source_url="https://www.R.example/1/") for _ in range(5)]
    )
    assert five["confidence"] == one["confidence"] and five["label"] == one["label"]
    two = score_dimension([_ev(kind="buyer_report", polarity=1, source_url=f"https://r.example/{i}") for i in (1, 2)])
    assert two["confidence"] > one["confidence"]  # genuinely separate reports still add up


def test_marketing_cannot_be_unlocked_by_a_token_independent_report():
    claims = [_ev(kind="seller_claim", polarity=1, source_url=f"https://seller/{i}") for i in range(20)]
    r = score_dimension([_ev(kind="buyer_report", polarity=-1, strength=0.01), *claims])
    assert r["score"] is None and r["label"] == "Unverified"
    # Claims never move a score: not against a real negative report, nor from Good to Strong.
    for polarity in (-1, 1):
        report = _ev(kind="buyer_report", polarity=polarity)
        assert score_dimension([report, *claims])["score"] == score_dimension([report])["score"]
