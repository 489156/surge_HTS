"""Unit tests for Meta-Labeling Quality Filter & Sizing (Qlib / Lopez de Prado pattern)."""


from surge.trading.debate import run_debate
from surge.trading.metafilter import MetaLabelingFilter
from surge.trading.models import AgentOpinion, MacroRegime, Recommendation


def test_meta_filter_passes_strong_setup():
    mf = MetaLabelingFilter()
    ctx = {
        "macro_regime": MacroRegime.RISK_ON,
        "snapshot": {"rvol": 3.8, "pct_change": 18.0},
        "trap": {},
    }
    res = mf.evaluate("GOOD", base_score=72.0, ctx=ctx)
    assert res["passed"] is True
    assert res["win_prob"] >= 0.55
    assert res["ev"] > 0
    assert 0.2 <= res["kelly_size"] <= 1.0


def test_meta_filter_vetoes_risk_off_and_low_volume():
    mf = MetaLabelingFilter()
    ctx = {
        "macro_regime": MacroRegime.RISK_OFF,
        "snapshot": {"rvol": 0.8, "pct_change": 5.0},
        "trap": {"exhausted": True},
    }
    # Even with a nominal raw score of 58
    res = mf.evaluate("BAD", base_score=58.0, ctx=ctx)
    assert res["passed"] is False
    assert res["win_prob"] < 0.52
    assert res["kelly_size"] == 0.0
    assert "메타필터 거부" in res["reason"]


def test_meta_filter_blocks_overextended_gap():
    mf = MetaLabelingFilter()
    ctx = {
        "macro_regime": MacroRegime.NEUTRAL,
        "snapshot": {"rvol": 2.0, "pct_change": 85.0},  # Overextended +85%
        "trap": {},
    }
    res = mf.evaluate("OVER", base_score=60.0, ctx=ctx)
    assert res["passed"] is False
    assert "메타필터 거부" in res["reason"]


def test_meta_filter_integration_in_debate():
    opinions = [
        AgentOpinion(agent="technical_agent", ticker="XYZ", score=68.0,
                     confidence=80.0, recommendation=Recommendation.BUY, reasoning="bullish"),
        AgentOpinion(agent="news_agent", ticker="XYZ", score=65.0,
                     confidence=75.0, recommendation=Recommendation.BUY, reasoning="catalyst"),
    ]

    # Without ctx, debate allows BUY
    res_no_ctx = run_debate(opinions)
    assert res_no_ctx["action"] == "BUY"

    # With bad ctx, meta-filter rejects BUY -> forces HOLD
    bad_ctx = {
        "macro_regime": MacroRegime.RISK_OFF,
        "snapshot": {"rvol": 0.5, "pct_change": 2.0},
        "trap": {"pending_offering": True},
    }
    res_with_ctx = run_debate(opinions, ctx=bad_ctx)
    assert res_with_ctx["action"] == "HOLD"
    assert res_with_ctx["size_factor"] == 0.0
    assert "meta-filter veto" in res_with_ctx["judge"]
