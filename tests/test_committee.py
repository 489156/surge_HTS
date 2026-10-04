"""Unit tests for Multi-Persona AI Investment Committee (virattt/ai-hedge-fund pattern)."""

import pytest

from surge.trading.committee import (
    AckmanCatalystAgent,
    BuffettQualityAgent,
    BurryForensicRiskAgent,
    CathieMomentumAgent,
    GrahamSafetyAgent,
    COMMITTEE_AGENTS,
)
from surge.trading.debate import run_debate
from surge.trading.models import AgentOpinion, Recommendation


def test_buffett_quality_evaluates_dilution_and_cap():
    agent = BuffettQualityAgent()
    # Case 1: Dilution overhang triggers low score
    ctx_dilution = {"snapshot": {"market_cap": 100_000_000}, "trap": {"pending_offering": True}}
    op1 = agent.evaluate("TEST", ctx_dilution)
    assert op1.score <= 30.0
    assert "증자/희석" in op1.reasoning

    # Case 2: Mid-large cap gives stability
    ctx_large = {"snapshot": {"market_cap": 800_000_000}, "trap": {}}
    op2 = agent.evaluate("TEST", ctx_large)
    assert op2.score >= 60.0
    assert op2.recommendation == Recommendation.BUY


def test_cathie_momentum_explosive_rvol():
    agent = CathieMomentumAgent()
    ctx_hot = {
        "snapshot": {"rvol": 4.5, "pct_change": 35.0, "shares_float": 3_000_000},
        "trap": {},
    }
    op = agent.evaluate("MOMO", ctx_hot)
    assert op.score >= 80.0
    assert op.recommendation == Recommendation.BUY
    assert "폭발적 거래량" in op.reasoning


def test_burry_forensic_detects_pump_and_dump_and_dilution():
    agent = BurryForensicRiskAgent()
    # Extreme gap with no volume -> pump and dump flag
    ctx_fake = {
        "snapshot": {"pct_change": 120.0, "rvol": 1.2},
        "trap": {"pending_offering": True, "exhausted": True},
    }
    op = agent.evaluate("TRAP", ctx_fake)
    assert op.score <= 20.0
    assert op.confidence >= 80.0
    assert op.recommendation == Recommendation.SELL
    assert "S-1/S-3" in op.reasoning


def test_burry_short_agent_hard_veto_in_debate():
    # Even if 3 agents are raging bulls, Burry's forensic sell veto forces HOLD
    opinions = [
        AgentOpinion(agent="cathie_wood_agent", ticker="XYZ", score=95.0,
                     confidence=90.0, recommendation=Recommendation.BUY, reasoning="moon"),
        AgentOpinion(agent="technical_agent", ticker="XYZ", score=90.0,
                     confidence=85.0, recommendation=Recommendation.BUY, reasoning="breakout"),
        AgentOpinion(agent="burry_short_agent", ticker="XYZ", score=10.0,
                     confidence=90.0, recommendation=Recommendation.SELL, reasoning="dilution bomb"),
    ]
    res = run_debate(opinions)
    assert res["action"] == "HOLD"
    assert res["size_factor"] == 0.0
    assert "veto" in res["judge"]


def test_ackman_and_graham_evaluations():
    ackman = AckmanCatalystAgent()
    graham = GrahamSafetyAgent()

    ctx = {
        "catalysts": [{"event_type": "contract", "detail": "Major defense contract"}],
        "snapshot": {"market_cap": 250_000_000},
    }
    op_ack = ackman.evaluate("GOV", ctx)
    assert op_ack.score >= 70.0
    assert "전략적 모멘텀" in op_ack.reasoning

    op_graham = graham.evaluate("GOV", ctx)
    assert op_graham.score >= 55.0


def test_committee_agents_list_structure():
    assert len(COMMITTEE_AGENTS) == 5
    names = {a.name for a in COMMITTEE_AGENTS}
    assert "buffett_agent" in names
    assert "cathie_wood_agent" in names
    assert "burry_short_agent" in names
    assert "ackman_catalyst_agent" in names
    assert "graham_value_agent" in names
