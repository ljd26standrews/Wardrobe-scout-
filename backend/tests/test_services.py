"""AI guardrails (optional, cached, capped), community parsing, landed cost."""

from types import SimpleNamespace

import pytest

from app.services import ai
from app.services.community import looks_affiliate, parse_reddit_search
from app.services.landed_cost import estimate_landed_cost
from app.services.settings_store import DEFAULTS, set_setting


class FakeMessages:
    calls = 0

    def create(self, **kw):
        FakeMessages.calls += 1
        assert kw["model"] == "claude-opus-5-5"
        assert kw["fallbacks"] == "default"
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Wool-cashmere hoodie")],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


@pytest.fixture()
def fake_anthropic(env, db, monkeypatch):
    import anthropic

    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    )
    monkeypatch.setattr(ai, "_last_call", 0.0)
    FakeMessages.calls = 0
    from app.config import get_settings

    get_settings().anthropic_api_key = "test-key"
    cfg = dict(DEFAULTS["ai"], enabled=True, min_seconds_between_calls=0)
    set_setting(db, "ai", cfg)
    return cfg


def test_ai_disabled_by_default(env):
    assert ai.is_enabled("translate") is False
    assert ai.translate("羊绒卫衣") is None  # falls back silently to glossary


def test_ai_calls_are_cached_and_costed(fake_anthropic):
    assert ai.translate("羊毛羊绒连帽卫衣") == "Wool-cashmere hoodie"
    assert ai.translate("羊毛羊绒连帽卫衣") == "Wool-cashmere hoodie"
    assert FakeMessages.calls == 1  # second call served from cache
    assert ai.month_spend() == pytest.approx((1000 * 4 + 200 * 20) / 1e6)


def test_ai_spend_cap_blocks_calls(fake_anthropic, db):
    set_setting(db, "ai", dict(fake_anthropic, monthly_cap_usd=0.0))
    assert ai.translate("新的文本") is None
    assert FakeMessages.calls == 0


def test_reddit_parsing_and_affiliate_flag():
    data = {
        "data": {
            "children": [
                {
                    "data": {
                        "permalink": "/r/QualityReps/comments/1/x",
                        "title": "QC: cashmere hoodie",
                        "selftext": "Looks great",
                        "author": "a",
                        "created_utc": 1_700_000_000,
                        "subreddit": "QualityReps",
                    }
                },
                {
                    "data": {
                        "permalink": "/r/x/2",
                        "title": "Best seller!!",
                        "selftext": "use my link for a discount code",
                        "author": "b",
                        "created_utc": 1_700_000_000,
                        "subreddit": "x",
                    }
                },
            ]
        }
    }
    leads = parse_reddit_search(data)
    assert leads[0]["url"] == "https://www.reddit.com/r/QualityReps/comments/1/x"
    assert leads[0]["possible_affiliate"] is False and leads[1]["possible_affiliate"] is True
    assert looks_affiliate("Sponsored #ad")


def test_landed_cost_duty_threshold():
    cfg = DEFAULTS["landed_cost"]
    cheap = estimate_landed_cost(400, "CNY", "trousers", 0.105, cfg)
    assert cheap["duty_gbp"] == 0 and cheap["vat_gbp"] > 0
    dear = estimate_landed_cost(2000, "CNY", "overcoat", 0.105, cfg)
    assert dear["duty_gbp"] == pytest.approx(2000 * 0.105 * 0.12, rel=1e-3)
    assert estimate_landed_cost(None, "CNY", "t-shirt", 0.105, cfg) is None
    assert estimate_landed_cost(100, "USD", "t-shirt", 0.105, cfg) is None  # no guessed conversion
