"""Group-B feature engineering from OHLCV. Pure functions over a per-symbol
price history sorted ascending by date. All values are point-in-time as of the
last row (no look-ahead)."""

from __future__ import annotations

import pandas as pd


def compute_features(hist: pd.DataFrame) -> dict | None:
    """`hist`: columns [date, open, high, low, close, volume] for ONE symbol,
    sorted ascending. Returns a feature dict for the LAST (most recent) row,
    or None if insufficient data."""
    if hist is None or len(hist) < 2:
        return None
    h = hist.sort_values("date").reset_index(drop=True)
    last = h.iloc[-1]
    prev = h.iloc[-2]

    close = float(last["close"])
    prev_close = float(prev["close"])
    high = float(last["high"])
    low = float(last["low"])
    open_ = float(last["open"])
    volume = float(last["volume"] or 0)

    pct_change = (close / prev_close - 1.0) * 100 if prev_close else None
    gap_pct = (open_ / prev_close - 1.0) * 100 if prev_close else None
    dollar_volume = close * volume

    avg_vol_20 = h["volume"].tail(21).head(20).mean()  # prior 20d, excl. today
    rvol = (volume / avg_vol_20) if avg_vol_20 and avg_vol_20 > 0 else None

    rng = high - low
    close_strength = ((close - low) / rng) if rng > 0 else None
    range_pct = (rng / prev_close * 100) if prev_close else None

    low_52 = float(h["low"].tail(252).min())
    dist_52w_low = ((close - low_52) / low_52 * 100) if low_52 > 0 else None

    # consecutive up days (close > prior close)
    consec = 0
    closes = h["close"].tolist()
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            consec += 1
        else:
            break

    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "prev_close": prev_close,
        "volume": int(volume),
        "dollar_volume": dollar_volume,
        "pct_change": pct_change,
        "gap_pct": gap_pct,
        "rvol": rvol,
        "close_strength": close_strength,
        "range_pct": range_pct,
        "dist_52w_low": dist_52w_low,
        "consec_up_days": consec,
    }


def recent_run_pct(hist: pd.DataFrame, lookback: int) -> float | None:
    """Cumulative % move over the last `lookback` days — for exhaustion trap."""
    if hist is None or len(hist) < 2:
        return None
    h = hist.sort_values("date")
    window = h.tail(lookback + 1)
    base = float(window.iloc[0]["close"])
    last = float(window.iloc[-1]["close"])
    return (last / base - 1.0) * 100 if base else None
