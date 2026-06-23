import pytest

from surge.config import settings
from surge.db import connect, ensure_securities, init_db
from surge.trading import killswitch, store
from surge.trading.agents import DEFAULT_AGENTS
from surge.trading.debate import run_debate
from surge.trading.models import (
    AgentOpinion,
    MacroRegime,
    Position,
    Recommendation,
    RiskStatus,
    TradingMode,
)

MODE = TradingMode.PAPER


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def _op(agent, score, conf, rec):
    return AgentOpinion(agent=agent, ticker="ABC", score=score, confidence=conf,
                        recommendation=rec, reasoning="x")


def test_debate_risk_veto_forces_hold():
    opinions = [
        _op("technical_agent", 95, 90, Recommendation.BUY),
        _op("macro_agent", 80, 80, Recommendation.BUY),
        _op("risk_agent", 10, 95, Recommendation.SELL),  # confident veto
    ]
    res = run_debate(opinions)
    assert res["action"] == "HOLD"
    assert res["size_factor"] == 0.0
    assert "veto" in res["judge"]


def test_debate_half_size_band():
    opinions = [
        _op("technical_agent", 60, 70, Recommendation.HOLD),
        _op("macro_agent", 58, 60, Recommendation.HOLD),
        _op("risk_agent", 55, 50, Recommendation.HOLD),
    ]
    res = run_debate(opinions)
    assert res["action"] in ("BUY", "HOLD")
    if res["action"] == "BUY":
        assert res["size_factor"] in (0.5, 1.0)


def test_agents_produce_valid_contract():
    ctx = {"snapshot": {"shares_float": 5e6, "rvol": 6, "pct_change": 40,
                        "close": 5, "market_cap": 40e6},
           "trap": {}, "catalysts": [], "macro_regime": MacroRegime.RISK_ON,
           "portfolio_status": RiskStatus.OK}
    for agent in DEFAULT_AGENTS:
        op = agent.evaluate("ABC", ctx)
        assert 0 <= op.score <= 100
        assert 0 <= op.confidence <= 100
        assert op.recommendation in Recommendation
        assert op.reasoning


def test_killswitch_flattens_paper_positions(db):
    with connect(db) as conn:
        ensure_securities(conn, ["ABC", "XYZ"])
    store.save_account(MODE, cash=0, equity=20_000)
    store.upsert_position(Position(symbol="ABC", mode=MODE, qty=100, avg_price=50))
    store.upsert_position(Position(symbol="XYZ", mode=MODE, qty=200, avg_price=50))
    summary = killswitch.trigger(MODE, "test", {"ABC": 48.0, "XYZ": 49.0})
    assert set(summary["positions_flattened"]) == {"ABC", "XYZ"}
    assert summary["halted"] is True
    # positions closed
    assert store.get_positions(MODE) == []
    # halt state recorded
    assert killswitch.is_halted(MODE) is True


def test_killswitch_reset(db):
    store.save_account(MODE, cash=100, equity=100)
    killswitch.trigger(MODE, "test", {})
    assert killswitch.is_halted(MODE) is True
    killswitch.reset(MODE)
    assert killswitch.is_halted(MODE) is False
