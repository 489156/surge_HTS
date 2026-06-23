"""KR rotation shadow FACTORS — does an ATTENTION signal select +10%/T+5 movers?

The AMVF "Lead Attention" hypothesis for KR: a surge in Naver search interest
precedes the move. This module tests it honestly. For each curated rotation
ticker it computes a leak-safe (shifted) search-surge per session from the Naver
DataLab daily series, and scores — STANDALONE, never touching the live screen —
whether sessions where the signal FIRED (search notably above its trailing norm)
hit the rotation target (+10% within T+5) more often than the whole-pool base
rate. A factor is recommended for the screen only if it beats that base rate at a
Šidák-corrected significance (shared learn.gate). DataLab has history, so this is
BACKFILLABLE (instant n) — `surge kr-factors --backfill`.
"""

from __future__ import annotations

import math

import pandas as pd

from ..db import connect, upsert, utc_now

_CONVICTION = 0.3        # |value| below this = no attention call (not scored)
_TARGET = 0.10           # rotation target: +10% within T+5
_FWD = 5


def _surge_values(search: dict[str, float], dates: list[str]) -> dict[str, float]:
    """Leak-safe search-surge per trading date: z-score of search interest vs its
    trailing 20-day norm, SHIFTED one session (row D = D−1 state). tanh-squashed
    to [-1,1] (sign = attention rising vs falling)."""
    if not search or not dates:
        return {}
    s = pd.Series({d: search[d] for d in dates if d in search}, dtype="float64")
    s = s.sort_index()
    if len(s) < 22:
        return {}
    mean = s.rolling(20).mean()
    std = s.rolling(20).std()
    z = ((s - mean) / std).shift(1)        # D row uses info through D−1
    return {d: float(math.tanh(v / 2.0)) for d, v in z.items() if pd.notna(v)}


def _hit_t5(px: pd.DataFrame) -> dict[str, int]:
    """Per trading date: 1 if any of the next 5 sessions' HIGH reaches +10% over
    that date's close (the rotation target). Only dates with 5 forward sessions."""
    px = px.sort_values("date").reset_index(drop=True)
    close = px["close"].astype(float).tolist()
    high = px["high"].astype(float).tolist()
    dates = px["date"].astype(str).tolist()
    out: dict[str, int] = {}
    for i in range(len(close) - _FWD):
        ref = close[i]
        if ref <= 0:
            continue
        fwd_high = max(high[i + 1: i + 1 + _FWD])
        out[dates[i]] = 1 if (fwd_high / ref - 1) >= _TARGET else 0
    return out


def _record(ticker: str, date: str, value: float) -> None:
    with connect() as conn:
        upsert(conn, "rotation_factor_shadow",
               [{"factor": "kr_search_surge", "ticker": ticker,
                 "decision_date": date, "value": round(value, 4),
                 "captured_at": utc_now()}], immutable=("captured_at",))


