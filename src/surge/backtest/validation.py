"""Robustness checks beyond a single backtest run: Monte Carlo on trade order,
walk-forward out-of-sample windows, and a synthetic crash stress test. A clean
single run proves little; these expose fragility and overfitting."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import BacktestEngine
from .metrics import summary
from .strategy import Strategy


def monte_carlo(trade_pnls: list[float], starting_capital: float = 100_000.0,
                n_sims: int = 2000, seed: int = 7) -> dict:
    """Bootstrap-resample the trade P&Ls to get a distribution of outcomes —
    'how much did trade ORDER and luck matter?'. Vectorized."""
    arr = np.asarray(trade_pnls, dtype=float)
    if arr.size == 0:
        return {"n_sims": 0}
    rng = np.random.default_rng(seed)
    sims = rng.choice(arr, size=(n_sims, arr.size), replace=True)
    equity = starting_capital + np.cumsum(sims, axis=1)
    equity = np.hstack([np.full((n_sims, 1), starting_capital), equity])
    finals = equity[:, -1]
    peak = np.maximum.accumulate(equity, axis=1)
    dd = (equity - peak) / peak
    mdd = dd.min(axis=1)
    return {
        "n_sims": n_sims,
        "mean_final": float(finals.mean()),
        "median_final": float(np.median(finals)),
        "p5_final": float(np.percentile(finals, 5)),
        "p95_final": float(np.percentile(finals, 95)),
        "prob_loss": float((finals < starting_capital).mean()),
        "median_max_drawdown": float(np.median(mdd)),
        "p5_max_drawdown": float(np.percentile(mdd, 5)),  # worst-5% drawdown
    }


def walk_forward(price_data: dict[str, pd.DataFrame], strategy: Strategy,
                 n_windows: int = 4, warmup: int = 60, **engine_kwargs) -> dict:
    """Evaluate the strategy on consecutive out-of-sample windows. Consistency
    across windows (not a single great run) is the real signal."""
    all_dates = sorted({
        d for df in price_data.values()
        for d in pd.to_datetime(df["date"]).dt.date.astype(str)
    })
    if len(all_dates) < n_windows * (warmup + 5):
        n_windows = max(1, len(all_dates) // (warmup + 5))
    if n_windows < 1:
        return {"windows": [], "note": "insufficient data"}

    chunks = np.array_split(all_dates, n_windows)
    results = []
    for k, chunk in enumerate(chunks):
        if len(chunk) == 0:
            continue
        start_idx = all_dates.index(chunk[0])
        warm_start = all_dates[max(0, start_idx - warmup)]
        end = chunk[-1]
        sub = {}
        for sym, df in price_data.items():
            d = df.copy()
            d["_d"] = pd.to_datetime(d["date"]).dt.date.astype(str)
            m = d[(d["_d"] >= warm_start) & (d["_d"] <= end)]
            if len(m) > warmup // 2:
                sub[sym] = m.drop(columns="_d")
        if not sub:
            continue
        res = BacktestEngine(strategy, **engine_kwargs).run(sub)
        results.append({"window": k + 1, "start": chunk[0], "end": end,
                        **{key: res.metrics.get(key) for key in
                           ("total_return", "sharpe", "max_drawdown", "n_trades")}})
    rets = [r["total_return"] or 0 for r in results]
    return {
        "windows": results,
        "n_windows": len(results),
        "mean_return": float(np.mean(rets)) if rets else 0.0,
        "pct_positive": float(np.mean([r > 0 for r in rets])) if rets else 0.0,
        "consistent": bool(rets) and all(r > -0.1 for r in rets),
    }


def crash_test(price_data: dict[str, pd.DataFrame], strategy: Strategy,
               shock: float = -0.30, at: float = 0.5, **engine_kwargs) -> dict:
    """Inject a one-day market-wide gap of `shock` partway through the data and
    measure damage — does risk control contain it?"""
    all_dates = sorted({
        d for df in price_data.values()
        for d in pd.to_datetime(df["date"]).dt.date.astype(str)
    })
    if not all_dates:
        return {}
    crash_date = all_dates[int(len(all_dates) * at)]
    shocked = {}
    for sym, df in price_data.items():
        d = df.copy()
        d["_d"] = pd.to_datetime(d["date"]).dt.date.astype(str)
        mask = d["_d"] == crash_date
        for col in ("open", "high", "low", "close"):
            d.loc[mask, col] = d.loc[mask, col] * (1 + shock)
        shocked[sym] = d.drop(columns="_d")
    res = BacktestEngine(strategy, **engine_kwargs).run(shocked)
    base = BacktestEngine(strategy, **engine_kwargs).run(price_data)
    return {
        "crash_date": crash_date, "shock": shock,
        "baseline_return": base.metrics.get("total_return"),
        "shocked_return": res.metrics.get("total_return"),
        "baseline_mdd": base.metrics.get("max_drawdown"),
        "shocked_mdd": res.metrics.get("max_drawdown"),
    }


def metrics_of(equity: list[float], trade_pnls: list[float]) -> dict:
    return summary(equity, trade_pnls)
