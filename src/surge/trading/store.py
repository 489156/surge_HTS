"""Trading persistence layer — thin, explicit SQL over the shared SQLite DB."""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import settings
from ..db import connect
from .models import (
    Decision,
    Fill,
    Order,
    Position,
    TradingMode,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Account ──────────────────────────────────────────────────────────────────
def latest_equity(mode: TradingMode) -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT equity FROM account_history WHERE mode=? ORDER BY id DESC LIMIT 1",
            (mode.value,),
        ).fetchone()
    return float(row["equity"]) if row else settings.starting_capital


def latest_cash(mode: TradingMode) -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT cash FROM account_history WHERE mode=? ORDER BY id DESC LIMIT 1",
            (mode.value,),
        ).fetchone()
    return float(row["cash"]) if row else settings.starting_capital


def save_account(mode: TradingMode, cash: float, equity: float,
                 realized: float = 0.0, unrealized: float = 0.0) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO account_history (ts, mode, cash, equity, realized_pnl, "
            "unrealized_pnl) VALUES (?,?,?,?,?,?)",
            (_now(), mode.value, cash, equity, realized, unrealized),
        )


def account_at_start_of(mode: TradingMode, iso_date_prefix: str) -> float | None:
    """Equity at/just-before the first snapshot on/after a date prefix
    (for daily/weekly/monthly P&L baselines)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT equity FROM account_history WHERE mode=? AND ts < ? "
            "ORDER BY id DESC LIMIT 1",
            (mode.value, iso_date_prefix),
        ).fetchone()
    return float(row["equity"]) if row else None


# ── Positions ────────────────────────────────────────────────────────────────
def get_positions(mode: TradingMode, *, open_only: bool = True) -> list[Position]:
    q = "SELECT * FROM positions WHERE mode=?"
    if open_only:
        q += " AND status='open'"
    with connect() as conn:
        rows = conn.execute(q, (mode.value,)).fetchall()
    return [
        Position(
            symbol=r["symbol"], mode=TradingMode(r["mode"]), qty=r["qty"],
            avg_price=r["avg_price"], stop_price=r["stop_price"],
            target_price=r["target_price"], realized_pnl=r["realized_pnl"],
            status=r["status"],
        )
        for r in rows
    ]


def get_position(symbol: str, mode: TradingMode) -> Position | None:
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND mode=? AND status='open'",
            (symbol, mode.value),
        ).fetchone()
    if not r:
        return None
    return Position(
        symbol=r["symbol"], mode=TradingMode(r["mode"]), qty=r["qty"],
        avg_price=r["avg_price"], stop_price=r["stop_price"],
        target_price=r["target_price"], realized_pnl=r["realized_pnl"],
        status=r["status"],
    )


def upsert_position(p: Position) -> None:
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO positions (symbol, mode, qty, avg_price, stop_price, "
            "target_price, opened_at, updated_at, realized_pnl, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, mode) DO UPDATE SET qty=excluded.qty, "
            "avg_price=excluded.avg_price, stop_price=excluded.stop_price, "
            "target_price=excluded.target_price, updated_at=excluded.updated_at, "
            "realized_pnl=excluded.realized_pnl, status=excluded.status",
            (p.symbol, p.mode.value, p.qty, p.avg_price, p.stop_price,
             p.target_price, now, now, p.realized_pnl, p.status),
        )


# ── Orders / fills ───────────────────────────────────────────────────────────
def insert_order(o: Order) -> None:
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO orders (order_id, ts, mode, symbol, side, qty, order_type, "
            "limit_price, stop_price, tif, status, decision_id, reason, "
            "broker_order_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.order_id, o.ts, o.mode.value, o.symbol, o.side.value, o.qty,
             o.order_type.value, o.limit_price, o.stop_price, o.tif,
             o.status.value, o.decision_id, o.reason, o.broker_order_id, now, now),
        )


def update_order_status(order_id: str, status: str,
                        broker_order_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status=?, broker_order_id=COALESCE(?, broker_order_id), "
            "updated_at=? WHERE order_id=?",
            (status, broker_order_id, _now(), order_id),
        )


def insert_fill(f: Fill) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO fills (fill_id, order_id, ts, symbol, side, qty, price, "
            "commission, slippage) VALUES (?,?,?,?,?,?,?,?,?)",
            (f.fill_id, f.order_id, f.ts, f.symbol, f.side.value, f.qty, f.price,
             f.commission, f.slippage),
        )


# ── Live-order approval queue ────────────────────────────────────────────────
def create_approval(order_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO approvals (order_id, ts, status) VALUES (?, ?, 'pending') "
            "ON CONFLICT(order_id) DO UPDATE SET ts=excluded.ts, status=excluded.status",
            (order_id, _now()),
        )


def list_pending_approvals() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.order_id, a.ts, o.symbol, o.side, o.qty, o.order_type, "
            "o.limit_price FROM approvals a JOIN orders o USING(order_id) "
            "WHERE a.status='pending' ORDER BY a.ts"
        ).fetchall()
    return [dict(r) for r in rows]


def set_approval(order_id: str, status: str, by: str = "operator",
                 note: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE approvals SET status=?, approved_by=?, approved_at=?, note=? "
            "WHERE order_id=?",
            (status, by, _now(), note, order_id),
        )


# ── Decisions / opinions / risk ──────────────────────────────────────────────
def insert_decision(d: Decision) -> None:
    import json
    with connect() as conn:
        conn.execute(
            "INSERT INTO decisions (decision_id, ts, mode, symbol, action, "
            "final_score, confidence, size_pct, stop_price, target_price, "
            "expected_risk, rationale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.decision_id, d.ts, d.mode.value, d.symbol, d.action.value,
             d.final_score, d.confidence, d.size_pct, d.stop_price, d.target_price,
             d.expected_risk, json.dumps(d.rationale, ensure_ascii=False, default=str)),
        )


def insert_opinions(decision_id: str, opinions: list) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO agent_opinions (decision_id, ts, agent, symbol, score, "
            "confidence, recommendation, reasoning) VALUES (?,?,?,?,?,?,?,?)",
            [
                (decision_id, op.ts, op.agent, op.ticker, op.score, op.confidence,
                 op.recommendation.value, op.reasoning)
                for op in opinions
            ],
        )


def insert_risk_state(mode: TradingMode, *, daily, weekly, monthly, gross_exposure,
                      n_positions, kill_switch, status) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO risk_state (ts, mode, daily_pnl_pct, weekly_pnl_pct, "
            "monthly_pnl_pct, gross_exposure_pct, n_positions, kill_switch, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_now(), mode.value, daily, weekly, monthly, gross_exposure,
             n_positions, 1 if kill_switch else 0, status),
        )
