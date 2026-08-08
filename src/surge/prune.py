"""Archive prune — trim the no-edge surge-screener tables to cap DB growth.

The SQLite archive crossed GitHub's 100MB push limit (2026-08-05) and froze the
pipeline. gzip bought runway, but the real bloat is the +100% surge screener's
wide daily tables (daily_snapshot + trap_flags ≈ 4,800 rows/session), and that
strategy has NO validated edge (verdict ⛔). So we retain only a recent window
of screener data and VACUUM the freed pages back.

Deliberately narrow: only the five surge-screener tables are trimmed by date.
price_history and every duel/rotation/adaptive/calibration table — the parts
with (or building toward) an edge — are never touched. The immutable-archive
promise holds where it matters; we just stop hoarding a no-edge firehose
forever. keep_days ≤ 0 disables it entirely.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

from .config import settings
from .db import connect

# surge-screener tables → their date column (the ⛔ no-edge strategy's data)
_TABLES = {
    "daily_snapshot": "snapshot_date",
    "trap_flags": "snapshot_date",
    "candidates": "snapshot_date",
    "candidate_outcomes": "snapshot_date",
    "surge_events": "event_date",
}


def prune_surge_screener(keep_days: int | None = None,
                         vacuum: bool = True) -> dict:
    """Delete surge-screener rows older than `keep_days` and reclaim space.
    Returns {cutoff, deleted:{table:n}, total, vacuumed}. Degrade-safe."""
    keep = settings.surge_prune_keep_days if keep_days is None else keep_days
    if keep is None or keep <= 0:
        return {"skipped": True, "reason": "disabled (keep_days<=0)"}
    cutoff = (_dt.date.today() - _dt.timedelta(days=keep)).isoformat()
    deleted: dict[str, int] = {}
    with connect() as conn:
        for table, col in _TABLES.items():
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))  # noqa: S608
                deleted[table] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except Exception:  # noqa: BLE001 — a missing table must not break prune
                deleted[table] = 0
    total = sum(deleted.values())
    vacuumed = False
    # VACUUM only makes sense for the SQLite file backend (reclaims freed pages);
    # it must run outside a transaction, so use a dedicated autocommit connection.
    if vacuum and total and not settings.pg_dsn:
        try:
            raw = sqlite3.connect(str(settings.db_path), isolation_level=None)
            raw.execute("VACUUM")
            raw.close()
            vacuumed = True
        except Exception:  # noqa: BLE001
            vacuumed = False
    return {"cutoff": cutoff, "deleted": deleted, "total": total,
            "vacuumed": vacuumed}
