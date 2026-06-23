"""Daily ingestion pipeline.

Two-stage funnel (the core accuracy lever):
  Stage 1 — cheap OHLCV pass over the eligible universe → features for all.
  Stage 2 — expensive per-symbol structural/options capture only for a SHORTLIST
            (already-moving or pre-ignition setups), respecting free rate limits.

Durability: each batch is processed and committed independently, so a mid-run
failure (e.g. yfinance throttling) never discards the data already gathered.
Run start/finish are logged in their own transactions for observability.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
from loguru import logger

from . import scoring
from .config import settings
from .db import connect, ensure_securities, finish_run, start_run, upsert, utc_now
from .features import compute_features, recent_run_pct
from .sources import market, sec, universe


def update_universe() -> int:
    """Refresh securities master from NASDAQ Trader. Survivorship-safe: existing
    rows keep their first_seen; we never delete."""
    rows = universe.fetch_symbol_master()
    with connect() as conn:
        run_id = start_run(conn, "universe", date.today().isoformat())
        n = upsert(conn, "securities", rows)
        finish_run(conn, run_id, n_symbols=len(rows), n_written=n)
    logger.info("universe updated: {} securities", n)
    return n


def _eligible_symbols(conn, *, price_filter: bool = False) -> list[str]:
    """Stage-1 static filter: tradable common stock (ETFs excluded).

    When `price_filter` is on, drop names whose most recent known close is far
    above max_price — a +100% surge candidate is overwhelmingly low-priced, so
    this shrinks the heavy OHLCV pass on subsequent runs. Symbols we've never
    snapshotted always pass (we don't yet know their price)."""
    rows = conn.execute(
        "SELECT symbol FROM securities WHERE etf = 0 AND delisted = 0"
    ).fetchall()
    syms = [r["symbol"] for r in rows]
    if not price_filter:
        return syms

    price_rows = conn.execute(
        "SELECT symbol, close FROM daily_snapshot "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM daily_snapshot)"
    ).fetchall()
    if not price_rows:
        return syms  # no history yet → keep full universe
    last_close = {r["symbol"]: r["close"] for r in price_rows}
    cutoff = settings.max_price * settings.stage1_price_multiplier
    kept = [
        s
        for s in syms
        if s not in last_close
        or last_close[s] is None
        or last_close[s] <= cutoff
    ]
    logger.info("stage-1 price filter: {} → {} symbols (cutoff ${:.0f})",
                len(syms), len(kept), cutoff)
    return kept


def _is_shortlist(feat: dict) -> bool:
    """Stage-2 gate: only spend expensive API calls on pre-ignition / moving names."""
    pct = feat.get("pct_change") or 0
    rvol = feat.get("rvol") or 0
    gap = feat.get("gap_pct") or 0
    return (
        pct >= settings.near_surge_pct
        or (rvol >= 3 and pct >= 5)
        or abs(gap) >= 10
        or (feat.get("consec_up_days") or 0) >= 4
    )


