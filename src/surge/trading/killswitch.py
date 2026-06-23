"""Kill switch — the ultimate safety override. Triggers on abnormal conditions
(loss-limit breach, API anomaly, order flood, sharp drop) and overrides every
agent: cancel working orders, flatten positions, disable trading, alert, log.

In PAPER mode it liquidates automatically. In LIVE mode it cancels working
orders and STAGES the flatten for human confirmation (never auto-dumps real
money) — the protective intent is preserved without unattended money movement.
"""

from __future__ import annotations

from loguru import logger

from . import store
from .audit import audit
from .execution import ExecutionEngine
from .models import OrderStatus, RiskStatus, TradingMode


def is_halted(mode: TradingMode) -> bool:
    """True if the most recent risk_state recorded an active kill switch/halt."""
    from ..db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT kill_switch, status FROM risk_state WHERE mode=? "
            "ORDER BY id DESC LIMIT 1",
            (mode.value,),
        ).fetchone()
    if not row:
        return False
    return bool(row["kill_switch"]) or row["status"] == RiskStatus.HALT.value


def trigger(
    mode: TradingMode,
    reason: str,
    last_prices: dict[str, float],
    engine: ExecutionEngine | None = None,
) -> dict:
    """Fire the kill switch. Returns a summary of actions taken."""
    engine = engine or ExecutionEngine(mode)
    logger.warning("KILL SWITCH triggered ({}): {}", mode.value, reason)
    audit("killswitch", "triggered", payload={"reason": reason, "mode": mode.value})

    # 1) Cancel working orders.
    from ..db import connect

    cancelled = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT order_id FROM orders WHERE mode=? AND status IN "
            "('new','submitted','pending_approval','partially_filled')",
            (mode.value,),
        ).fetchall()
    for r in rows:
        store.update_order_status(r["order_id"], OrderStatus.CANCELLED.value)
        cancelled += 1

    # 2) Flatten positions.
    positions = store.get_positions(mode)
    flattened, staged = [], []
    for p in positions:
        ref = last_prices.get(p.symbol, p.avg_price)
        out = engine.close_position(p.symbol, ref, reason=f"killswitch: {reason}")
        if out.get("result") == "closed":
            flattened.append(p.symbol)
        elif out.get("result") == "pending_approval":
            staged.append(p.symbol)

    # 3) Disable trading (record HALT state).
    store.insert_risk_state(
        mode, daily=None, weekly=None, monthly=None, gross_exposure=None,
        n_positions=len(positions), kill_switch=True, status=RiskStatus.HALT.value,
    )

    # 4) Alert operator (audit is the alert channel here).
    summary = {
        "reason": reason, "orders_cancelled": cancelled,
        "positions_flattened": flattened, "positions_staged": staged,
        "halted": True,
    }
    audit("killswitch", "completed", payload=summary)
    logger.warning("KILL SWITCH complete: {}", summary)
    return summary


def reset(mode: TradingMode, operator: str = "operator") -> None:
    """Clear the halt (operator action only). Logged for audit."""
    store.insert_risk_state(
        mode, daily=None, weekly=None, monthly=None, gross_exposure=None,
        n_positions=len(store.get_positions(mode)), kill_switch=False,
        status=RiskStatus.OK.value,
    )
    audit("operator", "killswitch_reset", payload={"by": operator})
