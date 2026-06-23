"""Execution engine — validation pipeline + portfolio bookkeeping.

Every order passes risk validation BEFORE it can reach a broker. The engine
never trusts raw agent/LLM output: quantities are recomputed by the risk engine,
prices sanity-checked, and live orders are staged for human approval rather than
submitted. Paper orders fill synchronously through the PaperBroker.
"""

from __future__ import annotations

from loguru import logger

from ..config import settings
from . import store
from .audit import audit
from .brokers import Broker, make_broker
from .models import (
    Decision,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    RiskStatus,
    Side,
    TradingMode,
)
from .risk import RiskEngine


class ExecutionEngine:
    def __init__(self, mode: TradingMode, broker: Broker | None = None,
                 risk: RiskEngine | None = None):
        self.mode = mode
        self.broker = broker or make_broker()
        self.risk = risk or RiskEngine(mode)

    # ── Portfolio bookkeeping ────────────────────────────────────────────────
    def apply_fill(self, fill: Fill) -> None:
        """Update position + cash + account snapshot from a fill. Single source
        of truth for portfolio state."""
        pos = store.get_position(fill.symbol, self.mode)
        cash = store.latest_cash(self.mode)

        if fill.side == Side.BUY:
            cash -= fill.qty * fill.price + fill.commission
            if pos:
                new_qty = pos.qty + fill.qty
                pos.avg_price = (pos.qty * pos.avg_price + fill.qty * fill.price) / new_qty
                pos.qty = new_qty
            else:
                pos = Position(symbol=fill.symbol, mode=self.mode, qty=fill.qty,
                               avg_price=fill.price)
            store.upsert_position(pos)
        else:  # SELL — reduce/close
            if not pos:
                logger.warning("sell with no position {}", fill.symbol)
                return
            sold = min(fill.qty, pos.qty)
            pos.realized_pnl += (fill.price - pos.avg_price) * sold
            pos.qty -= sold
            cash += sold * fill.price - fill.commission
            if pos.qty <= 1e-9:
                pos.status = "closed"
            store.upsert_position(pos)

        store.insert_fill(fill)
        # recompute equity from open positions at their avg cost (conservative)
        positions = store.get_positions(self.mode)
        equity = cash + sum(p.qty * p.avg_price for p in positions)
        store.save_account(self.mode, cash=cash, equity=equity)
        audit("execution", "fill", symbol=fill.symbol,
              payload={"qty": fill.qty, "price": fill.price, "side": fill.side.value})

    # ── Validation + submission ──────────────────────────────────────────────
    def execute_decision(
        self,
        decision: Decision,
        ref_price: float,
        positions: list[Position],
        equity: float,
        status: RiskStatus,
    ) -> dict:
        """Validate and act on a portfolio-manager decision. Returns outcome."""
        sym = decision.symbol
        if ref_price is None or ref_price <= 0:
            return self._reject(decision, "no valid reference price")

        # SELL / exit
        if decision.action.value == "SELL":
            pos = store.get_position(sym, self.mode)
            if not pos or pos.qty <= 0:
                return {"symbol": sym, "result": "no_position"}
            return self.close_position(sym, ref_price, reason="decision SELL")

        if decision.action.value != "BUY":
            return {"symbol": sym, "result": "hold"}

        # BUY — size via risk engine (NOT from raw agent output)
        stop = decision.stop_price or ref_price * (1 - settings.default_stop_pct)
        qty = self.risk.position_size(equity, ref_price, stop)
        rd = self.risk.assess(
            symbol=sym, side=Side.BUY, qty=qty, entry=ref_price, stop=stop,
            positions=positions, equity=equity, status=status,
        )
        audit("risk", "assess", symbol=sym, decision_id=decision.decision_id,
              payload={"approved": rd.approved, "reason": rd.reason,
                       "qty": qty, "adjusted": rd.adjusted_qty})
        if not rd.approved:
            return self._reject(decision, f"risk veto: {rd.reason}")

        final_qty = rd.adjusted_qty or qty
        order = Order(
            mode=self.mode, symbol=sym, side=Side.BUY, qty=final_qty,
            order_type=OrderType.MARKET, stop_price=stop,
            decision_id=decision.decision_id, reason=decision.action.value,
        )

        # LIVE: never auto-submit — stage for human approval.
        if self.mode == TradingMode.LIVE:
            order.status = OrderStatus.PENDING_APPROVAL
            store.insert_order(order)
            store.create_approval(order.order_id)
            audit("execution", "staged_for_approval", symbol=sym,
                  decision_id=decision.decision_id,
                  payload={"order_id": order.order_id, "qty": final_qty})
            return {"symbol": sym, "result": "pending_approval",
                    "order_id": order.order_id, "qty": final_qty}

        # PAPER: fill synchronously.
        store.insert_order(order)
        fill = self.broker.place_order(order, ref_price)
        if not fill:
            store.update_order_status(order.order_id, OrderStatus.REJECTED.value)
            return {"symbol": sym, "result": "not_filled"}
        self.apply_fill(fill)
        # attach stop/target to the new position
        pos = store.get_position(sym, self.mode)
        if pos:
            pos.stop_price = stop
            pos.target_price = decision.target_price or ref_price * (1 + settings.default_target_pct)
            store.upsert_position(pos)
        store.update_order_status(order.order_id, OrderStatus.FILLED.value)
        return {"symbol": sym, "result": "filled", "qty": fill.qty,
                "price": fill.price}

    def close_position(self, symbol: str, ref_price: float, reason: str = "") -> dict:
        pos = store.get_position(symbol, self.mode)
        if not pos or pos.qty <= 0:
            return {"symbol": symbol, "result": "no_position"}
        order = Order(mode=self.mode, symbol=symbol, side=Side.SELL, qty=pos.qty,
                      order_type=OrderType.MARKET, reason=reason)
        if self.mode == TradingMode.LIVE:
            order.status = OrderStatus.PENDING_APPROVAL
            store.insert_order(order)
            store.create_approval(order.order_id)
            audit("execution", "staged_exit_for_approval", symbol=symbol,
                  payload={"order_id": order.order_id})
            return {"symbol": symbol, "result": "pending_approval"}
        store.insert_order(order)
        fill = self.broker.place_order(order, ref_price)
        if not fill:
            store.update_order_status(order.order_id, OrderStatus.REJECTED.value)
            return {"symbol": symbol, "result": "not_filled"}
        self.apply_fill(fill)
        store.update_order_status(order.order_id, OrderStatus.FILLED.value)
        return {"symbol": symbol, "result": "closed", "qty": fill.qty,
                "price": fill.price}

    def _reject(self, decision: Decision, reason: str) -> dict:
        audit("execution", "rejected", symbol=decision.symbol,
              decision_id=decision.decision_id, payload={"reason": reason})
        return {"symbol": decision.symbol, "result": "rejected", "reason": reason}
