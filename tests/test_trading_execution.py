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


def test_concurrent_close_position_prevents_double_sell(db):
    import threading
    from surge.trading.models import Position

    store.save_account(TradingMode.PAPER, cash=99_000.0, equity=100_000.0)
    store.upsert_position(Position(
        symbol="ABC", mode=TradingMode.PAPER, qty=100.0, avg_price=10.0,
    ))
    eng = ExecutionEngine(TradingMode.PAPER, broker=PaperBroker())
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(eng.close_position("ABC", ref_price=12.0, reason="race"))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r["result"] for r in results)
    assert statuses == ["closed", "no_position"]
    closed_fill = next(r for r in results if r["result"] == "closed")
    assert store.latest_cash(TradingMode.PAPER) == pytest.approx(
        99_000.0 + 100.0 * closed_fill["price"]
    )
    assert store.get_position("ABC", TradingMode.PAPER) is None


def test_candidate_symbols_excludes_reversion_and_legacy(db):
    from surge.db import upsert as db_upsert
    from surge.trading.orchestrator import TradingEngine

    with connect(db) as conn:
        ensure_securities(conn, {"LEG", "FADE", "PRE", "SOXL"})
        db_upsert(conn, "candidates", [
            {"symbol": "LEG", "snapshot_date": "2026-06-12", "strategy_type": "legacy_chase",
             "score": 9.0, "captured_at": "x"},
            {"symbol": "FADE", "snapshot_date": "2026-06-12", "strategy_type": "reversion_fade",
             "score": 8.0, "captured_at": "x"},
            {"symbol": "PRE", "snapshot_date": "2026-06-12", "strategy_type": "pre_catalyst",
             "score": 5.0, "captured_at": "x"},
        ])
        db_upsert(conn, "duel_decisions", [{
            "pair": "soxl_soxs", "decision_date": "2026-06-12", "side": "SOXL",
            "score": 0.4, "conviction": 0.4, "captured_at": "x",
        }])
    syms = TradingEngine()._candidate_symbols(10)
    assert "SOXL" in syms and "PRE" in syms
    assert syms.index("SOXL") < syms.index("PRE")
    assert "LEG" not in syms
    assert "FADE" not in syms


def test_reset_paper_account_clears_state_and_guards_live(db):
    from surge.trading import killswitch
    from surge.trading.models import Order, OrderStatus, OrderType, Position, Side

    with connect(db) as conn:
        ensure_securities(conn, {"AAA"})
    store.save_account(TradingMode.PAPER, cash=50_000.0, equity=75_000.0)
    killswitch.trigger(TradingMode.PAPER, "test", {})
    store.upsert_position(Position(symbol="AAA", mode=TradingMode.PAPER, qty=10, avg_price=5.0))
    store.insert_order(Order(
        order_id="ord_open_1", ts="2026-06-12T00:00:00Z", mode=TradingMode.PAPER,
        symbol="AAA", side=Side.BUY, qty=10, order_type=OrderType.MARKET,
        status=OrderStatus.SUBMITTED,
    ))
    store.insert_risk_state(
        TradingMode.PAPER, daily=-0.1, weekly=-0.1, monthly=-0.1,
        gross_exposure=0.5, n_positions=1, kill_switch=True, status="halt",
    )

    res = store.reset_paper_account(TradingMode.PAPER)
    assert res["reset"] is True and res["cancelled_orders"] == 1
    assert store.latest_equity(TradingMode.PAPER) == settings.starting_capital
    assert store.latest_cash(TradingMode.PAPER) == settings.starting_capital
    assert store.get_positions(TradingMode.PAPER) == []
    assert store.get_order("ord_open_1").status == OrderStatus.CANCELLED
    assert killswitch.is_halted(TradingMode.PAPER) is False

    with pytest.raises(ValueError):
        store.reset_paper_account(TradingMode.LIVE)
