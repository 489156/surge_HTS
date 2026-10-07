import pytest
from surge import db
from surge.config import settings
from surge.duel.backtest import simulate_bracket
from surge.duel.decide import _gap_guard
from surge.trading.models import Position, RiskStatus, Side, TradingMode
from surge.trading.risk import RiskEngine


def test_sqlite_wal_pragmas(tmp_path, monkeypatch):
    """Verify SQLite initializes and connects in WAL mode with normal sync."""
    path = tmp_path / "wal_test.db"
    monkeypatch.setattr(settings, "db_path", path)
    db.init_db(path)

    with db.connect(path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        # WAL mode returns 'wal' (case-insensitive)
        assert mode.lower() == "wal"
        # synchronous NORMAL is integer 1
        assert sync in (1, "1", "NORMAL")


def test_simulate_bracket_break_even_ratchet():
    """Verify break-even ratchet stop protects profits after partial gain."""
    stop = 95.0
    target = 110.0
    ratchet_trigger = 105.0  # +5% triggers break-even
    ratchet_price = 100.0    # break-even at entry

    # Case 1: High reaches 106.0 (trigger reached), then drops to 99.0 (falls below entry 100.0)
    # Without ratchet, this would continue to 95.0 stop. With ratchet, it exits at 100.0.
    exit_px, reason = simulate_bracket(
        o=100.0, h=106.0, lo=98.0, c=99.0,
        stop=stop, target=target, slip_bps=0.0,
        ratchet_trigger=ratchet_trigger, ratchet_price=ratchet_price
    )
    assert reason == "ratchet_stop"
    assert exit_px == 100.0  # protected at break-even

    # Case 2: High only reaches 103.0 (trigger NOT reached), drops to 94.0 (normal stop)
    exit_px2, reason2 = simulate_bracket(
        o=100.0, h=103.0, lo=94.0, c=94.5,
        stop=stop, target=target, slip_bps=0.0,
        ratchet_trigger=ratchet_trigger, ratchet_price=ratchet_price
    )
    assert reason2 == "stop"
    assert exit_px2 == 95.0


def test_gap_guard_with_max_gap_atr(monkeypatch):
    """Verify ATR-based max gap ceiling is calculated correctly."""
    monkeypatch.setattr(settings, "duel_gap_guard_z", 0.0)
    monkeypatch.setattr(settings, "duel_max_gap_atr", 1.2)

    ctx = {
        "date": "2026-10-04",
        "und_atr14_pct": 0.03,  # 3% ATR
    }
    guard = _gap_guard(ctx)
    assert guard == pytest.approx(0.036, abs=1e-4)  # 1.2 * 0.03 = 0.036 (3.6%)


def test_risk_engine_cluster_concentration(tmp_path, monkeypatch):
    """Verify that existing position in tech beta cluster reduces size of new correlated position."""
    path = tmp_path / "risk_test.db"
    db.init_db(path)
    monkeypatch.setattr(settings, "db_path", path)

    engine = RiskEngine(TradingMode.PAPER)
    equity = 100_000.0

    # 1. First position in tech beta cluster (SOXL) with no existing positions
    size_soxl = engine.position_size(equity, entry=30.0, stop=28.0)
    decision1 = engine.assess(
        symbol="SOXL", side=Side.BUY, qty=size_soxl,
        entry=30.0, stop=28.0, positions=[],
        equity=equity, status=RiskStatus.OK
    )
    assert decision1.approved

    # 2. Existing position SOXL is held; now propose TQQQ (in same us_tech_beta cluster)
    existing_soxl = Position(
        symbol="SOXL", mode=TradingMode.PAPER, qty=decision1.adjusted_qty,
        avg_price=30.0, stop_price=28.0, target_price=35.0,
        opened_at="2026-10-04T00:00:00Z"
    )
    size_tqqq = engine.position_size(equity, entry=60.0, stop=56.0)
    decision2 = engine.assess(
        symbol="TQQQ", side=Side.BUY, qty=size_tqqq,
        entry=60.0, stop=56.0, positions=[existing_soxl],
        equity=equity, status=RiskStatus.OK
    )
    assert decision2.approved
    # Size must be shrunk by 50% due to cluster crowding
    assert decision2.adjusted_qty <= int(size_tqqq * 0.55)