def _process_batch(chunk: list[str], period: str, asof: str | None = None) -> dict:
    """Download + feature one batch and capture Stage-2 data for its shortlist.
    `asof` (ISO date) drops bars after that day — point-in-time replay with no
    look-ahead. Returns a dict of row-lists for atomic persistence by the caller."""
    ohlcv = market.download_ohlcv(chunk, period=period)
    out = {
        "snap_rows": [],
        "trap_rows": [],
        "surge_rows": [],
        "candidate_rows": [],
        "catalyst_rows": [],
        "shortlist_n": 0,
    }
    if ohlcv.empty:
        return out

    trap_by_sym: dict[str, dict] = {}
    shortlist: list[dict] = []

    for sym, hist in ohlcv.groupby("symbol"):
        if asof:
            hist = hist[pd.to_datetime(hist["date"]).dt.date.astype(str) <= asof]
        feat = compute_features(hist)
        if feat is None:
            continue
        snap_date = str(pd.to_datetime(hist["date"].max()).date())
        row = {
            "symbol": sym,
            "snapshot_date": snap_date,
            **{k: feat.get(k) for k in _SNAP_FEATURE_KEYS},
            "source": "yfinance",
            "captured_at": utc_now(),
        }
        out["snap_rows"].append(row)

        if (feat.get("pct_change") or 0) >= settings.surge_threshold_pct:
            out["surge_rows"].append(
                {
                    "symbol": sym,
                    "event_date": snap_date,
                    "prev_date": _prev_date(hist),
                    "surge_pct": feat["pct_change"],
                    "intraday_high_pct": _intraday_high_pct(feat),
                    "label_type": "close_to_close",
                    "sustained": None,
                    "captured_at": utc_now(),
                }
            )

        run = recent_run_pct(hist, settings.exhausted_lookback_days)
        trap_by_sym[sym] = {
            "symbol": sym,
            "snapshot_date": snap_date,
            "pending_offering": 0,
            "exhausted": 1 if (run or 0) >= settings.exhausted_run_pct else 0,
            "illiquid": 1
            if (feat.get("dollar_volume") or 0) < settings.min_dollar_volume
            else 0,
            "recent_rsplit": 0,
            "notes": None,
        }

        if settings.min_price <= feat["close"] <= settings.max_price and _is_shortlist(
            feat
        ):
            shortlist.append(row)

    out["shortlist_n"] = len(shortlist)

    # Stage-2 — expensive enrichment + scoring, only for the shortlist
    cik_map = sec.load_cik_map() if shortlist else {}
    for row in shortlist:
        sym = row["symbol"]
        row.update(market.fetch_structural(sym))
        row.update(market.fetch_options(sym))
        fl = row.get("shares_float")
        if fl:
            row["float_rotation"] = (row.get("volume") or 0) / fl

        corp = market.fetch_corporate(sym)
        sec_info = sec.assess_symbol(sym, cik_map=cik_map)
        trap = trap_by_sym[sym]
        trap["recent_rsplit"] = corp.get("recent_rsplit", 0)
        trap["pending_offering"] = sec_info.get("pending_offering", 0)

        for cdate, ctype, detail in corp.get("catalysts", []) + sec_info.get(
            "catalysts", []
        ):
            out["catalyst_rows"].append(
                {
                    "symbol": sym,
                    "event_date": cdate,
                    "event_type": ctype,
                    "detail": detail,
                    "source": "yfinance" if ctype != "offering" else "sec",
                }
            )

        score, reasons = scoring.setup_score(row, trap)
        if score >= settings.min_candidate_score:
            out["candidate_rows"].append(
                {
                    "symbol": sym,
                    "snapshot_date": row["snapshot_date"],
                    "score": score,
                    "reasons": json.dumps(reasons, ensure_ascii=False),
                    "pct_change": row.get("pct_change"),
                    "close": row.get("close"),
                    "shares_float": row.get("shares_float"),
                    "short_pct_float": row.get("short_pct_float"),
                    "rvol": row.get("rvol"),
                    "captured_at": utc_now(),
                }
            )

    out["trap_rows"] = list(trap_by_sym.values())
    return out


