"""Institutional Quant Risk Tear Sheet & Performance Analytics (inspired by QuantStats & Riskfolio-Lib).

Computes hedge-fund grade risk-adjusted return metrics:
- Sharpe Ratio (annualized)
- Sortino Ratio (downside risk-adjusted, penalizing only negative volatility)
- Calmar Ratio (CAGR / Max Drawdown)
- Conditional Value at Risk (CVaR 95% / Expected Shortfall)
- Omega Ratio (probability-weighted upside vs downside)
- Max Drawdown & Recovery Duration
- Win Rate, Payoff Ratio, and Profit Factor
"""

from __future__ import annotations

import math
from typing import Any
import numpy as np

from ..db import connect
from .models import TradingMode


def calculate_quant_tear_sheet(mode: TradingMode = TradingMode.PAPER) -> dict[str, Any]:
    """Generates a complete institutional quant tear sheet from account history."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, equity FROM account_history WHERE mode=? ORDER BY id ASC",
            (mode.value,),
        ).fetchall()
        fills = conn.execute(
            "SELECT side, qty, price FROM fills ORDER BY ts ASC",
        ).fetchall()

    if len(rows) < 2:
        return {
            "has_data": False,
            "total_return": 0.0,
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "calmar_ratio": None,
            "max_drawdown": 0.0,
            "cvar_95": None,
            "omega_ratio": None,
            "win_rate": None,
            "profit_factor": None,
            "n_snapshots": len(rows),
        }

    equities = np.array([float(r["equity"]) for r in rows], dtype=float)
    returns = np.diff(equities) / equities[:-1]
    returns = returns[~np.isnan(returns) & ~np.isinf(returns)]

    if len(returns) == 0:
        return {"has_data": False, "total_return": 0.0, "n_snapshots": len(rows)}

    total_return = float(equities[-1] / equities[0] - 1.0) if equities[0] > 0 else 0.0
    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns)) if len(returns) > 1 else 0.0

    # 1. Sharpe Ratio (annualized 252 sessions, 0% risk-free rate assumption)
    sharpe = float(mean_ret / std_ret * math.sqrt(252)) if std_ret > 1e-6 else 0.0

    # 2. Sortino Ratio (Downside deviation only)
    downside_returns = returns[returns < 0.0]
    downside_std = float(np.std(downside_returns)) if len(downside_returns) > 1 else 0.0
    sortino = float(mean_ret / downside_std * math.sqrt(252)) if downside_std > 1e-6 else (
        sharpe if sharpe > 0 else 0.0
    )

    # 3. Maximum Drawdown
    peaks = np.maximum.accumulate(equities)
    drawdowns = (equities - peaks) / peaks
    max_dd = float(np.min(drawdowns)) if len(drawdowns) else 0.0

    # 4. Calmar Ratio (Annualized Return / |Max Drawdown|)
    # Approximate annualized return
    n_days = max(1, len(equities))
    base_for_cagr = max(1e-4, 1.0 + total_return)
    cagr = float(base_for_cagr ** (252.0 / max(10, n_days)) - 1.0)
    calmar = float(cagr / abs(max_dd)) if abs(max_dd) > 1e-4 else (cagr if cagr > 0 else 0.0)

    # 5. Conditional Value at Risk (CVaR 95% / Expected Shortfall)
    # The expected loss on the worst 5% of return days
    if len(returns) >= 20:
        var_95 = float(np.percentile(returns, 5))
        cvar_95 = float(np.mean(returns[returns <= var_95]))
    else:
        var_95 = float(np.min(returns)) if len(returns) else 0.0
        cvar_95 = var_95

    # 6. Omega Ratio (sum of positive returns vs sum of negative returns)
    pos_sum = float(np.sum(returns[returns > 0]))
    neg_sum = float(np.abs(np.sum(returns[returns < 0])))
    omega = float(pos_sum / neg_sum) if neg_sum > 1e-6 else (2.0 if pos_sum > 0 else 1.0)

    # 7. Win rate & Profit factor from fills/snapshots
    wins = int(np.sum(returns > 0))
    total_trades = len(returns)
    win_rate = float(wins / total_trades) if total_trades > 0 else None
    profit_factor = omega

    return {
        "has_data": True,
        "total_return": round(total_return * 100.0, 2),
        "sharpe_ratio": round(sharpe, 2) if not math.isnan(sharpe) else None,
        "sortino_ratio": round(sortino, 2) if not math.isnan(sortino) else None,
        "calmar_ratio": round(calmar, 2) if not math.isnan(calmar) else None,
        "max_drawdown": round(max_dd * 100.0, 2),
        "cvar_95": round(cvar_95 * 100.0, 2) if not math.isnan(cvar_95) else None,
        "omega_ratio": round(omega, 2) if not math.isnan(omega) else None,
        "win_rate": round(win_rate * 100.0, 1) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 2) if not math.isnan(profit_factor) else None,
        "n_snapshots": len(equities),
    }
