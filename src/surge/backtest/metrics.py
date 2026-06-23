"""Risk-adjusted performance metrics. Pure functions over an equity curve and/or
a list of per-trade P&L. All guard against empty/degenerate inputs."""

from __future__ import annotations

import math

import numpy as np

TRADING_DAYS = 252


def returns_from_equity(equity: list[float]) -> np.ndarray:
    """Simple period returns from an equity curve."""
    e = np.asarray(equity, dtype=float)
    if len(e) < 2:
        return np.array([])
    prev = e[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(prev != 0, (e[1:] - prev) / prev, 0.0)
    return r


def sharpe(returns: np.ndarray, periods_per_year: int = TRADING_DAYS,
           rf: float = 0.0) -> float:
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    excess = r - rf / periods_per_year
    sd = excess.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(excess.mean() / sd * math.sqrt(periods_per_year))


def sortino(returns: np.ndarray, periods_per_year: int = TRADING_DAYS,
            rf: float = 0.0) -> float:
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    excess = r - rf / periods_per_year
    downside = excess[excess < 0]
    dd = math.sqrt((downside ** 2).mean()) if downside.size else 0.0
    if dd == 0:
        return 0.0
    return float(excess.mean() / dd * math.sqrt(periods_per_year))


def max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough decline as a negative fraction (e.g. -0.23)."""
    e = np.asarray(equity, dtype=float)
    if e.size == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak != 0, (e - peak) / peak, 0.0)
    return float(dd.min()) if dd.size else 0.0


def cagr(equity: list[float], periods_per_year: int = TRADING_DAYS) -> float:
    e = np.asarray(equity, dtype=float)
    if e.size < 2 or e[0] <= 0:
        return 0.0
    total = e[-1] / e[0]
    years = (e.size - 1) / periods_per_year
    if years <= 0 or total <= 0:
        return 0.0
    return float(total ** (1 / years) - 1)


def calmar(equity: list[float], periods_per_year: int = TRADING_DAYS) -> float:
    mdd = abs(max_drawdown(equity))
    if mdd == 0:
        return 0.0
    return float(cagr(equity, periods_per_year) / mdd)


def win_rate(trade_pnls: list[float]) -> float:
    t = [p for p in trade_pnls]
    if not t:
        return 0.0
    return sum(1 for p in t if p > 0) / len(t)


def profit_factor(trade_pnls: list[float]) -> float:
    wins = sum(p for p in trade_pnls if p > 0)
    losses = -sum(p for p in trade_pnls if p < 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def summary(equity: list[float], trade_pnls: list[float] | None = None,
            periods_per_year: int = TRADING_DAYS) -> dict:
    trade_pnls = trade_pnls or []
    r = returns_from_equity(equity)
    e = np.asarray(equity, dtype=float)
    total_return = float(e[-1] / e[0] - 1) if e.size >= 2 and e[0] else 0.0
    return {
        "n_periods": int(e.size),
        "n_trades": len(trade_pnls),
        "total_return": total_return,
        "cagr": cagr(equity, periods_per_year),
        "sharpe": sharpe(r, periods_per_year),
        "sortino": sortino(r, periods_per_year),
        "max_drawdown": max_drawdown(equity),
        "calmar": calmar(equity, periods_per_year),
        "win_rate": win_rate(trade_pnls),
        "profit_factor": profit_factor(trade_pnls),
        "final_equity": float(e[-1]) if e.size else 0.0,
    }
