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


_LISTING_CACHE: dict = {"ts": 0.0, "data": {}}


def _listing_dict() -> dict[str, float]:
    import time
    now = time.time()
    if _LISTING_CACHE["data"] and now - _LISTING_CACHE["ts"] < 300:
        return _LISTING_CACHE["data"]
    df = listing("KRX")
    if df.empty or "Code" not in df.columns or "Close" not in df.columns:
        return _LISTING_CACHE["data"]
    d = dict(zip(df["Code"].astype(str), pd.to_numeric(df["Close"], errors="coerce")))
    _LISTING_CACHE["ts"] = now
    _LISTING_CACHE["data"] = d
    return d


def latest_close(ticker: str) -> float | None:
    """Fast keyless lookup of the latest confirmed close for any KR ticker."""
    d = _listing_dict()
    val = d.get(ticker)
    if val is not None and not pd.isna(val) and val > 0:
        return float(val)
    return None


def batch_latest_closes(tickers: list[str]) -> dict[str, float | None]:
    """Concurrent/batch lookup of latest confirmed close for multiple KR tickers."""
    d = _listing_dict()
    return {t: (float(d[t]) if t in d and not pd.isna(d[t]) and d[t] > 0 else None) for t in tickers}



# ── account-gated (KRX MDC) ──────────────────────────────────────────────────
def _stock():
    try:
        from pykrx import stock
        return stock
    except ImportError:
        logger.warning("pykrx not installed — `uv sync --extra kr`")
        return None


def _parse_kr_int(val) -> float | None:
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _naver_mobile_flows(ticker: str, pages: int = 3) -> pd.DataFrame:
    """Structured JSON fallback via Naver mobile trend API (keyless, fast)."""
    import httpx

    page_size = min(max(pages, 1) * 20, 60)
    url = f"https://m.stock.naver.com/api/stock/{ticker}/trend"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": f"https://m.stock.naver.com/domestic/stock/{ticker}/total",
    }
    try:
        with httpx.Client(timeout=settings.request_timeout, headers=headers) as client:
            r = client.get(url, params={"pageSize": page_size})
            r.raise_for_status()
            items = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("naver mobile flows {} failed: {}", ticker, exc)
        return pd.DataFrame()
    if not isinstance(items, list) or not items:
        return pd.DataFrame()
    parsed = []
    for row in items:
        bd = str(row.get("bizdate") or "").strip()
        if len(bd) != 8 or not bd.isdigit():
            continue
        fn = _parse_kr_int(row.get("foreignerPureBuyQuant"))
        ins = _parse_kr_int(row.get("organPureBuyQuant"))
        if fn is None or ins is None:
            continue
        parsed.append({
            "date": f"{bd[:4]}-{bd[4:6]}-{bd[6:]}",
            "foreign_net": fn,
            "inst_net": ins,
        })
    if not parsed:
        return pd.DataFrame()
    return pd.DataFrame(parsed).drop_duplicates("date").sort_values("date")


def _naver_flows(ticker: str, pages: int = 3) -> pd.DataFrame:
    """Foreign/institutional net buying (shares) from Naver Finance — keyless and
    reliable (mobile JSON primary with frgn.naver HTML fallback). Returns
    columns: date, foreign_net, inst_net."""
    import io
    import time

    import httpx

    mobile = _naver_mobile_flows(ticker, pages=pages)
    if not mobile.empty:
        return mobile

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": f"https://finance.naver.com/item/frgn.naver?code={ticker}",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    rows = []
    with httpx.Client(timeout=settings.request_timeout, headers=headers) as client:
        for page in range(1, pages + 1):
            tables = None
            for attempt in range(2):
                try:
                    r = client.get("https://finance.naver.com/item/frgn.naver",
                                   params={"code": ticker, "page": page})
                    r.raise_for_status()
                    r.encoding = "euc-kr"
                    tables = pd.read_html(io.StringIO(r.text))
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 0:
                        time.sleep(0.3)
                        continue
                    logger.debug("naver flows {} p{} failed: {}", ticker, page, exc)
            if not tables:
                continue
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


def _normalize_pykrx_flows(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize pykrx trading-volume DataFrame into [date, foreign_net, inst_net]."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.reset_index()
    date_col = next((c for c in df.columns if "날짜" in str(c) or str(c).lower() == "date"),
                    df.columns[0])
    fcol = _find(df, "외국인")
    icol = _find(df, "기관")
    if not (fcol and icol):
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col]).dt.date.astype(str),
        "foreign_net": pd.to_numeric(df[fcol], errors="coerce"),
        "inst_net": pd.to_numeric(df[icol], errors="coerce"),
    })
    return out.dropna().drop_duplicates("date").sort_values("date")


def investor_flows(ticker: str, start: str | None = None, end: str | None = None,
                   pages: int = 3) -> pd.DataFrame:
    """Foreign + institutional net buying (shares) per date. PRIMARY = Naver
    (keyless JSON/HTML); FALLBACK = pykrx (KRX account). Optional [start,end]."""
    import datetime as dt

    df = _naver_flows(ticker, pages=pages)
    if df.empty:   # fallback to pykrx MDC if a KRX account is configured
        stock = _stock()
        if stock is not None and bridge_credentials():
            today = dt.date.today()
            e_dt = (end or today.isoformat()).replace("-", "")
            s_dt = (start or (today - dt.timedelta(days=pages * 35)).isoformat()).replace("-", "")
            try:
                raw = stock.get_market_trading_volume_by_date(s_dt, e_dt, ticker)
                df = _normalize_pykrx_flows(raw)
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
