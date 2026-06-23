from surge.db import connect, init_db, upsert


def test_init_and_upsert(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    with connect(db) as conn:
        n = upsert(
            conn,
            "securities",
            [
                {
                    "symbol": "ABCD",
                    "name": "Test",
                    "exchange": "NASDAQ",
                    "market": "US",
                    "etf": 0,
                    "first_seen": "2026-01-01",
                    "last_seen": "2026-01-01",
                    "delisted": 0,
                }
            ],
        )
        assert n == 1
    # upsert again with new last_seen → no duplicate, value updated
    with connect(db) as conn:
        upsert(
            conn,
            "securities",
            [
                {
                    "symbol": "ABCD",
                    "name": "Test",
                    "exchange": "NASDAQ",
                    "market": "US",
                    "etf": 0,
                    "first_seen": "2026-01-01",
                    "last_seen": "2026-02-02",
                    "delisted": 0,
                }
            ],
        )
        rows = conn.execute("SELECT * FROM securities").fetchall()
        assert len(rows) == 1
        assert rows[0]["last_seen"] == "2026-02-02"


def test_snapshot_pk_conflict(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    row = {
        "symbol": "ABCD",
        "snapshot_date": "2026-01-02",
        "close": 2.0,
        "pct_change": 100.0,
        "captured_at": "2026-01-02T00:00:00",
    }
    with connect(db) as conn:
        upsert(conn, "securities", [{
            "symbol": "ABCD", "name": "T", "exchange": "NASDAQ", "market": "US",
            "etf": 0, "first_seen": "2026-01-01", "last_seen": "2026-01-01",
            "delisted": 0,
        }])
        upsert(conn, "daily_snapshot", [row])
        upsert(conn, "daily_snapshot", [{**row, "close": 2.5}])
        rows = conn.execute("SELECT close FROM daily_snapshot").fetchall()
        assert len(rows) == 1
        assert rows[0]["close"] == 2.5