def run_snapshot(
    symbols: list[str] | None = None,
    period: str = "60d",
    *,
    limit: int | None = None,
    fast: bool = False,
    asof: str | None = None,
) -> dict:
    """Main daily job with per-batch incremental commits. `asof` replays a past
    day with no look-ahead. Returns a summary."""
    today = date.today().isoformat()

    with connect() as conn:
        universe_syms = symbols or _eligible_symbols(conn, price_filter=fast)
        if limit:
            universe_syms = universe_syms[:limit]
        run_id = start_run(conn, "snapshot", today)  # durable run-start

    if not universe_syms:
        logger.warning("empty universe — run `surge universe` first")
        with connect() as conn:
            finish_run(conn, run_id, status="error", error="empty universe")
        return {"error": "empty universe"}

    totals = {
        "snapshots": 0, "shortlist": 0, "surges": 0,
        "candidates": 0, "batches_failed": 0,
    }
    batches = list(_chunks(universe_syms, settings.batch_size))
    n_batches = len(batches)

    for i, chunk in enumerate(batches, 1):
        try:
            res = _process_batch(chunk, period, asof=asof)
        except Exception as exc:  # noqa: BLE001  (one bad batch must not kill the run)
            logger.warning("batch {}/{} failed: {}", i, n_batches, exc)
            totals["batches_failed"] += 1
            continue

        snap_rows = res["snap_rows"]
        if snap_rows:
            with connect() as conn:  # atomic per-batch commit
                ensure_securities(conn, {r["symbol"] for r in snap_rows})
                upsert(conn, "daily_snapshot", snap_rows)
                if res["trap_rows"]:
                    upsert(conn, "trap_flags", res["trap_rows"])
                if res["surge_rows"]:
                    upsert(conn, "surge_events", res["surge_rows"])
                if res["candidate_rows"]:
                    upsert(conn, "candidates", res["candidate_rows"])
                if res["catalyst_rows"]:
                    upsert(conn, "catalysts", res["catalyst_rows"])

        totals["snapshots"] += len(snap_rows)
        totals["shortlist"] += res["shortlist_n"]
        totals["surges"] += len(res["surge_rows"])
        totals["candidates"] += len(res["candidate_rows"])
        if i % 10 == 0 or i == n_batches:
            logger.info(
                "snapshot progress {}/{} batches · {} snapshots · {} surges · {} cands",
                i, n_batches, totals["snapshots"], totals["surges"], totals["candidates"],
            )

    status = "ok" if totals["batches_failed"] < n_batches else "error"
    with connect() as conn:
        finish_run(
            conn, run_id,
            n_symbols=len(universe_syms),
            n_written=totals["snapshots"],
            status=status,
        )

    summary = {"date": today, "universe": len(universe_syms), **totals}
    logger.info("snapshot done: {}", summary)
    return summary


def update_sustained(window: str = "3mo") -> int:
    """Fade-model labeling: for archived surges with unknown outcome, decide
    whether the move HELD the next trading day (sustained=1) or collapsed (0).
    This seeds the higher-accuracy 'predict the fade' model."""
    cutoff = (date.today() - timedelta(days=1)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol, event_date FROM surge_events "
            "WHERE sustained IS NULL AND event_date <= ?",
            (cutoff,),
        ).fetchall()
    pending = [(r["symbol"], r["event_date"]) for r in rows]
    if not pending:
        logger.info("no surge events pending fade-labeling")
        return 0

    updated = 0
    for sym, ev in pending:
        try:
            hist = market.download_ohlcv([sym], period=window)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fade fetch failed {}: {}", sym, exc)
            continue
        if hist.empty:
            continue
        hist = hist.copy()
        hist["d"] = pd.to_datetime(hist["date"]).dt.date.astype(str)
        ev_row = hist[hist["d"] == ev]
        after = hist[hist["d"] > ev].sort_values("d")
        if ev_row.empty or after.empty:
            continue
        ev_close = float(ev_row.iloc[0]["close"])
        next_close = float(after.iloc[0]["close"])
        sustained = 1 if ev_close and next_close >= ev_close * 0.9 else 0
        with connect() as conn:
            conn.execute(
                "UPDATE surge_events SET sustained=? WHERE symbol=? AND event_date=?",
                (sustained, sym, ev),
            )
        updated += 1
    logger.info("fade-labeling: {} surge events updated", updated)
    return updated


_SNAP_FEATURE_KEYS = (
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "dollar_volume",
    "pct_change",
    "gap_pct",
    "rvol",
    "close_strength",
    "range_pct",
    "dist_52w_low",
    "consec_up_days",
)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _prev_date(hist: pd.DataFrame) -> str | None:
    d = hist.sort_values("date")
    if len(d) < 2:
        return None
    return str(pd.to_datetime(d.iloc[-2]["date"]).date())


def _intraday_high_pct(feat: dict) -> float | None:
    pc = feat.get("prev_close")
    hi = feat.get("high")
    return ((hi / pc - 1.0) * 100) if pc and hi else None
