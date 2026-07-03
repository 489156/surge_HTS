"""Duel data layer (pair-aware).

Fetches a pair's universe (bull/bear legs + underlying index ETF + VIX/TNX +
Asian leads) and precomputes leak-safe feature columns:

- US frames: every feature column is `.shift(1)` — row at date D holds only
  information known at D−1's close.
- Asia frames: used UNshifted — their date-D bar is final hours before the US
  date-D open (time zones do the leak-proofing).
- The underlying's open→close return is unshifted and used ONLY as the label.

Also: the persistent `price_history` archive (secured historical data) — daily
bars for every registry symbol, enabling offline/backtest research independent
of any live vendor.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from ..db import connect, upsert, utc_now
from ..sources import market
from .pairs import DEFAULT_PAIR, get_pair

MACRO_SYMBOLS = ["^VIX", "^TNX"]
MARKET_PROXY = "QQQ"          # relative-strength benchmark (semis vs broad tech)
ASIA = {  # name -> (ticker, weight within the Asia composite)
    "TSMC": ("2330.TW", 0.40),
    "Samsung": ("005930.KS", 0.30),
    "SKHynix": ("000660.KS", 0.20),
    "TokyoElectron": ("8035.T", 0.10),
}
# Cross-asset macro context (leak-safe, shifted) — the "information-collection
# environment" expansion that feeds the shadow-FACTOR search: HY credit (risk
# appetite), the US dollar (semis are global/export), long bonds (duration bid).
EXTRA_MACRO = {"credit": "HYG", "dollar": "UUP", "bonds": "TLT"}


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("date").copy()
    d["date"] = pd.to_datetime(d["date"]).dt.date.astype(str)
    return d.drop_duplicates("date").set_index("date")


def fetch_shared(period: str = "2y") -> dict[str, pd.DataFrame]:
    """Macro (^VIX/^TNX) + Asia frames — identical for every pair, so multi-pair
    callers fetch them ONCE and pass them into fetch_frames(shared=...)."""
    frames: dict[str, pd.DataFrame] = {}
    for sym in MACRO_SYMBOLS:
        df = market.download_ohlcv([sym], period=period)
        if df.empty:
            logger.warning("duel: no data for {}", sym)
            continue
        frames[sym] = _tidy(df)
    for name, (ticker, _w) in ASIA.items():
        df = market.download_ohlcv([ticker], period=period)
        if df.empty:
            logger.warning("duel: no data for {} ({})", name, ticker)
            continue
        frames[name] = _tidy(df)
    for name, ticker in EXTRA_MACRO.items():
        df = market.download_ohlcv([ticker], period=period)
        if not df.empty:                              # absent ⇒ those factors stay silent
            frames[name] = _tidy(df)
    df = market.download_ohlcv([MARKET_PROXY], period=period)
    if not df.empty:                                  # relative-strength benchmark
        frames[MARKET_PROXY] = _tidy(df)
    return frames


def fetch_frames(period: str = "2y", pair: dict | None = None,
                 shared: dict[str, pd.DataFrame] | None = None,
                 ) -> dict[str, pd.DataFrame]:
    """symbol/name → tidy OHLCV indexed by ISO date, for one pair's universe.
    Pass `shared` (from fetch_shared) to skip re-downloading macro/Asia."""
    pair = pair or get_pair(DEFAULT_PAIR)
    frames: dict[str, pd.DataFrame] = dict(shared) if shared else {}
    for sym in (pair["bull"], pair["bear"], pair["underlying"]):
        df = market.download_ohlcv([sym], period=period)
        if df.empty:
            logger.warning("duel: no data for {}", sym)
            continue
        frames[sym] = _tidy(df)
    if shared is None:
        frames.update(fetch_shared(period))
    return frames


def _atr_pct(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return (tr.rolling(n).mean() / df["close"]).rename("atr_pct")


def prepare(frames: dict[str, pd.DataFrame], pair: dict | None = None,
            ) -> dict[str, pd.DataFrame]:
    """Add feature columns. US features are shifted so row D = knowledge at D−1."""
    pair = pair or get_pair(DEFAULT_PAIR)
    frames = {
        k: (_tidy(v) if "date" in v.columns else v) for k, v in frames.items()
    }
    out: dict[str, pd.DataFrame] = {}

    und = pair["underlying"]
    if und in frames:
        s = frames[und].copy()
        ret = s["close"].pct_change()
        s["f_ret1"] = ret.shift(1)
        s["f_ret5"] = s["close"].pct_change(5).shift(1)
        s["f_vol20"] = ret.rolling(20).std().shift(1)
        s["f_sma50_dist"] = (s["close"] / s["close"].rolling(50).mean() - 1).shift(1)
        # Intraday-aware features: the LABEL is open→close, but close→close
        # features mix in the overnight gap. Decompose so the learner can see
        # the intraday-only history separately (all shifted → D−1 knowledge).
        oc = s["close"] / s["open"] - 1
        gap = s["open"] / s["close"].shift(1) - 1
        s["f_oc_mom5"] = oc.rolling(5).mean().shift(1)   # intraday-only momentum
        s["f_oc1"] = oc.shift(1)                          # prior session intraday
        s["f_gap1"] = gap.shift(1)                        # prior session's open gap
        s["oc_ret"] = oc          # LABEL ONLY (same-day)
        # gap_ret is same-day EXECUTION-time info: the open gap is realized at
        # the moment the committed rule enters. Used ONLY by the gap guard —
        # never as a decision feature (the call itself is committed pre-open).
        s["gap_ret"] = gap
        # technical layer (leak-safe, D−1): Wilder RSI(14) — overbought/oversold
        # state the linear momentum features can't express — and relative
        # volume (participation confirms or questions a move).
        delta = s["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        s["f_rsi14"] = (100 - 100 / (1 + gain / loss.mask(loss == 0))).shift(1)
        if "volume" in s.columns:
            vol_ma = s["volume"].rolling(20).mean()
            s["f_rvol20"] = (s["volume"] / vol_ma.mask(vol_ma == 0)).shift(1)
        # relative strength vs the broad-tech proxy (leak-safe, D−1): is the
        # pair's sector out/under-running the market it trades inside of?
        proxy = frames.get(MARKET_PROXY)
        if proxy is not None and und != MARKET_PROXY:
            q = _tidy(proxy) if "date" in proxy.columns else proxy
            rel = (s["close"].pct_change(20)
                   - q["close"].pct_change(20).reindex(s.index))
            s["f_rel20"] = rel.shift(1)
        out[und] = s

    if "^VIX" in frames:
        v = frames["^VIX"].copy()
        v["f_level"] = v["close"].shift(1)
        v["f_chg"] = v["close"].pct_change().shift(1)
        out["^VIX"] = v

    if "^TNX" in frames:
        t = frames["^TNX"].copy()
        t["f_chg"] = t["close"].diff().shift(1)  # ^TNX points: 0.10 ≈ 10bp
        out["^TNX"] = t

    for name in EXTRA_MACRO:                       # cross-asset, leak-safe (D−1)
        if name in frames:
            x = frames[name].copy()
            x["f_chg"] = x["close"].pct_change().shift(1)
            out[name] = x

    for leg in (pair["bull"], pair["bear"]):
        if leg in frames:
            e = frames[leg].copy()
            e["f_atr_pct"] = _atr_pct(e).shift(1)  # bracket width from D−1
            out[leg] = e

    for name in ASIA:
        if name in frames:
            a = frames[name].copy()
            a["f_ret"] = a["close"].pct_change()          # date-D return (UNshifted)
            a["f_vol20"] = a["f_ret"].rolling(20).std()
            out[name] = a

    return out


def _base_ctx(pair: dict, date: str) -> dict:
    return {
        "date": date, "pair": pair, "underlying": pair["underlying"],
        "und_ret1": None, "und_ret5": None, "und_vol20": None,
        "und_sma50_dist": None, "und_oc_mom5": None, "und_oc1": None,
        "und_gap1": None, "und_rel20": None, "und_rsi": None, "und_rvol": None,
        "vix_level": None, "vix_chg": None,
        "tnx_chg": None, "credit_chg": None, "dollar_chg": None,
        "bonds_chg": None, "asia": {}, "futures_ret": None, "atr_pct": {},
    }


def context_for(prepared: dict[str, pd.DataFrame], date: str,
                pair: dict | None = None) -> dict | None:
    """Signal context for US session `date` (historical replay path)."""
    pair = pair or get_pair(DEFAULT_PAIR)
    und = prepared.get(pair["underlying"])
    if und is None or date not in und.index:
        return None
    row = und.loc[date]
    if pd.isna(row.get("f_sma50_dist")) or pd.isna(row.get("f_vol20")):
        return None  # warmup not satisfied

    ctx = _base_ctx(pair, date)
    ctx.update(
        und_ret1=float(row["f_ret1"]), und_ret5=float(row["f_ret5"]),
        und_vol20=float(row["f_vol20"]), und_sma50_dist=float(row["f_sma50_dist"]),
    )
    for key, col in (("und_oc_mom5", "f_oc_mom5"), ("und_oc1", "f_oc1"),
                     ("und_gap1", "f_gap1"), ("und_rel20", "f_rel20"),
                     ("und_rsi", "f_rsi14"), ("und_rvol", "f_rvol20")):
        v = row.get(col)
        if v is not None and not pd.isna(v):
            ctx[key] = float(v)
    vix = prepared.get("^VIX")
    if vix is not None and date in vix.index and not pd.isna(vix.loc[date, "f_level"]):
        ctx["vix_level"] = float(vix.loc[date, "f_level"])
        ctx["vix_chg"] = float(vix.loc[date, "f_chg"])
    tnx = prepared.get("^TNX")
    if tnx is not None and date in tnx.index and not pd.isna(tnx.loc[date, "f_chg"]):
        ctx["tnx_chg"] = float(tnx.loc[date, "f_chg"])
    for name in EXTRA_MACRO:
        x = prepared.get(name)
        if x is not None and date in x.index and not pd.isna(x.loc[date, "f_chg"]):
            ctx[f"{name}_chg"] = float(x.loc[date, "f_chg"])
    for name, (_t, w) in ASIA.items():
        a = prepared.get(name)
        if a is None or date not in a.index:
            continue
        r, v = a.loc[date, "f_ret"], a.loc[date, "f_vol20"]
        if pd.isna(r) or pd.isna(v) or v == 0:
            continue
        ctx["asia"][name] = {"ret": float(r), "vol": float(v), "weight": w}
    for leg in (pair["bull"], pair["bear"]):
        e = prepared.get(leg)
        if e is not None and date in e.index and not pd.isna(e.loc[date, "f_atr_pct"]):
            ctx["atr_pct"][leg] = float(e.loc[date, "f_atr_pct"])
    return ctx


def latest_context(prepared: dict[str, pd.DataFrame], session: str,
                   pair: dict | None = None) -> dict | None:
    """LIVE-ONLY context for a future `session` with no bar yet: features are
    computed UNshifted from the latest completed bars (leak-free — those
    sessions are over). Asia bars dated == session are included when present."""
    pair = pair or get_pair(DEFAULT_PAIR)
    und = prepared.get(pair["underlying"])
    if und is None or len(und) < 51:
        return None
    closes = und["close"]
    rets = closes.pct_change()
    ctx = _base_ctx(pair, session)
    ctx.update(
        und_ret1=float(rets.iloc[-1]),
        und_ret5=float(closes.pct_change(5).iloc[-1]),
        und_vol20=float(rets.rolling(20).std().iloc[-1]),
        und_sma50_dist=float(closes.iloc[-1] / closes.rolling(50).mean().iloc[-1] - 1),
    )
    oc = und["close"] / und["open"] - 1              # completed bars only
    gap = und["open"] / und["close"].shift(1) - 1
    if len(oc) >= 5 and not pd.isna(oc.iloc[-5:].mean()):
        ctx["und_oc_mom5"] = float(oc.iloc[-5:].mean())
    if not pd.isna(oc.iloc[-1]):
        ctx["und_oc1"] = float(oc.iloc[-1])
    if not pd.isna(gap.iloc[-1]):
        ctx["und_gap1"] = float(gap.iloc[-1])
    proxy = prepared.get(MARKET_PROXY)
    if (proxy is not None and pair["underlying"] != MARKET_PROXY
            and len(proxy) >= 21 and len(closes) >= 21):
        ctx["und_rel20"] = float(
            (closes.iloc[-1] / closes.iloc[-21] - 1)
            - (proxy["close"].iloc[-1] / proxy["close"].iloc[-21] - 1))
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.mask(loss == 0))
    if not pd.isna(rsi.iloc[-1]):
        ctx["und_rsi"] = float(rsi.iloc[-1])
    if "volume" in und.columns and len(und) >= 20:
        vma = und["volume"].rolling(20).mean().iloc[-1]
        if vma and not pd.isna(vma):
            ctx["und_rvol"] = float(und["volume"].iloc[-1] / vma)
    vix = prepared.get("^VIX")
    if vix is not None and len(vix) >= 2:
        ctx["vix_level"] = float(vix["close"].iloc[-1])
        ctx["vix_chg"] = float(vix["close"].pct_change().iloc[-1])
    tnx = prepared.get("^TNX")
    if tnx is not None and len(tnx) >= 2:
        ctx["tnx_chg"] = float(tnx["close"].diff().iloc[-1])
    for name in EXTRA_MACRO:
        x = prepared.get(name)
        if x is not None and len(x) >= 2:
            ctx[f"{name}_chg"] = float(x["close"].pct_change().iloc[-1])
    for name, (_t, w) in ASIA.items():
        a = prepared.get(name)
        if a is None or session not in a.index:
            continue  # only the same-session Asia bar is a valid lead
        r, v = a.loc[session, "f_ret"], a.loc[session, "f_vol20"]
        if not (pd.isna(r) or pd.isna(v) or v == 0):
            ctx["asia"][name] = {"ret": float(r), "vol": float(v), "weight": w}
    for leg in (pair["bull"], pair["bear"]):
        e = prepared.get(leg)
        if e is not None and len(e) > 15:
            atr = _atr_pct(e).iloc[-1]
            if not pd.isna(atr):
                ctx["atr_pct"][leg] = float(atr)
    return ctx


# ── Secured history archive ──────────────────────────────────────────────────
def archive_symbols() -> list[str]:
    from .pairs import all_symbols

    return [*all_symbols(), *MACRO_SYMBOLS, *[t for t, _w in ASIA.values()]]


def archive(period: str = "max") -> dict:
    """Persist daily bars for the whole registry into price_history. Idempotent
    upserts; run incrementally (e.g. '3mo') daily, or 'max' once for backfill."""
    total = 0
    per_symbol: dict[str, int] = {}
    for sym in archive_symbols():
        df = market.download_ohlcv([sym], period=period)
        if df.empty:
            per_symbol[sym] = 0
            continue
        rows = [
            {
                "symbol": sym,
                "date": str(pd.to_datetime(r["date"]).date()),
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "volume": r["volume"],
                "source": "yfinance", "captured_at": utc_now(),
            }
            for r in df.to_dict("records")
        ]
        with connect() as conn:
            upsert(conn, "price_history", rows)
        per_symbol[sym] = len(rows)
        total += len(rows)
    logger.info("price_history archive: {} rows across {} symbols",
                total, len(per_symbol))
    return {"total_rows": total, "symbols": per_symbol}


def frames_from_archive(pair: dict | None = None) -> dict[str, pd.DataFrame]:
    """Rebuild a pair's frames from price_history — offline research path."""
    pair = pair or get_pair(DEFAULT_PAIR)
    name_by_ticker = {t: n for n, (t, _w) in ASIA.items()}
    wanted = [pair["bull"], pair["bear"], pair["underlying"], *MACRO_SYMBOLS,
              MARKET_PROXY, *name_by_ticker]
    frames: dict[str, pd.DataFrame] = {}
    with connect() as conn:
        for sym in wanted:
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume FROM price_history "
                "WHERE symbol=? ORDER BY date", (sym,),
            ).fetchall()
            if not rows:
                continue
            df = pd.DataFrame([dict(r) for r in rows])
            frames[name_by_ticker.get(sym, sym)] = df.set_index("date")
    return frames
