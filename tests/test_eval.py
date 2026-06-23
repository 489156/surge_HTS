import pandas as pd
import pytest

from surge import eval as evaluation
from surge.config import settings
from surge.db import connect, ensure_securities, init_db, upsert


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def _seed_candidate(path, symbol, snap_date, close, score):
    with connect(path) as conn:
        ensure_securities(conn, [symbol])
        upsert(conn, "daily_snapshot", [
            {"symbol": symbol, "snapshot_date": snap_date, "close": close,
             "captured_at": "x"},
        ])
        upsert(conn, "candidates", [
            {"symbol": symbol, "snapshot_date": snap_date, "score": score,
             "reasons": "[]", "close": close, "captured_at": "x"},
        ])


def test_backfill_and_metrics(db, monkeypatch):
    # WINNER doubles next day; DUD goes nowhere
    _seed_candidate(db, "WINNER", "2026-06-01", 1.0, 9.0)
    _seed_candidate(db, "DUD", "2026-06-01", 5.0, 4.0)
    # a newer snapshot must exist so 2026-06-01 is considered "past"
    with connect(db) as conn:
        ensure_securities(conn, ["ZZZ"])
        upsert(conn, "daily_snapshot", [
            {"symbol": "ZZZ", "snapshot_date": "2026-06-02", "close": 1.0,
             "captured_at": "x"}])

    def fake_download(syms, period="1mo", start=None, end=None):
        sym = syms[0]
        if sym == "WINNER":
            closes = [1.0, 2.2]   # +120% next day
        else:
            closes = [5.0, 5.1]   # +2%
        return pd.DataFrame({
            "symbol": sym,
            "date": pd.to_datetime(["2026-06-01", "2026-06-02"]),
            "open": closes, "high": [c * 1.0 for c in closes],
            "low": closes, "close": closes, "volume": [1000, 1000],
        })
    monkeypatch.setattr(evaluation.market, "download_ohlcv", fake_download)

    n = evaluation.backfill_outcomes()
    assert n == 2

    with connect(db) as conn:
        res = {r["symbol"]: (r["hit"], r["surged100"])
               for r in conn.execute("SELECT symbol, hit, surged100 FROM candidate_outcomes")}
    assert res["WINNER"] == (1, 1)   # ≥30% and ≥100%
    assert res["DUD"] == (0, 0)

    s = evaluation.summary()
    assert s["candidates_evaluated"] == 2
    assert s["candidate_hit_rate"] == 0.5
    assert s["candidate_surge_rate"] == 0.5

    pk = evaluation.precision_at_k(k=1)  # top-1 by score = WINNER
    assert pk["mean_hit_rate"] == 1.0
    assert pk["mean_surge_rate"] == 1.0


def test_empty_summary(db):
    s = evaluation.summary()
    assert s["candidates_evaluated"] == 0
