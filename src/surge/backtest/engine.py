"""Event-driven backtest engine. Leak-free by construction: a strategy sees data
only up to a decision date, and the resulting entry fills at the NEXT bar's open.
Exits (stop / target / time) and realistic slippage + commission are modeled."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from .metrics import summary
from .strategy import Strategy


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    reason: str


@dataclass
class _Open:
    symbol: str
    qty: float
    entry_price: float
    entry_date: str
    stop: float
    target: float
    hold_until: int


@dataclass
class BacktestResult:
    dates: list[str]
    equity_curve: list[float]
    trades: list[Trade]
    metrics: dict = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        *,
        starting_capital: float = 100_000.0,
        position_pct: float = 0.05,
        max_positions: int = 20,
        stop_pct: float = 0.10,
        target_pct: float = 0.20,
        hold_days: int = 5,
        slippage_bps: float = 20.0,
        commission_per_share: float = 0.0,
    ):
        self.strategy = strategy
        self.capital = starting_capital
        self.position_pct = position_pct
        self.max_positions = max_positions
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.hold_days = hold_days
        self.slip = slippage_bps / 1e4
        self.commission = commission_per_share

    def run(self, price_data: dict[str, pd.DataFrame]) -> BacktestResult:
        # Per-symbol date→row lookup; union of all dates as the master clock.
        rows: dict[str, dict[str, dict]] = {}
        all_dates: set[str] = set()
        for sym, df in price_data.items():
            d = df.sort_values("date").copy()
            d["date"] = pd.to_datetime(d["date"]).dt.date.astype(str)
            rows[sym] = {r["date"]: r for r in d.to_dict("records")}
            all_dates.update(rows[sym].keys())
        dates = sorted(all_dates)
        if len(dates) < 3:
            return BacktestResult(dates, [self.capital], [], {})

        cash = self.capital
        positions: dict[str, _Open] = {}
        pending: list[tuple[str, float]] = []   # entries queued for next open
        trades: list[Trade] = []
        equity_curve: list[float] = []

        for i, date in enumerate(dates):
            # 1) fill pending entries at today's OPEN
            for sym, _score in pending:
                if sym in positions or sym not in rows or date not in rows[sym]:
                    continue
                if len(positions) >= self.max_positions:
                    break
                o = rows[sym][date]
                fill = o["open"] * (1 + self.slip)
                if fill <= 0:
                    continue
                qty = math.floor((self.position_pct * (cash + self._mtm(positions, rows, date))) / fill)
                if qty <= 0 or qty * fill > cash:
                    continue
                cash -= qty * fill + qty * self.commission
                positions[sym] = _Open(
                    symbol=sym, qty=qty, entry_price=fill, entry_date=date,
                    stop=fill * (1 - self.stop_pct), target=fill * (1 + self.target_pct),
                    hold_until=i + self.hold_days,
                )
            pending = []

            # 2) exits on today's range
            for sym in list(positions):
                if sym not in rows or date not in rows[sym]:
                    continue
                p = positions[sym]
                bar = rows[sym][date]
                exit_price = reason = None
                if bar["low"] <= p.stop:
                    exit_price = min(bar["open"], p.stop)   # gap-through fills at open
                    reason = "stop"
                elif bar["high"] >= p.target:
                    exit_price = max(bar["open"], p.target)
                    reason = "target"
                elif i >= p.hold_until:
                    exit_price = bar["close"]
                    reason = "time"
                if exit_price is None:
                    continue
                fill = exit_price * (1 - self.slip)
                cash += p.qty * fill - p.qty * self.commission
                pnl = (fill - p.entry_price) * p.qty
                trades.append(Trade(
                    symbol=sym, entry_date=p.entry_date, entry_price=round(p.entry_price, 4),
                    exit_date=date, exit_price=round(fill, 4), qty=p.qty,
                    pnl=round(pnl, 2), pnl_pct=round(fill / p.entry_price - 1, 4),
                    reason=reason,
                ))
                del positions[sym]

            # 3) mark equity at close
            equity_curve.append(cash + self._mtm(positions, rows, date))

            # 4) generate signals for NEXT day's open (no look-ahead)
            if len(positions) < self.max_positions and i < len(dates) - 1:
                history = self._history_asof(price_data, date)
                signals = self.strategy.generate(history)
                held = set(positions)
                pending = [(s, sc) for s, sc in signals if s not in held][
                    : self.max_positions - len(positions)
                ]

        trade_pnls = [t.pnl for t in trades]
        result = BacktestResult(dates, equity_curve, trades,
                                summary(equity_curve, trade_pnls))
        return result

    @staticmethod
    def _mtm(positions: dict[str, _Open], rows: dict, date: str) -> float:
        total = 0.0
        for sym, p in positions.items():
            bar = rows.get(sym, {}).get(date)
            last = bar["close"] if bar else p.entry_price
            total += p.qty * last
        return total

    @staticmethod
    def _history_asof(price_data: dict[str, pd.DataFrame], date: str) -> dict:
        out = {}
        for sym, df in price_data.items():
            d = df.copy()
            d["_d"] = pd.to_datetime(d["date"]).dt.date.astype(str)
            sub = d[d["_d"] <= date]
            if len(sub):
                out[sym] = sub.drop(columns="_d")
        return out
