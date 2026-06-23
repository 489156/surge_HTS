"""Multi-provider quote layer with failover — built for SOXL/SOXS tracking.

Order: yfinance (free, no key) → Finnhub (true vendor redundancy, free 60
calls/min, needs SURGE_FINNHUB_API_KEY) → Yahoo chart API direct (client-path
redundancy: survives yfinance-library breakage). A nightly direction engine
must not have a single point of failure on its two tickers; every consumer
(trading, duel, dashboard) goes through this chain via
`brokers.default_last_price`, which adds the 60s cache.
"""

from __future__ import annotations

import time

import httpx
from loguru import logger

from ..config import settings

# last provider that answered, per symbol (display/diagnostics only)
last_source: dict[str, str] = {}


def _from_yfinance(symbol: str) -> float | None:
    try:
        import yfinance as yf

        tk = yf.Ticker(symbol)
        fi = tk.fast_info
        lp = getattr(fi, "last_price", None) or (
            fi.get("lastPrice") if hasattr(fi, "get") else None)
        if lp:
            return float(lp)
        hist = tk.history(period="2d")
        if len(hist):
            return float(hist["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001
        logger.debug("yfinance quote failed {}: {}", symbol, exc)
    return None


def _from_finnhub(symbol: str) -> float | None:
    if not settings.finnhub_api_key:
        return None
    try:
        with httpx.Client(timeout=settings.request_timeout) as client:
            r = client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": symbol, "token": settings.finnhub_api_key},
            )
            r.raise_for_status()
            c = r.json().get("c")
            if c:  # Finnhub returns c=0 for unknown symbols
                return float(c)
    except Exception as exc:  # noqa: BLE001
        logger.debug("finnhub quote failed {}: {}", symbol, exc)
    return None


def parse_yahoo_chart(payload: dict) -> float | None:
    """Extract the regular-market price from Yahoo's v8 chart JSON. Pure."""
    try:
        meta = payload["chart"]["result"][0]["meta"]
        px = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(px) if px else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _from_yahoo_direct(symbol: str) -> float | None:
    """Raw Yahoo chart endpoint via httpx — bypasses the yfinance LIBRARY (the
    most common breakage is the library, not the endpoint). NB: still Yahoo
    infrastructure — client-path redundancy only; true vendor redundancy is
    finnhub with a key. (Stooq was evaluated and dropped: 404 from this region.)"""
    try:
        with httpx.Client(timeout=settings.request_timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }) as client:
            r = client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": "1d", "interval": "1d"},
            )
            r.raise_for_status()
            return parse_yahoo_chart(r.json())
    except Exception as exc:  # noqa: BLE001
        logger.debug("yahoo-direct quote failed {}: {}", symbol, exc)
    return None


PROVIDERS = [
    ("yfinance", _from_yfinance),
    ("finnhub", _from_finnhub),
    ("yahoo-direct", _from_yahoo_direct),
]


def fetch_quote(symbol: str) -> dict | None:
    """Uncached failover chain → {price, source} or None if every source fails."""
    for name, fn in PROVIDERS:
        px = fn(symbol)
        if px and px > 0:
            last_source[symbol] = name
            if name != "yfinance":
                logger.info("quote failover: {} served by {}", symbol, name)
            return {"price": px, "source": name}
    logger.warning("all quote providers failed for {}", symbol)
    return None


def provider_health(symbol: str = "SOXL") -> list[dict]:
    """Probe every provider directly (uncached) — for `surge quotes --health`."""
    out = []
    for name, fn in PROVIDERS:
        t0 = time.perf_counter()
        px = fn(symbol)
        ms = (time.perf_counter() - t0) * 1000
        configured = name != "finnhub" or bool(settings.finnhub_api_key)
        out.append({
            "provider": name,
            "configured": configured,
            "price": px,
            "ok": px is not None,
            "ms": round(ms),
        })
    return out
