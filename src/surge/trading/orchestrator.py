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
from . import killswitch, llm, store
from .agents import Agent, DEFAULT_AGENTS, compute_macro_regime
from .committee import COMMITTEE_AGENTS
from .audit import audit
from .brokers import default_last_price
from .debate import run_debate
from .execution import ExecutionEngine
from .models import MacroRegime, RiskStatus, TradingMode
from .portfolio import PortfolioManager


class TradingEngine:
    def __init__(self, mode: TradingMode | None = None,
                 price_fn: Callable[[str], float | None] | None = None,
                 agents: list[Agent] | None = None):
        self.mode = mode or TradingMode(settings.trading_mode)
        self.price_fn = price_fn or default_last_price
        self.agents = agents or (DEFAULT_AGENTS + COMMITTEE_AGENTS)
        self.pm = PortfolioManager()
        self.execution = ExecutionEngine(self.mode)
        self.risk = self.execution.risk

    # ── context assembly ─────────────────────────────────────────────────────
    def _candidate_symbols(self, top: int) -> list[str]:
        from .. import verdict

        src = getattr(settings, "trading_signal_source", "auto")
        duel_ok = src in ("auto", "duel") and verdict._duel().get("mark") != "⛔"
        surge_ok = (
            src in ("auto", "pre_catalyst", "surge")
            and verdict._surge_pre().get("mark") != "⛔"
        )
        syms: list[str] = []
        with connect() as conn:
            if duel_ok:
                d_date = conn.execute(
                    "SELECT MAX(decision_date) d FROM duel_decisions"
                ).fetchone()["d"]
                if d_date:
                    d_rows = conn.execute(
                        "SELECT side FROM duel_decisions "
                        "WHERE decision_date=? AND side != 'STAND_ASIDE' "
                        "ORDER BY COALESCE(conviction, 0) DESC",
                        (d_date,),
                    ).fetchall()
                    for r in d_rows:
                        s = r["side"]
                        if s and s not in syms:
                            syms.append(s)

            if surge_ok and len(syms) < top:
                latest = conn.execute(
                    "SELECT MAX(snapshot_date) d FROM candidates "
                    "WHERE strategy_type='pre_catalyst'"
                ).fetchone()["d"]
                if latest:
                    c_rows = conn.execute(
                        "SELECT symbol FROM candidates "
                        "WHERE snapshot_date=? AND strategy_type='pre_catalyst' "
                        "ORDER BY score DESC LIMIT ?",
                        (latest, top),
                    ).fetchall()
                    for r in c_rows:
                        s = r["symbol"]
                        if s and s not in syms:
                            syms.append(s)
        return syms[:top]

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
                
            # Dynamic Trailing Stop: tighten from highest high once profitable (or initialize)
            if settings.enable_trailing_stops:
                potential_stop = last * (1 - settings.trailing_stop_pct)
                if p.stop_price is None or (last > p.avg_price and potential_stop > p.stop_price):
                    p.stop_price = potential_stop
                    store.upsert_position(p)
                    audit("execution", "trailing_stop_updated", symbol=p.symbol,
                          payload={"last": last, "new_stop": potential_stop})

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
        
        # Cost-saving hierarchical filter: only allow full LLM Committee eval on the top 3 candidates.
        # The universe is already sorted by Technical score DESC.
        MAX_LLM_EVALS = 3
        llm_evals_done = 0

        for symbol in universe:
            ref = last_prices.get(symbol)
            if not ref:
                continue
            ctx = self._ctx(symbol, macro, status)

            use_llm = bool(settings.anthropic_api_key and llm_evals_done < MAX_LLM_EVALS)
            if use_llm and "headlines" not in ctx and any(hasattr(a, "persona_prompt") for a in self.agents):
                ctx["headlines"] = llm.fetch_headlines(symbol)

            opinions = []
            used_llm = False
            for a in self.agents:
                if hasattr(a, "persona_prompt"):
                    if not use_llm:
                        # Cost/key budget exhausted: preserve deterministic rule evaluation & Burry veto
                        opinions.append(a.evaluate_rules(symbol, ctx))
                        continue
                    used_llm = True
                opinions.append(a.evaluate(symbol, ctx))

            if used_llm:
                llm_evals_done += 1
                
            debate = run_debate(opinions, ctx=ctx)
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
