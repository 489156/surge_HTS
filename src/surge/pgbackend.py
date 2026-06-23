"""Optional PostgreSQL backend (gated on SURGE_PG_DSN).

SQLite is the default and the fully-tested path. When a DSN is configured, the
data layer transparently switches to Postgres via a thin adapter that mimics the
exact `sqlite3.Connection` surface our code uses (execute / executemany /
executescript / commit / rollback / close, with dict-accessible rows and a
working `lastrowid`). Pure translation helpers below are unit-tested; the live
round-trip is exercised by `docker compose up` (Postgres is provisioned there).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any


# ── pure translation (unit-tested) ──────────────────────────────────────────
def translate_placeholders(sql: str) -> str:
    """SQLite uses '?'; psycopg uses '%s'. Our SQL contains no literal '?'."""
    return sql.replace("?", "%s")


def translate_schema(sqlite_schema: str) -> str:
    """Convert the SQLite schema DDL to Postgres dialect."""
    lines = [
        ln for ln in sqlite_schema.splitlines()
        if not ln.strip().upper().startswith("PRAGMA")
    ]
    s = "\n".join(lines)
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    return s


def split_statements(schema: str) -> list[str]:
    """Split a DDL script into individual statements, dropping comment-only and
    empty fragments. (Our schema has no ';' inside string literals.)"""
    out = []
    for chunk in schema.split(";"):
        code = "\n".join(
            ln for ln in chunk.splitlines() if not ln.strip().startswith("--")
        )
        if code.strip():
            out.append(code.strip())
    return out


# ── adapter ──────────────────────────────────────────────────────────────────
def _require_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "SURGE_PG_DSN is set but psycopg is not installed. "
            "Install the Postgres extra:  uv sync --extra pg"
        ) from exc
    return psycopg, dict_row


class _PGCursor:
    """Wraps a psycopg cursor; adds sqlite-style `lastrowid` via lastval()."""

    def __init__(self, cur, conn):
        self._cur = cur
        self._conn = conn

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        try:
            row = self._conn.execute("SELECT lastval() AS v").fetchone()
            return row["v"] if row else None
        except Exception:  # noqa: BLE001 - no sequence touched yet
            return None


class PGConnection:
    """Duck-types the subset of sqlite3.Connection our code calls."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _PGCursor:
        cur = self.raw.execute(translate_placeholders(sql), tuple(params))
        return _PGCursor(cur, self.raw)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self.raw.cursor() as cur:
            cur.executemany(translate_placeholders(sql), [tuple(r) for r in seq])

    def executescript(self, script: str) -> None:
        for stmt in split_statements(translate_schema(script)):
            self.raw.execute(stmt)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def connect_pg(dsn: str) -> PGConnection:
    psycopg, dict_row = _require_psycopg()
    raw = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    return PGConnection(raw)


def init_pg(dsn: str, sqlite_schema: str) -> None:
    """Create the schema in Postgres (idempotent — CREATE ... IF NOT EXISTS)."""
    psycopg, dict_row = _require_psycopg()
    statements = split_statements(translate_schema(sqlite_schema))
    with psycopg.connect(dsn, autocommit=True) as raw:
        for stmt in statements:
            raw.execute(stmt)
