"""Watch engine — mechanical levels + long-term optionality dossier.

Reads the existing adapters only (surge.sources.market for US, surge.sources.krx
for KR). Computes reproducible price levels; assigns NO probabilities anywhere.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from loguru import logger

from ..db import connect, upsert, utc_now
from . import targets as T


def _ohlcv(tg: dict) -> pd.DataFrame:
    """date,open,high,low,close,volume sorted asc — both markets normalized."""
    if tg["mkt"] == "us":
        from ..sources import market
        df = market.download_ohlcv([tg["t"]], period="1y")
        if df.empty:
            return pd.DataFrame()
        df = df[["date", "open", "high", "low", "close", "volume"]]
    else:
        from ..sources import krx
        end = date.today()
        df = krx.ohlcv(tg["t"], (end - timedelta(days=420)).isoformat(),
                       end.isoformat())
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    return df.sort_values("date").reset_index(drop=True)


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    v = tr.rolling(n).mean().iloc[-1]
    return float(v) if pd.notna(v) else float((df["high"] - df["low"]).mean())


def _kr_smart(tg: dict) -> float:
    if tg["mkt"] != "kr":
        return 0.0
    from ..sources import krx
    f = krx.investor_flows(tg["t"], pages=2)
    if f.empty or len(f) < 20:
        return 0.0
    net = (f["foreign_net"] + f["inst_net"]).astype(float)
    d20 = net.tail(20).mean()
    return float(net.tail(5).sum() / (abs(d20) * 5)) if d20 else 0.0


def _levels(tg: dict, horizon: str) -> dict | None:
    df = _ohlcv(tg)
    if df.empty or len(df) < 30:
        return None
    close = df["close"].astype(float)
    ref = float(close.iloc[-1])
    atr = _atr(df)
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean()) if len(close) >= 50 else ma20
    ma200 = float(close.tail(200).mean()) if len(close) >= 200 else ma50
    vol = df["volume"].astype(float)
    rvol = float(vol.iloc[-1] / vol.iloc[-21:-1].mean()) if vol.iloc[-21:-1].mean() else 1.0
    mom = float(ref / close.iloc[-6] - 1) if len(close) > 6 else 0.0
    sm = _kr_smart(tg)

    if horizon == "short":   # ≤1 week — tight ATR bracket
        buy_low = round(min(float(df["low"].tail(3).min()), ref - 0.5 * atr), 2)
        buy_high = round(ref + 0.3 * atr, 2)            # don't chase far above
        stop = round(ref - 1.0 * atr, 2)
        target = round(ref + 1.5 * atr, 2)
        trend = "up" if ref > ma20 else "down"
        why = [f"ATR {atr:.2f}", f"20일선 {'위' if ref > ma20 else '아래'}",
               f"RVOL {rvol:.1f}x", f"5일 {mom*100:+.0f}%"]
    else:                    # swing — MA regime + multi-week channel
        ch_high = float(df["high"].tail(60).max())
        buy_low = round(min(ma50, ref - 1.0 * atr), 2)
        buy_high = round(ref + 0.5 * atr, 2)
        stop = round(ref - 2.0 * atr, 2)
        target = round(max(ch_high, ref + 3.0 * atr), 2)
        trend = ("up" if ref > ma50 > ma200 else
                 "down" if ref < ma50 < ma200 else "mixed")
        why = [f"추세 {trend}", f"50/200선 {ma50:.0f}/{ma200:.0f}",
               f"60일고 {ch_high:.0f}", f"ATR {atr:.2f}"]

    rr = round((target - ref) / (ref - stop), 2) if ref > stop else 0.0
    # transparent setup score 0..100
    s = 50.0
    s += 12 if ref > ma20 else -8
    s += min(15, max(-10, mom * 80))
    s += 10 if rvol >= 1.5 else 0
    if tg["mkt"] == "kr":
        s += 12 if sm > 0.5 else (-6 if sm < -0.5 else 0)
    if horizon == "swing":
        s += 10 if trend == "up" else (-12 if trend == "down" else 0)
    score = round(max(0, min(100, s)), 0)
    if tg["mkt"] == "kr":
        why.append(f"수급 {sm:+.1f}σ")
    return {"ticker": tg["t"], "name": tg["name"], "mkt": tg["mkt"],
            "horizon": horizon, "ref": round(ref, 2), "buy_low": buy_low,
            "buy_high": buy_high, "stop": stop, "target": target, "rr": rr,
            "score": score, "trend": trend, "reasons": why,
            "asof": str(df["date"].iloc[-1])}


def levels(horizon: str, persist: bool = False) -> list[dict]:
    out = []
    for tg in T.by_horizon(horizon):
        lv = _levels(tg, horizon)
        if lv:
            out.append(lv)
    out.sort(key=lambda x: x["score"], reverse=True)
    if persist and out:
        now = utc_now()
        rows = [{
            "asof": lv["asof"], "ticker": lv["ticker"], "market": lv["mkt"],
            "horizon": horizon, "ref_close": lv["ref"], "buy_low": lv["buy_low"],
            "buy_high": lv["buy_high"], "stop": lv["stop"], "target": lv["target"],
            "rr": lv["rr"], "setup_score": lv["score"], "trend": lv["trend"],
            "note": " · ".join(lv["reasons"]), "captured_at": now,
        } for lv in out]
        with connect() as conn:
            upsert(conn, "watch_levels", rows, immutable=("captured_at",))
    logger.info("watch {}: {} targets", horizon, len(out))
    return out


# ── long-term multibagger dossier (NO probability) ───────────────────────────
def _us_cap(ticker: str) -> float | None:
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        return float(getattr(fi, "market_cap", None) or 0) or None
    except Exception:  # noqa: BLE001
        return None


def multibagger() -> list[dict]:
    """Structural optionality dossier for the long-horizon targets. Reports the
    facts that govern 10x ROOM (cap, base, theme) — explicitly NOT a probability."""
    out = []
    for tg in T.by_horizon("long"):
        df = _ohlcv(tg)
        if df.empty or len(df) < 60:
            continue
        close = df["close"].astype(float)
        ref = float(close.iloc[-1])
        hi52 = float(df["high"].tail(252).max())
        lo52 = float(df["low"].tail(252).min())
        if ref <= 0 or hi52 <= 0 or lo52 <= 0:
            continue
        drawdown = ref / hi52 - 1
        off_low = ref / lo52 - 1
        cap = _us_cap(tg["t"]) if tg["mkt"] == "us" else None
        # optionality score (qualitative 0..100): room flag + base depth.
        room_pts = {"high": 35, "mid": 20, "low": 5, "na": 0}[tg["room"]]
        base_pts = 25 if drawdown <= -0.4 else (15 if drawdown <= -0.2 else 5)
        score = room_pts + base_pts + (20 if off_low < 0.5 else 8)
        capS = f"${cap/1e9:.0f}B" if cap else "—"
        tenx = (f"10x ≈ {capS}→${cap*10/1e9:.0f}B" if cap
                else "10x 여지: 소형일수록 큼(KR 시총 별도확인)")
        out.append({
            "ticker": tg["t"], "name": tg["name"], "mkt": tg["mkt"],
            "theme": tg["theme"], "room": tg["room"], "cap": capS,
            "drawdown": round(drawdown, 2), "score": min(100, score),
            "tenx": tenx, "asof": str(df["date"].iloc[-1])})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out
