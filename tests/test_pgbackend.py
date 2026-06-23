"""Tests for the Postgres backend translation layer (pure, no live PG needed)
and the backend-selection logic. The live PG round-trip is verified via
docker compose, not here."""

import sqlite3

from surge import pgbackend
from surge.config import settings
from surge.db import _schema_sql, connect


def test_translate_placeholders():
    sql = "SELECT * FROM t WHERE a=? AND b=?"
    assert pgbackend.translate_placeholders(sql) == "SELECT * FROM t WHERE a=%s AND b=%s"


def test_translate_schema_idioms():
    s = pgbackend.translate_schema(
        "PRAGMA foreign_keys = ON;\n"
        "CREATE TABLE x (id INTEGER PRIMARY KEY AUTOINCREMENT, v REAL);"
    )
    assert "PRAGMA" not in s
    assert "BIGSERIAL PRIMARY KEY" in s
    assert "DOUBLE PRECISION" in s
    assert "AUTOINCREMENT" not in s


def test_split_statements_drops_comments():
    stmts = pgbackend.split_statements(
        "-- a comment\nCREATE TABLE x(a int);\n-- another\nCREATE INDEX i ON x(a);"
    )
    assert len(stmts) == 2
    assert all(not s.startswith("--") for s in stmts)


def test_full_schema_translates_to_pg():
    pg = pgbackend.translate_schema(_schema_sql())
    stmts = pgbackend.split_statements(pg)
    assert "AUTOINCREMENT" not in pg
    assert "PRAGMA" not in pg
    assert len(stmts) > 10  # all CREATE TABLE/INDEX statements present


def test_pgconnection_translates_and_passes_params():
    calls = []

    class FakeRaw:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            return object()

    conn = pgbackend.PGConnection(FakeRaw())
    conn.execute("INSERT INTO t VALUES (?, ?)", (1, 2))
    assert calls[0][0] == "INSERT INTO t VALUES (%s, %s)"
    assert calls[0][1] == (1, 2)


def test_connect_uses_sqlite_when_no_dsn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pg_dsn", None)
    monkeypatch.setattr(settings, "db_path", tmp_path / "x.db")
    with connect() as c:
        assert isinstance(c, sqlite3.Connection)
