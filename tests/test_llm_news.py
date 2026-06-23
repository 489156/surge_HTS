"""LLM news-agent tests. Live Anthropic calls are mocked; the parser and the
fallback/override logic are verified deterministically."""

import pytest

from surge.config import settings
from surge.trading import agents, llm
from surge.trading.models import MacroRegime, Recommendation, RiskStatus


# ── parser ───────────────────────────────────────────────────────────────────
def test_parse_valid_json():
    r = llm.parse_response('here you go: {"sentiment": 0.6, "impact": 0.8, '
                           '"summary": "beat earnings"} thanks')
    assert r["sentiment"] == 0.6
    assert r["impact"] == 0.8
    assert "beat" in r["summary"]


def test_parse_clamps_out_of_range():
    r = llm.parse_response('{"sentiment": 5, "impact": -2, "summary": "x"}')
    assert r["sentiment"] == 1.0
    assert r["impact"] == 0.0


def test_parse_rejects_garbage():
    assert llm.parse_response("no json here") is None
    assert llm.parse_response('{"impact": 0.5}') is None  # missing sentiment
    assert llm.parse_response("") is None


def test_analyze_returns_none_without_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert llm.analyze_news("AAPL", ["headline"]) is None


# ── agent integration ────────────────────────────────────────────────────────
def _ctx(**kw):
    base = {"snapshot": {}, "trap": {}, "catalysts": [],
            "macro_regime": MacroRegime.NEUTRAL, "portfolio_status": RiskStatus.OK}
    base.update(kw)
    return base


def test_newsagent_rule_based_without_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    op = agents.NewsAgent().evaluate("AAPL", _ctx())
    assert op.reasoning == "no material news signal"
    assert op.score == 50


def test_newsagent_blends_llm_sentiment(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(agents.llm, "analyze_news",
                        lambda s, h: {"sentiment": 0.8, "impact": 1.0,
                                      "summary": "strong beat"})
    op = agents.NewsAgent().evaluate("AAPL", _ctx(headlines=["x"]))
    assert op.score == pytest.approx(50 + 0.8 * 30)   # 74
    assert op.recommendation == Recommendation.BUY
    assert "LLM news" in op.reasoning


def test_offering_override_beats_positive_llm(monkeypatch):
    # A pending offering must stay bearish even if the LLM is bullish.
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(agents.llm, "analyze_news",
                        lambda s, h: {"sentiment": 1.0, "impact": 1.0, "summary": "moon"})
    op = agents.NewsAgent().evaluate("AAPL", _ctx(trap={"pending_offering": 1}))
    assert op.recommendation == Recommendation.SELL
    assert "dilutive" in op.reasoning


def test_newsagent_falls_back_when_llm_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(agents.llm, "analyze_news", lambda s, h: None)
    op = agents.NewsAgent().evaluate(
        "AAPL", _ctx(headlines=["x"],
                     catalysts=[{"event_type": "earnings"}]))
    assert "earnings" in op.reasoning  # rule-based path
