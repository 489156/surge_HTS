"""yfinance adapters: OHLCV history (batch) + per-symbol structural/options snapshot.

These are the immediate-capture ("박제") fetchers. `.info` and `.option_chain`
return *current* values that cannot be reconstructed historically, so the
pipeline persists them every day.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import yfinance as yf
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def download_ohlcv(symbols: list[str], period: str = "60d",
                   start: str | None = None, end: str | None = None,
                   ) -> pd.DataFrame:
    """Batch daily OHLCV for many symbols. Returns a tidy long DataFrame:
    columns = [symbol, date, open, high, low, close, volume].

    `period` uses yfinance's enumerated windows; pass `start` (ISO date) instead
    when you need an exact range — e.g. realized-outcome backfill must fetch FROM
    a specific snapshot date, not a fixed look-back, or an old candidate's "next
    day" would resolve to the wrong bar.
    """
    if not symbols:
        return pd.DataFrame()
    kw = ({"start": start, "end": end} if start
          else {"period": period})
    raw = yf.download(
        tickers=" ".join(symbols),
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
        **kw,
    )
    if raw is None or raw.empty:
        return pd.DataFrame()

    frames = []
    if isinstance(raw.columns, pd.MultiIndex):
        # Recent yfinance returns a MultiIndex even for a single ticker. Find
        # which level holds the ticker symbols (group_by="ticker" → level 0).
        lvl0 = set(raw.columns.get_level_values(0))
        ticker_level = 0 if set(symbols) & lvl0 else 1
        present = set(raw.columns.get_level_values(ticker_level))
        for sym in symbols:
            if sym not in present:
                continue
            df = raw.xs(sym, axis=1, level=ticker_level).reset_index()
            df["symbol"] = sym
            frames.append(df)
    else:
        df = raw.reset_index()
        df["symbol"] = symbols[0]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out.columns = [str(c).lower() for c in out.columns]
    out = out.rename(columns={"date": "date"})
    keep = ["symbol", "date", "open", "high", "low", "close", "volume"]
    out = out[[c for c in keep if c in out.columns]].dropna(subset=["close"])
    return out


def fetch_structural(symbol: str) -> dict[str, Any]:
    """float / short interest / institutional ownership (current, delayed)."""
    info: dict[str, Any] = {}
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("info failed {}: {}", symbol, exc)
        return {}
    return {
        "market_cap": info.get("marketCap"),
        "shares_float": info.get("floatShares"),
        "shares_out": info.get("sharesOutstanding"),
        "inst_pct": info.get("heldPercentInstitutions"),
        "short_pct_float": info.get("shortPercentOfFloat"),
        "short_ratio": info.get("shortRatio"),
    }


def fetch_corporate(symbol: str, rsplit_window_days: int = 90) -> dict[str, Any]:
    """Detect a recent reverse split (a low-float squeeze setup AND a trap) and
    the next earnings date. Returns {recent_rsplit, catalysts: [(date,type,detail)]}."""
    out: dict[str, Any] = {"recent_rsplit": 0, "catalysts": []}
    try:
        tk = yf.Ticker(symbol)
        splits = tk.splits
        if splits is not None and len(splits):
            cutoff = pd.Timestamp(date.today() - timedelta(days=rsplit_window_days))
            recent = splits[splits.index.tz_localize(None) >= cutoff]
            for ts, ratio in recent.items():
                if ratio and ratio < 1:  # reverse split (e.g. 0.1 == 1-for-10)
                    out["recent_rsplit"] = 1
                    out["catalysts"].append(
                        (str(ts.date()), "reverse_split", f"1-for-{round(1/ratio)}")
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("splits failed {}: {}", symbol, exc)
    try:
        ed = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if ed is not None and len(ed):
            today = pd.Timestamp(date.today())
            future = ed[ed.index.tz_localize(None) >= today]
            if len(future):
                nxt = future.index.min()
                out["catalysts"].append((str(nxt.date()), "earnings", "next earnings"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings dates failed {}: {}", symbol, exc)
    return out


def fetch_options(symbol: str) -> dict[str, Any]:
    """Options summary: IV (ATM-ish), call/put volume. Cannot be reconstructed."""
    out: dict[str, Any] = {"opt_has_chain": 0}
    try:
        tk = yf.Ticker(symbol)
        expiries = tk.options
        if not expiries:
            return out
        out["opt_has_chain"] = 1
        chain = tk.option_chain(expiries[0])  # nearest expiry
        calls, puts = chain.calls, chain.puts
        cv = int(calls["volume"].fillna(0).sum()) if "volume" in calls else 0
        pv = int(puts["volume"].fillna(0).sum()) if "volume" in puts else 0
        out["call_volume"] = cv
        out["put_volume"] = pv
        out["call_put_ratio"] = (cv / pv) if pv else None
        if "impliedVolatility" in calls and not calls.empty:
            out["iv"] = float(calls["impliedVolatility"].median())
    except Exception as exc:  # noqa: BLE001
        logger.debug("options failed {}: {}", symbol, exc)
    return out
