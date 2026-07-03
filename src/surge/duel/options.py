"""Options-flow snapshot archive — keyless, forward-accumulating.

Options positioning (implied vol, put/call balance) is a first-order driver
of a 3x ETF's intraday leg, but free sources offer NO history — only the
current chain. So the architecture's answer applies: snapshot the chain every
evening at call time, point-in-time, and let history accumulate. After a few
months the columns here become learnable variables (via duel_live_context-
style joins) and candidate factors; until then they are archive, not signal.

Source: yfinance option chains (keyless). Everything is degrade-safe — a
missing chain, an empty expiry, a network failure records nothing and never
touches the call.
"""

from __future__ import annotations

from loguru import logger

from ..db import connect, upsert, utc_now


def snapshot(symbol: str) -> dict | None:
    """Nearest-expiry chain summary: ATM IV (call/put mean), put/call open-
    interest ratio, put/call volume ratio. None when unavailable."""
    try:
        import yfinance as yf

        t = yf.Ticker(symbol)
        expiries = t.options
        if not expiries:
            return None
        expiry = expiries[0]
        chain = t.option_chain(expiry)
        calls, puts = chain.calls, chain.puts
        if calls.empty or puts.empty:
            return None
        last = getattr(t.fast_info, "last_price", None)
        if not last:
            # midpoint of the strike grid as a crude spot proxy
            last = float(calls["strike"].median())
        atm_call = calls.iloc[(calls["strike"] - last).abs().argsort()[:1]]
        atm_put = puts.iloc[(puts["strike"] - last).abs().argsort()[:1]]
        ivs = [float(v) for v in (atm_call["impliedVolatility"].iloc[0],
                                  atm_put["impliedVolatility"].iloc[0])
               if v == v and v]
        call_oi = float(calls["openInterest"].fillna(0).sum())
        put_oi = float(puts["openInterest"].fillna(0).sum())
        call_vol = float(calls["volume"].fillna(0).sum())
        put_vol = float(puts["volume"].fillna(0).sum())
        return {
            "expiry": expiry,
            "atm_iv": round(sum(ivs) / len(ivs), 4) if ivs else None,
            "pc_oi_ratio": round(put_oi / call_oi, 4) if call_oi else None,
            "pc_vol_ratio": round(put_vol / call_vol, 4) if call_vol else None,
        }
    except Exception as exc:  # noqa: BLE001 — archive-only, never breaks the call
        logger.debug("options snapshot {} failed: {}", symbol, exc)
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
