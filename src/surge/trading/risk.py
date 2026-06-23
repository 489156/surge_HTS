"""Risk engine — the highest-priority component. It sizes positions, enforces
exposure/concentration/loss limits, and can veto or shrink any trade. Risk
preservation outranks every agent and the portfolio manager.

"Risk" here means *capital at risk* = position notional × stop distance, so the
default limits (per-trade 0.5%, portfolio 10% across ~20 names) are internally
consistent.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from ..config import settings
from .models import Position, RiskDecision, RiskStatus, Side, TradingMode
from . import store


class RiskEngine:
    def __init__(self, mode: TradingMode):
        self.mode = mode

    # ── Position sizing ──────────────────────────────────────────────────────
    def position_size(self, equity: float, entry: float, stop: float) -> int:
        """Shares such that (entry-stop) loss ≈ per_trade_risk × equity, capped
        by max_position_pct and rounded down to whole shares."""
        if entry <= 0:
            return 0
        per_share_risk = max(entry - stop, entry * 0.01)  # floor to avoid div→inf
        risk_budget = settings.per_trade_risk * equity
        qty_by_risk = risk_budget / per_share_risk
        qty_by_cap = (settings.max_position_pct * equity) / entry
        return max(0, int(math.floor(min(qty_by_risk, qty_by_cap))))

    # ── Portfolio risk helpers ───────────────────────────────────────────────
    @staticmethod
    def _position_risk(p: Position) -> float:
        stop = p.stop_price if p.stop_price else p.avg_price * (1 - settings.default_stop_pct)
        return abs(p.qty) * abs(p.avg_price - stop)

    def current_portfolio_risk(self, positions: list[Position]) -> float:
        return sum(self._position_risk(p) for p in positions)

    # ── Pre-trade assessment ─────────────────────────────────────────────────
    def assess(
        self,
        *,
        symbol: str,
        side: Side,
        qty: int,
        entry: float,
        stop: float,
        positions: list[Position],
        equity: float,
        status: RiskStatus,
    ) -> RiskDecision:
        """Approve / shrink / veto a proposed OPENING trade. Closing trades
        (risk-reducing) bypass most checks."""
        opening = side == Side.BUY  # long-only platform for now

        # 1) Loss-limit gates (system state) block new entries.
        if status in (RiskStatus.HALT, RiskStatus.LIQUIDATE):
            return RiskDecision(approved=False, status=status,
                                reason=f"system status {status.value} — no new entries")
        if status == RiskStatus.HALT_NEW and opening:
            return RiskDecision(approved=False, status=status,
                                reason="daily loss limit — new entries halted")

        if qty <= 0:
            return RiskDecision(approved=False, status=status,
                                reason="non-positive quantity")

        held = {p.symbol for p in positions}
        # 2) Concurrent-position cap (new symbols only).
        if opening and symbol not in held and len(held) >= settings.max_concurrent_positions:
            return RiskDecision(approved=False, status=status,
                                reason=f"max concurrent positions "
                                       f"({settings.max_concurrent_positions}) reached")

        adjusted = qty
        # 3) Per-position notional cap.
        notional = qty * entry
        cap = settings.max_position_pct * equity
        if notional > cap:
            adjusted = int(math.floor(cap / entry))
            if adjusted <= 0:
                return RiskDecision(approved=False, status=status,
                                    reason="position cap leaves zero shares")

        # 4) Portfolio at-risk cap.
        trade_risk = adjusted * abs(entry - stop)
        room = settings.max_portfolio_risk * equity - self.current_portfolio_risk(positions)
        if trade_risk > room:
            if room <= 0:
                return RiskDecision(approved=False, status=status,
                                    reason="portfolio risk budget exhausted")
            per_share_risk = max(abs(entry - stop), entry * 0.01)
            adjusted = int(math.floor(room / per_share_risk))
            if adjusted <= 0:
                return RiskDecision(approved=False, status=status,
                                    reason="portfolio risk leaves zero shares")

        # 5) Capital check.
        if adjusted * entry > store.latest_cash(self.mode) + 1e-6:
            affordable = int(math.floor(store.latest_cash(self.mode) / entry))
            adjusted = min(adjusted, affordable)
            if adjusted <= 0:
                return RiskDecision(approved=False, status=status,
                                    reason="insufficient cash")

        reason = "ok" if adjusted == qty else f"size reduced {qty}→{adjusted} by risk caps"
        return RiskDecision(approved=True, status=status, reason=reason,
                            adjusted_qty=adjusted)

    # ── System risk state (loss limits → status) ─────────────────────────────
    def risk_state(self, last_prices: dict[str, float]) -> tuple[RiskStatus, dict]:
        positions = store.get_positions(self.mode)
        cash = store.latest_cash(self.mode)
        mkt_value = sum(p.qty * last_prices.get(p.symbol, p.avg_price) for p in positions)
        equity = cash + mkt_value

        today = date.today()
        d_base = store.account_at_start_of(self.mode, today.isoformat())
        w_base = store.account_at_start_of(self.mode, (today - timedelta(days=7)).isoformat())
        m_base = store.account_at_start_of(self.mode, (today - timedelta(days=30)).isoformat())

        def pct(base):
            return ((equity - base) / base) if base else 0.0

        daily, weekly, monthly = pct(d_base), pct(w_base), pct(m_base)

        status = RiskStatus.OK
        if monthly <= -settings.monthly_loss_limit:
            status = RiskStatus.HALT
        elif weekly <= -settings.weekly_loss_limit:
            status = RiskStatus.LIQUIDATE
        elif daily <= -settings.daily_loss_limit:
            status = RiskStatus.HALT_NEW
        elif daily <= -settings.daily_loss_limit / 2:
            status = RiskStatus.WARN

        gross_exposure = (mkt_value / equity) if equity else 0.0
        metrics = {
            "equity": equity, "cash": cash, "daily": daily, "weekly": weekly,
            "monthly": monthly, "gross_exposure": gross_exposure,
            "n_positions": len(positions),
        }
        return status, metrics
