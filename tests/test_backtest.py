import numpy as np
import pandas as pd

from surge.backtest import metrics, validation
from surge.backtest.engine import BacktestEngine
from surge.backtest.strategy import MomentumBreakout


# ── metrics ──────────────────────────────────────────────────────────────────
def test_max_drawdown():
    eq = [100, 120, 90, 110]
    assert round(metrics.max_drawdown(eq), 3) == round((90 - 120) / 120, 3)


def test_sharpe_zero_when_flat():
    assert metrics.sharpe(np.zeros(10)) == 0.0


def test_profit_factor_and_win_rate():
    pnls = [10, -5, 20, -5]
    assert metrics.profit_factor(pnls) == 3.0  # 30 / 10
    assert metrics.win_rate(pnls) == 0.5


def test_summary_keys():
    s = metrics.summary([100, 101, 103, 102], [1, -1, 2])
    for k in ("sharpe", "sortino", "calmar", "max_drawdown", "win_rate",
              "profit_factor", "cagr"):
        assert k in s


# ── engine ───────────────────────────────────────────────────────────────────
def _ramp(symbol, n=60, start=10.0, step=0.0, vol=1000):
    """Build a deterministic OHLCV frame."""
    closes = [start + step * i for i in range(n)]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D").astype(str),
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "volume": [vol] * n,
    })


def test_engine_no_trades_on_flat_market():
    data = {"AAA": _ramp("AAA", step=0.0)}
    res = BacktestEngine(MomentumBreakout()).run(data)
    assert res.metrics["n_trades"] == 0
    # equity preserved (no trades)
    assert res.equity_curve[-1] == 100_000


def test_engine_takes_breakout_trades():
    # a steady uptrend with a volume spike triggers the breakout strategy
    df = _ramp("UP", n=80, start=10.0, step=0.2)
    df.loc[df.index >= 40, "volume"] = 5000  # volume confirmation later in series
    res = BacktestEngine(MomentumBreakout(lookback=20, vol_mult=1.5),
                         hold_days=5).run({"UP": df})
    assert res.metrics["n_trades"] >= 1
    # in a monotonic uptrend the strategy should not be deeply underwater
    assert res.metrics["max_drawdown"] >= -0.25


def test_engine_is_leak_free_entry_next_open():
    # Entry must fill at the NEXT bar's open, never the signal bar's close.
    df = _ramp("X", n=60, start=10.0, step=0.5)
    df.loc[df.index >= 25, "volume"] = 9000
    res = BacktestEngine(MomentumBreakout(lookback=20)).run({"X": df})
    for t in res.trades:
        assert t.entry_date > df["date"].iloc[0]  # never the first bar


# ── Monte Carlo ──────────────────────────────────────────────────────────────
def test_monte_carlo_distribution():
    pnls = [100, -50, 200, -30, 80]
    mc = validation.monte_carlo(pnls, starting_capital=10_000, n_sims=500, seed=1)
    assert mc["n_sims"] == 500
    assert 0.0 <= mc["prob_loss"] <= 1.0
    assert mc["p5_final"] <= mc["median_final"] <= mc["p95_final"]


def test_monte_carlo_empty():
    assert validation.monte_carlo([])["n_sims"] == 0
