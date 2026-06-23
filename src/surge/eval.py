"""Predictability measurement — the honest scientific loop.

Takes each past candidate (a prediction made on day T) and records what the
stock ACTUALLY did on day T+1, then reports whether the watchlist beats the
base rate. This is how we answer "is any of this predictable?" with data
instead of hope.

Metrics:
- base rate  = P(a random eligible name surges) — tiny (<0.1%)
- hit rate   = P(candidate makes a meaningful move ≥ near_surge_pct next day)
- surge rate = P(candidate hits the +100% target next day)
- Precision@K = hit rate among the top-K scored candidates each day
- lift       = candidate rate / base rate  (>1 means the score adds signal)

With one day of data these are illustrative, not validated — real validation
needs many days across regimes, plus the 4 anti-traps (survivorship, look-ahead,
liquidity, manipulation). See README.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from .config import settings
from .db import connect, utc_now
from .sources import market


def backfill_outcomes() -> int:
    """For candidates without a recorded outcome whose next trading day is now
    available, fetch the realized move and store it. Returns rows written."""
    with connect() as conn:
        latest_snap = conn.execute(
            "SELECT MAX(snapshot_date) d FROM daily_snapshot"
        ).fetchone()["d"]
        rows = conn.execute(
            "SELECT c.symbol, c.snapshot_date, c.score, c.close "
            "FROM candidates c "
            "LEFT JOIN candidate_outcomes o "
            "  ON c.symbol = o.symbol AND c.snapshot_date = o.snapshot_date "
            "WHERE o.symbol IS NULL AND c.snapshot_date < ?",
            (latest_snap,),
        ).fetchall()

    pending = [(r["symbol"], r["snapshot_date"], r["score"], r["close"]) for r in rows]
    if not pending:
        logger.info("no candidate outcomes to backfill")
        return 0

    written = 0
    for sym, snap_date, score, cand_close in pending:
        try:
            # Fetch FROM the snapshot date, not a fixed look-back: a candidate
            # older than `window` would otherwise resolve its "next day" to the
            # first bar in the window (weeks later) and record a multi-day move
            # as if it were the T+1 outcome — corrupting candidate_outcomes.
            hist = market.download_ohlcv([sym], start=snap_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("outcome fetch failed {}: {}", sym, exc)
            continue
        if hist.empty or not cand_close:
            continue
        hist = hist.copy()
        hist["d"] = pd.to_datetime(hist["date"]).dt.date.astype(str)
        after = hist[hist["d"] > snap_date].sort_values("d")
        if after.empty:
            continue
        nxt = after.iloc[0]
        # Defense in depth: the true next session is within a few calendar days
        # (weekend/holiday). A larger gap means a data hole right after the
        # snapshot — skip rather than mis-record a distant bar as T+1.
        if (pd.to_datetime(nxt["d"]) - pd.to_datetime(snap_date)).days > 6:
            logger.debug("outcome gap too large for {} @ {} → skip", sym, snap_date)
            continue
        next_close = float(nxt["close"])
        next_high = float(nxt["high"])
        next_pct = (next_close / cand_close - 1.0) * 100
        next_high_pct = (next_high / cand_close - 1.0) * 100
        outcome = {
            "symbol": sym,
            "snapshot_date": snap_date,
            "score": score,
            "next_date": str(nxt["d"]),
            "cand_close": cand_close,
            "next_close": next_close,
            "next_pct": next_pct,
            "next_high_pct": next_high_pct,
            "hit": 1 if next_pct >= settings.near_surge_pct else 0,
            "surged100": 1 if next_high_pct >= settings.surge_threshold_pct else 0,
            "captured_at": utc_now(),
        }
        with connect() as conn:
            from .db import upsert

            upsert(conn, "candidate_outcomes", [outcome])
        written += 1
    logger.info("backfilled {} candidate outcomes", written)
    return written


def precision_at_k(k: int = 10) -> dict:
    """Top-K precision per prediction date, averaged. Needs recorded outcomes."""
    with connect() as conn:
        dates = [
            r["snapshot_date"]
            for r in conn.execute(
                "SELECT DISTINCT snapshot_date FROM candidate_outcomes"
            ).fetchall()
        ]
        per_day = []
        for d in dates:
            top = conn.execute(
                "SELECT hit, surged100 FROM candidate_outcomes "
                "WHERE snapshot_date = ? ORDER BY score DESC LIMIT ?",
                (d, k),
            ).fetchall()
            if not top:
                continue
            per_day.append(
                {
                    "date": d,
                    "n": len(top),
                    "hit_rate": sum(r["hit"] or 0 for r in top) / len(top),
                    "surge_rate": sum(r["surged100"] or 0 for r in top) / len(top),
                }
            )
    if not per_day:
        return {"days": 0}
    return {
        "days": len(per_day),
        "k": k,
        "mean_hit_rate": sum(p["hit_rate"] for p in per_day) / len(per_day),
        "mean_surge_rate": sum(p["surge_rate"] for p in per_day) / len(per_day),
        "per_day": per_day,
    }


def summary() -> dict:
    """Overall predictability snapshot: candidate rates vs base rate + lift."""
    with connect() as conn:
        o = conn.execute(
            "SELECT COUNT(*) n, "
            "AVG(hit) hit_rate, AVG(surged100) surge_rate, AVG(next_pct) mean_pct "
            "FROM candidate_outcomes"
        ).fetchone()
        # base rate: surges vs all snapshots that have a known next-day outcome
        snaps = conn.execute("SELECT COUNT(*) n FROM daily_snapshot").fetchone()["n"]
        surges = conn.execute("SELECT COUNT(*) n FROM surge_events").fetchone()["n"]

    n = o["n"] or 0
    base_rate = (surges / snaps) if snaps else 0.0
    hit_rate = o["hit_rate"] or 0.0
    surge_rate = o["surge_rate"] or 0.0
    return {
        "candidates_evaluated": n,
        "base_rate": base_rate,
        "candidate_hit_rate": hit_rate,
        "candidate_surge_rate": surge_rate,
        "mean_next_pct": o["mean_pct"] or 0.0,
        "lift_vs_base": (surge_rate / base_rate) if base_rate else None,
    }
