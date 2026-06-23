import pytest

from surge.config import settings
from surge.db import connect, ensure_securities, init_db
from surge.trading import store
from surge.trading.brokers import AlpacaLiveBroker, LiveBrokerGateError, PaperBroker
from surge.trading.execution import ExecutionEngine
from surge.trading.models import (
    Action,
    Decision,
    Order,
    OrderType,
    Side,
    TradingMode,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    with connect(path) as conn:
        ensure_securities(conn, ["ABC"])
    store.save_account(TradingMode.PAPER, cash=100_000, equity=100_000)
    return path


def test_paper_buy_fills_and_updates_position(db):
    eng = ExecutionEngine(TradingMode.PAPER, broker=PaperBroker())
    dec = Decision(mode=TradingMode.PAPER, symbol="ABC", action=Action.BUY,
                   final_score=70, confidence=60, stop_price=9.0)
    out = eng.execute_decision(dec, ref_price=10.0, positions=[], equity=100_000,
                               status=__import__("surge.trading.models",
                                                 fromlist=["RiskStatus"]).RiskStatus.OK)
    assert out["result"] == "filled"
    pos = store.get_position("ABC", TradingMode.PAPER)
    assert pos is not None and pos.qty == out["qty"]
    # cash decreased
    assert store.latest_cash(TradingMode.PAPER) < 100_000


def test_paper_slippage_is_adverse_on_buy(db):
    broker = PaperBroker()
    order = Order(mode=TradingMode.PAPER, symbol="ABC", side=Side.BUY, qty=100,
                  order_type=OrderType.MARKET)
    fill = broker.place_order(order, 10.0)
    assert fill.price > 10.0  # buyer pays up


def test_execution_rejects_bad_price(db):
    from surge.trading.models import RiskStatus
    eng = ExecutionEngine(TradingMode.PAPER, broker=PaperBroker())
    dec = Decision(mode=TradingMode.PAPER, symbol="ABC", action=Action.BUY,
                   final_score=70, confidence=60)
    out = eng.execute_decision(dec, ref_price=0, positions=[], equity=100_000,
                               status=RiskStatus.OK)
    assert out["result"] == "rejected"


def test_live_order_is_staged_not_filled(db, monkeypatch):
    from surge.trading.models import RiskStatus
    store.save_account(TradingMode.LIVE, cash=100_000, equity=100_000)
    eng = ExecutionEngine(TradingMode.LIVE, broker=PaperBroker())
    dec = Decision(mode=TradingMode.LIVE, symbol="ABC", action=Action.BUY,
                   final_score=70, confidence=60, stop_price=9.0)
    out = eng.execute_decision(dec, ref_price=10.0, positions=[], equity=100_000,
                               status=RiskStatus.OK)
    assert out["result"] == "pending_approval"
    # no position created, order queued for approval
    assert store.get_position("ABC", TradingMode.LIVE) is None
    assert len(store.list_pending_approvals()) == 1


def test_live_broker_refuses_auto_submit(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_api_key", "k")
    monkeypatch.setattr(settings, "alpaca_api_secret", "s")
    broker = AlpacaLiveBroker()
    order = Order(mode=TradingMode.LIVE, symbol="ABC", side=Side.BUY, qty=10)
    with pytest.raises(LiveBrokerGateError):
        broker.place_order(order, 10.0)
