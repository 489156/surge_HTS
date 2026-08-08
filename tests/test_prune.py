"""Archive prune — trims only the no-edge surge-screener tables, by date."""
import datetime as dt

from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert
from surge.prune import prune_surge_screener


def test_prune_trims_old_screener_keeps_recent_and_price_history(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    old = "2020-01-01"
    recent = dt.date.today().isoformat()
    with connect(db) as conn:
        db_upsert(conn, "securities", [{"symbol": "AAA", "name": "x",
            "first_seen": old, "last_seen": recent}], immutable=())
        db_upsert(conn, "daily_snapshot", [
            {"symbol": "AAA", "snapshot_date": old, "captured_at": "x"},
            {"symbol": "AAA", "snapshot_date": recent, "captured_at": "x"}],
            immutable=("captured_at",))
        # price_history must NEVER be pruned (it has an edge / is shared)
        db_upsert(conn, "price_history", [
            {"symbol": "AAA", "date": old, "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 1, "source": "t", "captured_at": "x"}],
            immutable=("captured_at",))
    res = prune_surge_screener(keep_days=30, vacuum=False)
    assert res["deleted"]["daily_snapshot"] == 1        # only the 2020 row
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_snapshot").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1  # untouched


def test_prune_disabled_when_keep_zero(tmp_path, monkeypatch):
    db = tmp_path / "d.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    assert prune_surge_screener(keep_days=0).get("skipped") is True
