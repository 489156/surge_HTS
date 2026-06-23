"""Decision cycle orchestrator — ties the whole platform together for one tick.

Flow per cycle:
  0. honor halt / kill-switch state
  1. compute macro regime (once)
  2. compute risk state from live prices → maybe fire kill switch
  3. manage exits on open positions (stop-loss / take-profit)
  4. for each candidate: agents → debate → portfolio manager → risk → execution
  5. snapshot the account + risk state; everything audited

Universe defaults to surge's own candidate watchlist (the surge signal feeds the
trader). Paper mode executes; live mode stages orders for human approval.
"""

from __future__ import annotations

from typing import Callable

from loguru import logger

from ..config import settings
from ..db import connect
from . import killswitch, store
from .agents import DEFAULT_AGENTS, compute_macro_regime
from .audit import audit
from .brokers import default_last_price
from .debate import run_debate
from .execution import ExecutionEngine
from .models import MacroRegime, RiskStatus, TradingMode
from .portfolio import PortfolioManager


class TradingEngine:
    def __init__(self, mode: TradingMode | None = None,
                 price_fn: Callable[[str], float | None] | None = None):
        self.mode = mode or TradingMode(settings.trading_mode)
        self.price_fn = price_fn or default_last_price
        self.agents = DEFAULT_AGENTS
        self.pm = PortfolioManager()
        self.execution = ExecutionEngine(self.mode)
        self.risk = self.execution.risk

    # ── context assembly ─────────────────────────────────────────────────────
    def _candidate_symbols(self, top: int) -> list[str]:
        with connect() as conn:
            latest = conn.execute(
                "SELECT MAX(snapshot_date) d FROM candidates"
            ).fetchone()["d"]
            if not latest:
                return []
            rows = conn.execute(
                "SELECT symbol FROM candidates WHERE snapshot_date=? "
                "ORDER BY score DESC LIMIT ?",
                (latest, top),
            ).fetchall()
        return [r["symbol"] for r in rows]

    def _ctx(self, symbol: str, macro: MacroRegime, status: RiskStatus) -> dict:
        with connect() as conn:
            snap = conn.execute(
                "SELECT * FROM daily_snapshot WHERE symbol=? "
                "ORDER BY snapshot_date DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            trap = conn.execute(
                "SELECT * FROM trap_flags WHERE symbol=? "
                "ORDER BY snapshot_date DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            cats = conn.execute(
                "SELECT event_type, event_date, detail FROM catalysts WHERE symbol=?",
                (symbol,),
            ).fetchall()
        return {
            "symbol": symbol,
            "snapshot": dict(snap) if snap else {},
            "trap": dict(trap) if trap else {},
            "catalysts": [dict(c) for c in cats],
            "macro_regime": macro,
            "portfolio_status": status,
        }

    # ── exits ────────────────────────────────────────────────────────────────
    def _manage_exits(self, last_prices: dict[str, float]) -> list[dict]:
        out = []
        for p in store.get_positions(self.mode):
            last = last_prices.get(p.symbol)
            if last is None:
                continue
            if p.stop_price and last <= p.stop_price:
                audit("execution", "stop_loss", symbol=p.symbol,
                      payload={"last": last, "stop": p.stop_price})
                out.append(self.execution.close_position(p.symbol, last, "stop_loss"))
            elif p.target_price and last >= p.target_price:
                audit("execution", "take_profit", symbol=p.symbol,
                      payload={"last": last, "target": p.target_price})
                out.append(self.execution.close_position(p.symbol, last, "take_profit"))
        return out

    # ── one cycle ────────────────────────────────────────────────────────────
    def run_cycle(self, symbols: list[str] | None = None, top: int = 10) -> dict:
        audit("engine", "cycle_start", payload={"mode": self.mode.value})

        if killswitch.is_halted(self.mode):
            logger.warning("system halted — cycle aborted")
            return {"halted": True}

        universe = symbols or self._candidate_symbols(top)
        macro = compute_macro_regime()

        # last prices for open positions + universe
        positions = store.get_positions(self.mode)
        watch = {p.symbol for p in positions} | set(universe)
        last_prices = {s: self.price_fn(s) for s in watch}
        last_prices = {k: v for k, v in last_prices.items() if v}

        # risk state + loss-limit gates
        status, metrics = self.risk.risk_state(last_prices)
        store.insert_risk_state(
            self.mode, daily=metrics["daily"], weekly=metrics["weekly"],
            monthly=metrics["monthly"], gross_exposure=metrics["gross_exposure"],
            n_positions=metrics["n_positions"],
            kill_switch=status in (RiskStatus.LIQUIDATE, RiskStatus.HALT),
            status=status.value,
        )
        if status in (RiskStatus.LIQUIDATE, RiskStatus.HALT):
            ks = killswitch.trigger(self.mode, f"loss limit → {status.value}",
                                    last_prices, self.execution)
            return {"status": status.value, "kill_switch": ks}

        exits = self._manage_exits(last_prices)

        equity = metrics["equity"]
        decisions = []
        for symbol in universe:
            ref = last_prices.get(symbol)
            if not ref:
                continue
            ctx = self._ctx(symbol, macro, status)
            opinions = [a.evaluate(symbol, ctx) for a in self.agents]
            debate = run_debate(opinions)
            decision = self.pm.decide(symbol, opinions, debate, ref, self.mode)
            store.insert_decision(decision)
            store.insert_opinions(decision.decision_id, opinions)
            positions = store.get_positions(self.mode)
            outcome = self.execution.execute_decision(
                decision, ref, positions, equity, status)
            decisions.append({"symbol": symbol, "action": decision.action.value,
                              "net": debate["net_score"], "outcome": outcome})

        # final account snapshot
        positions = store.get_positions(self.mode)
        mkt = sum(p.qty * last_prices.get(p.symbol, p.avg_price) for p in positions)
        store.save_account(self.mode, cash=store.latest_cash(self.mode),
                           equity=store.latest_cash(self.mode) + mkt)
        summary = {
            "mode": self.mode.value, "status": status.value, "macro": macro.value,
            "universe": len(universe), "decisions": decisions, "exits": exits,
            "equity": round(equity, 2),
        }
        audit("engine", "cycle_end", payload={"n_decisions": len(decisions),
                                              "status": status.value})
        return summary


def ensure_funded(mode: TradingMode) -> None:
    """Seed the account on first run."""
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM account_history WHERE mode=?",
                         (mode.value,)).fetchone()["n"]
    if n == 0:
        store.save_account(mode, cash=settings.starting_capital,
                           equity=settings.starting_capital)
        audit("operator", "account_funded",
              payload={"capital": settings.starting_capital, "mode": mode.value})