def score_pending() -> int:
    """Score un-evaluated rows: label = realized hit_t5 (already stored at backfill
    via the label column); correct = the hit ONLY when the factor fired
    (|value| ≥ conviction) — a non-firing session makes no attention call."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT factor, ticker, decision_date, value, label "
            "FROM rotation_factor_shadow WHERE evaluated_at IS NULL "
            "AND label IS NOT NULL").fetchall()]
    now = utc_now()
    for r in rows:
        correct = r["label"] if abs(r["value"]) >= _CONVICTION else None
        with connect() as conn:
            conn.execute(
                "UPDATE rotation_factor_shadow SET correct=?, evaluated_at=? "
                "WHERE factor=? AND ticker=? AND decision_date=?",
                (correct, now, r["factor"], r["ticker"], r["decision_date"]))
    return len(rows)


def backfill(period_days: int = 365) -> int:
    """Replay kr_search_surge over history for every curated rotation ticker:
    DataLab search series + KRX OHLCV → per-session surge (shifted) and the
    realized +10%/T+5 label. One DataLab + one OHLCV call per ticker. Returns
    (ticker, session) rows written."""
    import datetime as dt

    from ..sources import krx
    from . import attention, chains, policy

    end = dt.date.today()
    start = end - dt.timedelta(days=period_days)
    idx = chains.ticker_index()
    written = 0
    for ticker, meta in idx.items():
        name = meta.get("name") or ticker
        px = krx.ohlcv(ticker, start.isoformat(), end.isoformat())
        if px.empty or len(px) < 30:
            continue
        px = px.copy()
        px["date"] = pd.to_datetime(px["date"]).dt.date.astype(str)
        hits = _hit_t5(px)
        now = utc_now()
        rows = []

        # (a) kr_search_surge — Naver DataLab attention
        search = attention.search_series(name, start.isoformat(), end.isoformat())
        surge = _surge_values(search, px["date"].tolist()) if search else {}
        for d, v in surge.items():
            if d in hits:
                rows.append({"factor": "kr_search_surge", "ticker": ticker,
                             "decision_date": d, "value": round(v, 4),
                             "label": int(hits[d]), "captured_at": now})

        # (b) policy_tagged — does a 대미투자특별법 beneficiary hit +10%/T+5 more than
        # the pool SINCE the law emerged (tag_from)? Tests "정책 수혜 = 주가 수혜?".
        pol = policy.beneficiary(ticker)
        if pol:
            for d in hits:
                if d >= pol["tag_from"]:
                    rows.append({"factor": "policy_tagged", "ticker": ticker,
                                 "decision_date": d, "value": 1.0,
                                 "label": int(hits[d]), "captured_at": now})

        if rows:
            with connect() as conn:
                upsert(conn, "rotation_factor_shadow", rows,
                       immutable=("captured_at",))
            written += len(rows)
    score_pending()
    return written


# Factors whose BACKFILL is in-sample-biased and must NOT be trusted from history
# — only forward, out-of-sample data counts. policy_tagged is the case: the
# beneficiary list was curated in hindsight (knowing which stocks already rose),
# so a positive backfill is selection bias, not predictive edge. tag_from blocks
# date look-ahead but not the look-ahead baked into the CHOICE of tickers.
_BIASED = {"policy_tagged":
           "사후 큐레이션(선택편향) — 백필은 인샘플, 전진 OOS만 신뢰"}


def leaderboard() -> dict:
    """Per-factor hit rate WHEN FIRED vs the whole-pool +10%/T+5 base rate, with a
    Šidák-corrected promotion test. Selection-biased factors (see _BIASED) are
    NEVER recommended from backfill — they carry a caveat and await forward OOS."""
    from .. import learn
    from ..config import settings

    with connect() as conn:
        rows = conn.execute(
            "SELECT factor, COUNT(*) n, "
            "SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) wins "
            "FROM rotation_factor_shadow WHERE correct IS NOT NULL GROUP BY factor"
        ).fetchall()
        pool = conn.execute(
            "SELECT AVG(CASE WHEN label=1 THEN 1.0 ELSE 0 END) hr, COUNT(*) n "
            "FROM rotation_factor_shadow WHERE label IS NOT NULL").fetchone()
    base = pool["hr"]
    stats = {r["factor"]: {"n": r["n"], "wins": r["wins"] or 0,
                           "acc": (r["wins"] or 0) / r["n"] if r["n"] else None,
                           "caveat": _BIASED.get(r["factor"])}
             for r in rows}
    ranked = sorted(stats.items(),
                    key=lambda kv: (kv[1]["acc"] or 0, kv[1]["n"]), reverse=True)
    k = max(1, sum(1 for f, s in stats.items()
                   if s["n"] >= settings.variant_min_n and f not in _BIASED))
    zreq = learn.corrected_z(settings.variant_promote_z, k)
    rec = None
    for name, s in ranked:
        if name in _BIASED or s["n"] < settings.variant_min_n or base is None:
            continue                     # biased factors can't be promoted from backfill
        z = learn.one_prop_z(s["wins"], s["n"], base)
        if z >= zreq and (s["acc"] or 0) > base:
            rec = {"factor": name, "acc": s["acc"], "n": s["n"],
                   "z": z, "z_req": zreq, "baseline": base}
            break
    return {"ranked": ranked, "recommend": rec, "baseline": base,
            "pool_n": pool["n"] or 0}
