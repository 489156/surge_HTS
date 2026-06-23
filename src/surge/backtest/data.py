"""Price-data loader for backtests. Reuses the surge market adapter; returns a
dict[symbol] -> tidy OHLCV DataFrame.

Survivorship note: yfinance only returns currently-listed tickers, so a universe
pulled live is survivorship-biased upward. For honest results, drive the symbol
list from a point-in-time source (the securities master, which never deletes
delisted rows) — passed in by the caller."""

from __future__ import annotations

import pandas as pd
from loguru import logger

from ..sources import market


def load_price_data(symbols: list[str], period: str = "2y",
                    batch: int = 50) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        df = market.download_ohlcv(chunk, period=period)
        if df.empty:
            continue
        for sym, g in df.groupby("symbol"):
            g = g.sort_values("date")
            if len(g) >= 30:
                out[sym] = g[["date", "open", "high", "low", "close", "volume"]].copy()
    logger.info("loaded price data for {}/{} symbols", len(out), len(symbols))
    return out


def candidate_symbols(top: int = 30) -> list[str]:
    """Default universe = the surge candidate watchlist (latest date)."""
    from ..db import connect

    with connect() as conn:
        latest = conn.execute("SELECT MAX(snapshot_date) d FROM candidates").fetchone()["d"]
        if not latest:
            return []
        rows = conn.execute(
            "SELECT symbol FROM candidates WHERE snapshot_date=? ORDER BY score DESC "
            "LIMIT ?", (latest, top),
        ).fetchall()
    return [r["symbol"] for r in rows]
