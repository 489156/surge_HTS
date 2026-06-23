"""Constituent baskets + AMVF/ADVCRF/NGRF framework features per leveraged pair.

The AMVF/ADVCRF/NGRF frameworks (see docs) describe how *attention → smart money
→ liquidity → value-chain rotation → growth* moves through a sector. The duel
underlyings are sector ETFs (SOXX semis, QQQ/XLK tech, XBI biotech), so the
faithful adaptation is to measure those framework signals on the ETF's OWN
constituent basket — the value-chain names from the user's watch universe — and
use them as leading factors for the leveraged pair's next-session direction.

Every feature is computed from constituent OHLCV (keyless, one batched fetch) and
SHIFTED by one session for the historical replay: row at date D carries only the
basket's state as of D−1 close, so it is a leak-free predictor of D's open→close.

Framework → feature mapping:
- AMVF  Lead Attention / Liquidity → `breadth` (participation) + `rvol` (volume thrust)
- AMVF  Smart-money leadership     → `leadership` (the cap leader vs the basket)
- ADVCRF value-chain rotation      → `rotation` (back-end/equipment vs front-end)
- NGRF  growth momentum            → `growth` (basket medium-term momentum)
"""

from __future__ import annotations

import pandas as pd

from ..sources import market

# pair_id → basket spec. `tickers` = value-chain constituents (from the watch
# universe); `leader` = the dominant smart-money name; `back` = value-chain
# back-end (equipment/materials) used for the ADVCRF rotation read.
BASKETS: dict[str, dict] = {
    "soxl_soxs": {   # semis value chain: AI compute → equipment
        "tickers": ["NVDA", "AVGO", "AMD", "MRVL", "ALAB", "CRDO",
                    "AMAT", "LRCX", "KLAC", "ASML", "TSM", "MU"],
        "leader": "NVDA",
        "back": ["AMAT", "LRCX", "KLAC", "ASML"],
    },
    "tqqq_sqqq": {   # nasdaq-100 mega-cap tech
        "tickers": ["NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL",
                    "AVGO", "TSLA", "NFLX", "COST"],
        "leader": "NVDA", "back": [],
    },
    "tecl_tecs": {   # tech select sector
        "tickers": ["AAPL", "MSFT", "NVDA", "AVGO", "CRM", "ORCL",
                    "ADBE", "AMD", "CSCO", "ACN"],
        "leader": "NVDA", "back": [],
    },
    "labu_labd": {   # biotech — breadth-driven, no single leader
        "tickers": ["VRTX", "REGN", "GILD", "AMGN", "BIIB", "MRNA",
                    "INCY", "ALNY", "NBIX", "EXEL"],
        "leader": None, "back": [],
    },
}


def framework_features(pair_id: str, period: str = "2y",
                       shift: bool = True) -> pd.DataFrame:
    """Per-date AMVF/ADVCRF/NGRF aggregates over the pair's constituent basket.
    Columns: breadth, rvol, leadership, rotation, growth (indexed by ISO date).
    `shift=True` lags every column one session (D row = D−1 state → leak-free
    predictor of D); `shift=False` (live) keeps the latest completed state to
    predict the upcoming session."""
    spec = BASKETS.get(pair_id)
    if not spec:
        return pd.DataFrame()
    df = market.download_ohlcv(spec["tickers"], period=period)
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    df = df.sort_values(["symbol", "date"])
    df["ret"] = df.groupby("symbol")["close"].pct_change()
    df["avgvol20"] = df.groupby("symbol")["volume"].transform(
        lambda v: v.rolling(20).mean())
    df["rvol"] = df["volume"] / df["avgvol20"]
    df["mom20"] = df.groupby("symbol")["close"].transform(
        lambda c: c.pct_change(20))

    g = df.groupby("date")
    feat = pd.DataFrame({
        "breadth": g["ret"].apply(lambda s: (s > 0).mean()),
        "rvol": g["rvol"].mean(),
        "growth": g["mom20"].mean(),
    })
    leader = spec.get("leader")
    if leader and leader in set(df["symbol"]):
        lead = df[df["symbol"] == leader].set_index("date")["ret"]
        feat["leadership"] = lead - g["ret"].mean()
    else:
        feat["leadership"] = pd.NA
    back = spec.get("back") or []
    if back:
        br = df[df["symbol"].isin(back)].groupby("date")["ret"].mean()
        fr = df[~df["symbol"].isin(back)].groupby("date")["ret"].mean()
        feat["rotation"] = br - fr
    else:
        feat["rotation"] = pd.NA

    feat = feat.sort_index()
    return feat.shift(1) if shift else feat
