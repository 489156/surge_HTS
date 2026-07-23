"""Data-quality gate — reject bad data BEFORE it enters the immutable archive.

The point-in-time archive is this project's crown jewel; a single corrupt frame
(duplicate dates, a zero/negative print, a NaN gap, an out-of-order series)
silently poisons every downstream feature and label. The engine already blocks
look-ahead by construction (shift(1)); this adds the OTHER half — an automated
integrity check that runs at write time and refuses to persist a frame that
fails a HARD check, and scores every frame so degradation is visible instead of
silent.

Deliberately conservative: only HARD failures (empty / duplicate dates /
non-positive prices / non-monotonic dates) block a write — those are always
corruption. SOFT issues (staleness, a small NaN ratio) lower the score and are
surfaced, but never reject, so a quiet vendor day still accumulates. Pure and
dependency-light: one function over a tidy OHLCV frame, no DB, no network —
fully unit-testable offline.
"""

from __future__ import annotations

import datetime as _dt

import pandas as pd

# score penalties (soft issues subtract; hard failures set ok=False outright)
_STALE_PENALTY = 0.3
_NAN_PENALTY = 0.5
_SHORT_PENALTY = 0.2
_OK_THRESHOLD = 0.7


def assess_frame(df: pd.DataFrame, *, symbol: str | None = None,
                 asof: str | None = None, max_stale_days: int = 7,
                 min_rows: int = 30) -> dict:
    """Integrity verdict for one tidy OHLCV frame (columns: date, open, high,
    low, close, volume). Returns hard-fail flags, a quality `score` ∈ [0,1], an
    `ok` gate (hard-clean AND score ≥ threshold — but a write only needs
    `hard_ok`), and human-readable `reasons`."""
    reasons: list[str] = []
    n = int(len(df))
    if n == 0 or "close" not in df.columns or "date" not in df.columns:
        return {"symbol": symbol, "n": n, "hard_ok": False, "ok": False,
                "score": 0.0, "reasons": ["empty/columns"]}

    dates = pd.to_datetime(df["date"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")

    dup_dates = int(dates.duplicated().sum())
    # monotonic non-decreasing dates (a tidy series should already be sorted)
    monotonic = bool(dates.is_monotonic_increasing)
    n_bad_dates = int(dates.isna().sum())
    nonpos = int((close <= 0).sum())              # zero/negative print = corruption
    nan_ratio = float(close.isna().mean())

    stale_days = None
    if asof and dates.notna().any():
        try:
            last = dates.max().date()
            asof_d = _dt.date.fromisoformat(asof[:10])
            stale_days = (asof_d - last).days
        except (ValueError, TypeError):
            stale_days = None

    # ── HARD failures: always corruption, block the write ──
    hard_ok = True
    if dup_dates:
        hard_ok = False
        reasons.append(f"중복 날짜 {dup_dates}")
    if nonpos:
        hard_ok = False
        reasons.append(f"비양수 종가 {nonpos}")
    if n_bad_dates:
        hard_ok = False
        reasons.append(f"파싱불가 날짜 {n_bad_dates}")
    if not monotonic:
        hard_ok = False
        reasons.append("날짜 비단조")

    # ── SOFT issues: lower the score, never reject ──
    score = 1.0
    if nan_ratio > 0:
        score -= _NAN_PENALTY * min(1.0, nan_ratio * 5)
        reasons.append(f"NaN 비율 {nan_ratio:.1%}")
    if n < min_rows:
        score -= _SHORT_PENALTY
        reasons.append(f"행 부족 {n}<{min_rows}")
    if stale_days is not None and stale_days > max_stale_days:
        score -= _STALE_PENALTY
        reasons.append(f"stale {stale_days}일")
    score = 0.0 if not hard_ok else round(max(0.0, min(1.0, score)), 3)

    return {"symbol": symbol, "n": n, "dup_dates": dup_dates,
            "nonpos": nonpos, "nan_ratio": round(nan_ratio, 4),
            "monotonic": monotonic, "stale_days": stale_days,
            "hard_ok": hard_ok, "ok": hard_ok and score >= _OK_THRESHOLD,
            "score": score, "reasons": reasons}


def archive_integrity(conn) -> dict:
    """Read-only nightly integrity read on the price_history archive — surfaces
    corruption that would otherwise sit undetected. Cheap aggregate queries."""
    nonpos = conn.execute(
        "SELECT COUNT(*) c FROM price_history WHERE close <= 0 OR close IS NULL"
    ).fetchone()["c"]
    symbols = conn.execute(
        "SELECT COUNT(DISTINCT symbol) c FROM price_history").fetchone()["c"]
    # symbols whose freshest bar is > 10 sessions old (~2 weeks) → likely dead feed
    stale = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT symbol, MAX(date) m FROM price_history "
        "GROUP BY symbol) WHERE m < date('now', '-16 day')").fetchone()["c"]
    return {"nonpos_prices": int(nonpos), "symbols": int(symbols),
            "stale_symbols": int(stale), "clean": nonpos == 0}
