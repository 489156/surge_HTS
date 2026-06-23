"""Korean-market data adapter (rotation module).

Two tiers:
- **Keyless** (FinanceDataReader): daily OHLCV + the full KRX listing. Needs no
  account; powers the price/volume/sector/value-chain layers.
- **Account-gated** (pykrx → KRX MDC at data.krx.co.kr): investor net flows
  (smart money) and short balance (days-to-cover). Need a FREE KRX data account;
  `SURGE_KRX_ID`/`SURGE_KRX_PW` are bridged into the `KRX_ID`/`KRX_PW` env vars
  pykrx reads.

Every MDC call is defensive: pykrx raises column KeyErrors (e.g. '거래대금',
'거래량') when the server returns no rows, so we catch and return an empty frame
rather than crash. `health()` (and `surge krx-check`) probe each capability and
report exactly what is reachable — so the live round-trip can be confirmed on a
real machine even though this sandbox's network does not serve the MDC API.
"""

from __future__ import annotations

import os

import pandas as pd
from loguru import logger

from ..config import settings


def bridge_credentials() -> bool:
    """Expose SURGE_KRX_ID/PW to pykrx as KRX_ID/KRX_PW. Returns True if both set."""
    if settings.krx_id and settings.krx_pw:
        os.environ["KRX_ID"] = settings.krx_id
        os.environ["KRX_PW"] = settings.krx_pw
        return True
    return False


def _tidy(df: pd.DataFrame, date_col: str = "Date") -> pd.DataFrame:
    d = df.reset_index() if df.index.name or date_col not in df.columns else df.copy()
    d.columns = [str(c) for c in d.columns]
    if date_col in d.columns:
        d = d.rename(columns={date_col: "date"})
    elif "index" in d.columns:
        d = d.rename(columns={"index": "date"})
    if "date" in d.columns:
        d["date"] = pd.to_datetime(d["date"]).dt.date.astype(str)
    return d


