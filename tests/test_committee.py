"""Unit tests for Multi-Persona AI Investment Committee (virattt/ai-hedge-fund pattern)."""


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


def test_orchestrator_runs_rules_after_llm_cap_and_fetches_headlines_once(tmp_path, monkeypatch):
    from surge.config import settings
    from surge.db import connect, ensure_securities, init_db
    from surge.trading import llm, store
    from surge.trading.models import TradingMode
    from surge.trading.orchestrator import TradingEngine

    db = tmp_path / "orch.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "anthropic_api_key", "k")

    syms = ["C1", "C2", "C3", "TRAP4"]
    with connect(db) as conn:
        ensure_securities(conn, syms)
        for idx, s in enumerate(syms):
            conn.execute(
                "INSERT INTO daily_snapshot (symbol, snapshot_date, close, rvol, pct_change, market_cap, shares_float, captured_at) "
                "VALUES (?, '2026-10-10', 10.0, 5.0, 40.0, 50000000.0, 5000000.0, 'x')",
                (s,),
            )
            conn.execute(
                "INSERT INTO candidates (symbol, snapshot_date, score, close, captured_at) "
                "VALUES (?, '2026-10-10', ?, 10.0, 'x')",
                (s, 9.0 - idx),
            )
        conn.execute(
            "INSERT INTO trap_flags (symbol, snapshot_date, pending_offering, exhausted) "
            "VALUES ('TRAP4', '2026-10-10', 1, 1)"
        )

    store.save_account(TradingMode.PAPER, cash=100_000, equity=100_000)

    headline_calls = []
    monkeypatch.setattr(llm, "fetch_headlines", lambda s: (headline_calls.append(s) or ["h1"]))
    monkeypatch.setattr(llm, "analyze_news", lambda s, h: None)
    monkeypatch.setattr(
        llm, "analyze_persona",
        lambda s, p, ctx: {"score": 85.0, "confidence": 85.0, "reasoning": "bullish llm"},
    )

    eng = TradingEngine(TradingMode.PAPER, price_fn=lambda s: 10.0)
    res = eng.run_cycle(symbols=syms)
    # 1 headline fetch per LLM-evaluated candidate (top 3 only, never 5x per persona)
    assert headline_calls == ["C1", "C2", "C3"]
    c1_res = next(d for d in res["decisions"] if d["symbol"] == "C1")
    trap_res = next(d for d in res["decisions"] if d["symbol"] == "TRAP4")
    assert c1_res["action"] == "BUY"
    # Candidate #4 (TRAP4) exceeds MAX_LLM_EVALS=3, yet Burry rule veto STILL forces HOLD
    assert trap_res["action"] == "HOLD"
