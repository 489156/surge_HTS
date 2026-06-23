import pytest

from surge.config import settings
from surge.db import init_db
from surge.trading import store
from surge.trading.models import Position, RiskStatus, Side, TradingMode
from surge.trading.risk import RiskEngine

MODE = TradingMode.PAPER


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    store.save_account(MODE, cash=100_000, equity=100_000)
    return path


def test_position_size_respects_per_trade_risk(db):
    eng = RiskEngine(MODE)
    # entry 10, stop 9 → per-share risk 1; risk budget = 0.5% * 100k = 500 → 500 shares
    # cap: 5% * 100k / 10 = 500 shares → min = 500
    qty = eng.position_size(100_000, 10.0, 9.0)
    assert qty == 500


def test_position_size_capped_by_max_position(db):
    eng = RiskEngine(MODE)
    # tiny stop distance would size huge by risk, but max_position caps it
    qty = eng.position_size(100_000, 10.0, 9.99)
    assert qty == int(0.05 * 100_000 / 10.0)  # 500 cap


def test_assess_vetoes_when_halted(db):
    eng = RiskEngine(MODE)
    rd = eng.assess(symbol="ABC", side=Side.BUY, qty=100, entry=10, stop=9,
                    positions=[], equity=100_000, status=RiskStatus.HALT_NEW)
    assert rd.approved is False
    assert "halt" in rd.reason.lower()


def test_assess_caps_position_size(db):
    eng = RiskEngine(MODE)
    # request 10000 sh @ $10 = $100k notional, way over 5% cap → shrink
    rd = eng.assess(symbol="ABC", side=Side.BUY, qty=10_000, entry=10, stop=9,
                    positions=[], equity=100_000, status=RiskStatus.OK)
    assert rd.approved is True
    assert rd.adjusted_qty <= int(0.05 * 100_000 / 10)


def test_assess_blocks_when_max_concurrent_reached(db, monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_positions", 2)
    eng = RiskEngine(MODE)
    positions = [
        Position(symbol="AAA", mode=MODE, qty=10, avg_price=5),
        Position(symbol="BBB", mode=MODE, qty=10, avg_price=5),
    ]
    rd = eng.assess(symbol="CCC", side=Side.BUY, qty=10, entry=10, stop=9,
                    positions=positions, equity=100_000, status=RiskStatus.OK)
    assert rd.approved is False
    assert "concurrent" in rd.reason


def test_risk_state_loss_limits(db):
    from surge.db import connect
    # Insert a backdated baseline (8 days ago) so a weekly drawdown is measurable.
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO account_history (ts, mode, cash, equity) VALUES "
            "('2026-05-30T00:00:00','paper',100000,100000)"
        )
    # current state: cash 6k + position worth 88k = 94k equity → -6% on the week
    store.upsert_position(Position(symbol="XYZ", mode=MODE, qty=1000, avg_price=100))
    store.save_account(MODE, cash=6_000, equity=94_000)
    eng = RiskEngine(MODE)
    status, m = eng.risk_state({"XYZ": 88.0})  # 1000*88 + 6k = 94k → -6%
    assert status == RiskStatus.LIQUIDATE
    assert round(m["weekly"], 3) == -0.06
