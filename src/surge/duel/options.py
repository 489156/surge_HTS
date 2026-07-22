"""Options-flow snapshot archive — keyless, forward-accumulating.

Options positioning (implied vol, put/call balance) is a first-order driver
of a 3x ETF's intraday leg, but free sources offer NO history — only the
current chain. So the architecture's answer applies: snapshot the chain every
evening at call time, point-in-time, and let history accumulate. After a few
months the columns here become learnable variables (via duel_live_context-
style joins) and candidate factors; until then they are archive, not signal.

Reliability (2026-07-15 수리): the collector froze after a single row on
07-03 — the yfinance library path failed silently on the Actions runner and
the degrade-safe catch hid it at debug level. Fixes, mirroring quotes.py's
proven client-path redundancy:
  1. yfinance path first (keeps local/test behavior),
  2. RAW Yahoo options endpoint via httpx as fallback — bypasses the LIBRARY,
     which is the usual breakage, not the endpoint,
  3. a failed night now logs at WARNING with the per-path reasons, so a stall
     is visible in the pipeline logs instead of silent.
Everything remains degrade-safe — a failure records nothing and never touches
the call.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ..config import settings
from ..db import connect, upsert, utc_now


def _summarize(calls: list[dict], puts: list[dict], spot: float | None,
               expiry: str) -> dict | None:
    """Chain rows (dicts with strike/impliedVolatility/openInterest/volume)
    → the archived summary. Shared by both client paths."""
    if not calls or not puts:
        return None
    if not spot:
        strikes = sorted(c.get("strike") for c in calls if c.get("strike"))
        spot = strikes[len(strikes) // 2] if strikes else None
    if not spot:
        return None

    def _atm(rows: list[dict]) -> float | None:
        best = min((r for r in rows if r.get("strike") is not None),
                   key=lambda r: abs(r["strike"] - spot), default=None)
        iv = best.get("impliedVolatility") if best else None
        return float(iv) if iv and iv == iv else None

    ivs = [v for v in (_atm(calls), _atm(puts)) if v is not None]
    call_oi = sum(float(r.get("openInterest") or 0) for r in calls)
    put_oi = sum(float(r.get("openInterest") or 0) for r in puts)
    call_vol = sum(float(r.get("volume") or 0) for r in calls)
    put_vol = sum(float(r.get("volume") or 0) for r in puts)
    return {
        "expiry": expiry,
        "atm_iv": round(sum(ivs) / len(ivs), 4) if ivs else None,
        "pc_oi_ratio": round(put_oi / call_oi, 4) if call_oi else None,
        "pc_vol_ratio": round(put_vol / call_vol, 4) if call_vol else None,
    }


def _via_yfinance(symbol: str) -> dict | None:
    """Path 1 — the yfinance library."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    expiries = t.options
    if not expiries:
        return None
    expiry = expiries[0]
    chain = t.option_chain(expiry)
    if chain.calls.empty or chain.puts.empty:
        return None
    spot = getattr(t.fast_info, "last_price", None)
    return _summarize(chain.calls.to_dict("records"),
                      chain.puts.to_dict("records"), spot, expiry)


_YAHOO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _yahoo_crumb(client: httpx.Client) -> str:
    """Prime Yahoo's identity cookie (A1/A3) and fetch the matching crumb. The
    /v7/finance/options endpoint began returning 401 'Invalid Crumb' without
    this pair (2026-07 breakage that froze the archive at 07-03) — the chart
    endpoint quotes.py uses never needed it, so quotes stayed alive while this
    silently died. Best-effort: any step failing just yields an empty crumb and
    the caller still tries the request (some regions still serve crumbless)."""
    try:
        client.get("https://fc.yahoo.com/")            # sets the A1/A3 cookies
    except Exception:  # noqa: BLE001 — cookie prime is best-effort
        pass
    try:
        cr = client.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
        crumb = cr.text.strip() if cr.status_code == 200 else ""
    except Exception:  # noqa: BLE001
        crumb = ""
    # a valid crumb is a short token; an HTML/error body is not one
    return crumb if crumb and "<" not in crumb and len(crumb) <= 40 else ""


def _via_yahoo_direct(symbol: str) -> dict | None:
    """Path 2 — raw Yahoo options endpoint via httpx (bypasses the yfinance
    LIBRARY; same client-path redundancy that keeps quotes.py alive). Now
    cookie+crumb authenticated (see _yahoo_crumb)."""
    import datetime as dt

    with httpx.Client(timeout=settings.request_timeout,
                      headers={"User-Agent": _YAHOO_UA},
                      follow_redirects=True) as client:
        crumb = _yahoo_crumb(client)
        params = {"crumb": crumb} if crumb else {}
        r = client.get(
            f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}",
            params=params)
        r.raise_for_status()
        res = (r.json().get("optionChain") or {}).get("result") or []
    if not res:
        return None
    node = res[0]
    opts = (node.get("options") or [{}])[0]
    calls, puts = opts.get("calls") or [], opts.get("puts") or []
    exp_ts = opts.get("expirationDate")
    expiry = (dt.datetime.fromtimestamp(exp_ts, dt.timezone.utc)
              .date().isoformat() if exp_ts else "")
    spot = (node.get("quote") or {}).get("regularMarketPrice")
    return _summarize(calls, puts, spot, expiry)


def snapshot(symbol: str) -> dict | None:
    """Nearest-expiry chain summary: ATM IV (call/put mean), put/call open-
    interest ratio, put/call volume ratio. Tries both client paths; a full
    miss is WARNED (not silently dropped) so stalls surface in pipeline logs."""
    errors: list[str] = []
    for name, fn in (("yfinance", _via_yfinance), ("yahoo-direct", _via_yahoo_direct)):
        try:
            snap = fn(symbol)
            if snap is not None:
                return snap
            errors.append(f"{name}: empty chain")
        except Exception as exc:  # noqa: BLE001 — archive-only, never breaks the call
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    logger.warning("options snapshot MISSED {} ({})", symbol, " | ".join(errors))
    return None


def record(symbol: str, date: str) -> bool:
    """Persist one (symbol, session) chain snapshot. Idempotent; captured_at
    is write-once. Returns whether a row was written."""
    snap = snapshot(symbol)
    if snap is None:
        return False
    with connect() as conn:
        upsert(conn, "options_snapshots", [{
            "symbol": symbol, "date": date, **snap,
            "captured_at": utc_now(),
        }], immutable=("captured_at",))
    return True