# ── keyless ──────────────────────────────────────────────────────────────────
def ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Daily OHLCV via FinanceDataReader (keyless). Columns: date,open,high,low,
    close,volume. Empty frame on failure."""
    try:
        import FinanceDataReader as fdr

        df = fdr.DataReader(ticker, start, end)
        if df is None or not len(df):
            return pd.DataFrame()
        d = _tidy(df)
        ren = {"Open": "open", "High": "high", "Low": "low", "Close": "close",
               "Volume": "volume"}
        d = d.rename(columns=ren)
        keep = [c for c in ("date", "open", "high", "low", "close", "volume")
                if c in d.columns]
        return d[keep]
    except Exception as exc:  # noqa: BLE001
        logger.debug("krx ohlcv {} failed: {}", ticker, exc)
        return pd.DataFrame()


def listing(market: str = "KRX") -> pd.DataFrame:
    """All listed Korean equities (keyless): Code, Name, Market, Dept/Sector."""
    try:
        import FinanceDataReader as fdr

        df = fdr.StockListing(market)
        return df if df is not None else pd.DataFrame()
    except Exception as exc:  # noqa: BLE001
        logger.debug("krx listing failed: {}", exc)
        return pd.DataFrame()


# ── account-gated (KRX MDC) ──────────────────────────────────────────────────
def _stock():
    try:
        from pykrx import stock
        return stock
    except ImportError:
        logger.warning("pykrx not installed — `uv sync --extra kr`")
        return None


def _naver_flows(ticker: str, pages: int = 3) -> pd.DataFrame:
    """Foreign/institutional net buying (shares) from Naver Finance — keyless and
    reliable (pykrx's KRX-MDC path returns empty against current KRX). Returns
    columns: date, foreign_net, inst_net."""
    import io

    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    rows = []
    with httpx.Client(timeout=settings.request_timeout, headers=headers) as client:
        for page in range(1, pages + 1):
            try:
                r = client.get("https://finance.naver.com/item/frgn.naver",
                               params={"code": ticker, "page": page})
                r.encoding = "euc-kr"
                tables = pd.read_html(io.StringIO(r.text))
            except Exception as exc:  # noqa: BLE001
                logger.debug("naver flows {} p{} failed: {}", ticker, page, exc)
                break
            tbl = next((t for t in tables if t.shape[0] >= 5 and t.shape[1] >= 8),
                       None)
            if tbl is None:
                continue
            # flatten 2-row header, then locate columns by name (position-robust)
            tbl.columns = [
                " ".join(str(x) for x in c if "Unnamed" not in str(x)).strip()
                if isinstance(c, tuple) else str(c) for c in tbl.columns]
            col = {"date": _find(tbl, "날짜"), "foreign_net": _find(tbl, "외국인"),
                   "inst_net": _find(tbl, "기관")}
            if not all(col.values()):
                continue
            sub = tbl[[col["date"], col["foreign_net"], col["inst_net"]]].copy()
            sub.columns = ["date", "foreign_net", "inst_net"]
            sub = sub[sub["date"].astype(str).str.match(r"\d{4}\.\d{2}\.\d{2}")]
            rows.append(sub)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"], format="%Y.%m.%d").dt.date.astype(str)
    for c in ("foreign_net", "inst_net"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna().drop_duplicates("date").sort_values("date")


def _find(df: pd.DataFrame, needle: str) -> str | None:
    return next((c for c in df.columns if needle in str(c)), None)


def investor_flows(ticker: str, start: str | None = None, end: str | None = None,
                   pages: int = 3) -> pd.DataFrame:
    """Foreign + institutional net buying (shares) per date. PRIMARY = Naver
    (keyless); FALLBACK = pykrx (KRX account). Optional [start,end] ISO filter."""
    df = _naver_flows(ticker, pages=pages)
    if df.empty:   # fallback to pykrx MDC if a KRX account is configured
        stock = _stock()
        if stock is not None and bridge_credentials() and start and end:
            try:
                raw = stock.get_market_trading_value_by_date(start, end, ticker)
                if raw is not None and len(raw):
                    df = raw
            except Exception as exc:  # noqa: BLE001
                logger.debug("krx investor_flows fallback {}: {}", ticker, exc)
    if not df.empty and start and end and "date" in df.columns:
        s, e = start.replace("-", ""), end.replace("-", "")
        d = df["date"].str.replace("-", "")
        df = df[(d >= s) & (d <= e)]
    return df


def short_balance(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Short-interest balance by date (needs KRX account). Empty on no-data."""
    stock = _stock()
    if stock is None:
        return pd.DataFrame()
    bridge_credentials()
    try:
        df = stock.get_shorting_balance_by_date(start, end, ticker)
        return df if df is not None and len(df) else pd.DataFrame()
    except Exception as exc:  # noqa: BLE001
        logger.debug("krx short_balance {} unavailable: {}", ticker, exc)
        return pd.DataFrame()


# ── self-diagnostic ──────────────────────────────────────────────────────────
def health(ticker: str = "005930", start: str = "20240102",
           end: str = "20240131") -> dict:
    """Probe every capability against a known-historical window; report what is
    actually reachable from THIS machine."""
    has_creds = bool(settings.krx_id and settings.krx_pw)
    bridged = bridge_credentials()
    caps = []

    def probe(name: str, gated: bool, fn):
        try:
            df = fn()
            caps.append({"capability": name, "gated": gated,
                         "rows": len(df), "ok": len(df) > 0, "error": None})
        except Exception as exc:  # noqa: BLE001
            caps.append({"capability": name, "gated": gated, "rows": 0,
                         "ok": False, "error": f"{type(exc).__name__}: {exc}"[:60]})

    probe("ohlcv (keyless)", False, lambda: ohlcv(ticker, start, end))
    probe("listing (keyless)", False, lambda: listing("KRX"))
    probe("investor_flows (Naver, keyless)", False,
          lambda: investor_flows(ticker))   # smart money — now keyless
    probe("short_balance (KRX account)", True,
          lambda: short_balance(ticker, start, end))

    keyless_ok = all(c["ok"] for c in caps if not c["gated"])
    gated_ok = all(c["ok"] for c in caps if c["gated"])
    return {
        "creds_present": has_creds,
        "creds_bridged": bridged,
        "keyless_ok": keyless_ok,
        "gated_ok": gated_ok,
        "capabilities": caps,
    }
