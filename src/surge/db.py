"""SQLite access layer. Thin, dependency-free wrapper around the schema."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from loguru import logger

from .config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _schema_sql() -> str:
    return resources.files("surge").joinpath("schema.sql").read_text(encoding="utf-8")


def init_db(db_path: Path | None = None) -> Path | None:
    """Create the database and apply the schema (idempotent). Uses Postgres when
    SURGE_PG_DSN is set, else SQLite at `db_path`."""
    if settings.pg_dsn:
        from . import pgbackend

        pgbackend.init_pg(settings.pg_dsn, _schema_sql())
        logger.info("Postgres schema ready")
        return None
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_sqlite(conn)
        conn.executescript(_schema_sql())
    logger.info("DB ready at {}", path)
    return path


@contextmanager
def connect(db_path: Path | None = None):
    # Postgres backend (gated): transparent to all callers.
    if settings.pg_dsn:
        from . import pgbackend

        conn = pgbackend.connect_pg(settings.pg_dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    # SQLite backend (default).
    path = Path(db_path or settings.db_path)
    if not path.exists():
        init_db(path)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")  # tolerate concurrent writers
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert(conn: sqlite3.Connection, table: str, rows: Sequence[dict[str, Any]],
           immutable: tuple[str, ...] = ()) -> int:
    """INSERT .. ON CONFLICT DO UPDATE for a list of homogeneous dict rows."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(cols)
    # Primary keys differ per table; rely on table's PK for the conflict target.
    # `immutable` columns are written on INSERT but never overwritten on
    # conflict (e.g. captured_at must keep the FIRST capture's timestamp).
    update_cols = [c for c in cols if c not in immutable]
    updates = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    pk = _PRIMARY_KEYS.get(table)
    if pk and updates:
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({pk}) DO UPDATE SET {updates}"
        )
    elif pk:
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({pk}) DO NOTHING"
        )
    else:
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


_PRIMARY_KEYS = {
    "securities": "symbol",
    "daily_snapshot": "symbol, snapshot_date",
    "trap_flags": "symbol, snapshot_date",
    "catalysts": "symbol, event_date, event_type",
    "surge_events": "symbol, event_date, label_type",
    "candidates": "symbol, snapshot_date",
    "candidate_outcomes": "symbol, snapshot_date",
    "duel_decisions": "pair, decision_date",
    "price_history": "symbol, date",
    "duel_variants": "variant, pair, decision_date",
    "duel_factor_shadow": "factor, pair, decision_date",
    "rotation_factor_shadow": "factor, ticker, decision_date",
    "model_state": "key",
    "rotation_decisions": "decision_date, ticker",
    "watch_levels": "asof, ticker, horizon",
    "adaptive_weights": "pair, decision_date, feature",
}


def _migrate_sqlite(conn: sqlite3.Connection) -> None:
    """Idempotent in-place migrations for existing SQLite files (PG starts from
    the current schema, so this is SQLite-only)."""
    def _cols() -> list[str]:
        return [r[1] for r in
                conn.execute("PRAGMA table_info(duel_decisions)").fetchall()]

    # Crash recovery: a kill between the RENAME/recreate and the copy leaves the
    # old data stranded in duel_decisions_v1 while the new table looks migrated.
    # Detect the leftover and resume the copy before anything else.
    v1 = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='duel_decisions_v1'"
    ).fetchone()
    if v1:
        conn.executescript(_schema_sql())          # ensure the new table exists
        v1_cols = [r[1] for r in
                   conn.execute("PRAGMA table_info(duel_decisions_v1)").fetchall()]
        col_list = ", ".join(v1_cols)
        conn.execute(
            f"INSERT INTO duel_decisions ({col_list}, pair) "
            f"SELECT {col_list}, 'soxl_soxs' FROM duel_decisions_v1 "
            f"WHERE true ON CONFLICT(pair, decision_date) DO NOTHING"
        )
        conn.execute("DROP TABLE duel_decisions_v1")
        logger.warning("recovered interrupted duel_decisions migration")

    cols = _cols()
    if cols and "pair" not in cols:
        # duel_decisions: PK(decision_date) → PK(pair, decision_date)
        conn.execute("ALTER TABLE duel_decisions RENAME TO duel_decisions_v1")
        conn.executescript(_schema_sql())          # recreates the new shape
        old_cols = ", ".join(cols)
        conn.execute(
            f"INSERT INTO duel_decisions ({old_cols}, pair) "
            f"SELECT {old_cols}, 'soxl_soxs' FROM duel_decisions_v1"
        )
        conn.execute("DROP TABLE duel_decisions_v1")
        logger.info("migrated duel_decisions to (pair, decision_date)")
        cols = _cols()                              # re-read the real shape
    if cols and "components" not in cols:
        conn.execute("ALTER TABLE duel_decisions ADD COLUMN components TEXT")
        logger.info("migrated duel_decisions: added components column")
    if cols and "gap_guard" not in _cols():
        conn.execute("ALTER TABLE duel_decisions ADD COLUMN gap_guard REAL")
        conn.execute("ALTER TABLE duel_decisions "
                     "ADD COLUMN model TEXT DEFAULT 'champion'")
        logger.info("migrated duel_decisions: added gap_guard/model columns")
    rcols = [r[1] for r in
             conn.execute("PRAGMA table_info(rotation_decisions)").fetchall()]
    if rcols and "components" not in rcols:
        conn.execute("ALTER TABLE rotation_decisions ADD COLUMN components TEXT")
        logger.info("migrated rotation_decisions: added components column")


def ensure_securities(conn: sqlite3.Connection, symbols: Iterable[str]) -> None:
    """Register minimal master rows for symbols we captured, without clobbering
    existing first_seen (survivorship-safe). Uses INSERT OR IGNORE."""
    now = utc_now()
    conn.executemany(
        "INSERT INTO securities "
        "(symbol, name, exchange, market, etf, first_seen, last_seen, delisted) "
        "VALUES (?, NULL, NULL, 'US', 0, ?, ?, 0) "
        "ON CONFLICT(symbol) DO NOTHING",
        [(s, now, now) for s in symbols],
    )


def start_run(conn: sqlite3.Connection, job: str, run_date: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_runs (job, run_date, started_at, status) "
        "VALUES (?, ?, ?, 'running')",
        (job, run_date, utc_now()),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    n_symbols: int = 0,
    n_written: int = 0,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE ingest_runs SET finished_at=?, n_symbols=?, n_written=?, "
        "status=?, error=? WHERE run_id=?",
        (utc_now(), n_symbols, n_written, status, error, run_id),
    )
