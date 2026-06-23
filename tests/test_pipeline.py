import pandas as pd
import pytest

from surge import pipeline
from surge.config import settings
from surge.db import connect, ensure_securities, init_db, upsert


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def _seed_securities(path, symbols):
    with connect(path) as conn:
        ensure_securities(conn, symbols)


def test_price_filter_keeps_cheap_and_unknown(db):
    _seed_securities(db, ["CHEAP", "PRICEY", "NEVERSEEN"])
    with connect(db) as conn:
        upsert(conn, "daily_snapshot", [
            {"symbol": "CHEAP", "snapshot_date": "2026-06-01", "close": 3.0,
             "captured_at": "x"},
            {"symbol": "PRICEY", "snapshot_date": "2026-06-01", "close": 500.0,
             "captured_at": "x"},
        ])
    with connect(db) as conn:
        # without filter: all 3
        assert set(pipeline._eligible_symbols(conn)) == {"CHEAP", "PRICEY", "NEVERSEEN"}
        # with filter: PRICEY dropped (500 > 20*3), unknown kept
        kept = set(pipeline._eligible_symbols(conn, price_filter=True))
        assert "CHEAP" in kept and "NEVERSEEN" in kept and "PRICEY" not in kept


def test_shortlist_gate():
    assert pipeline._is_shortlist({"pct_change": 40}) is True       # near-surge
    assert pipeline._is_shortlist({"rvol": 5, "pct_change": 8}) is True
    assert pipeline._is_shortlist({"gap_pct": -15}) is True
    assert pipeline._is_shortlist({"consec_up_days": 5}) is True
    assert pipeline._is_shortlist({"pct_change": 1, "rvol": 1}) is False


def _fake_ohlcv(symbol, dates, closes):
    return pd.DataFrame({
        "symbol": symbol,
        "date": pd.to_datetime(dates),
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1000] * len(closes),
    })


def test_process_batch_labels_surge(db, monkeypatch):
    # ABCD doubles on the last day → surge event
    def fake_download(syms, period="60d"):
        return _fake_ohlcv("ABCD", ["2026-06-01", "2026-06-02"], [1.0, 2.0])
    monkeypatch.setattr(pipeline.market, "download_ohlcv", fake_download)
    monkeypatch.setattr(pipeline.market, "fetch_structural", lambda s: {})
    monkeypatch.setattr(pipeline.market, "fetch_options", lambda s: {"opt_has_chain": 0})
    monkeypatch.setattr(
        pipeline.market, "fetch_corporate",
        lambda s, **k: {"recent_rsplit": 0, "catalysts": []},
    )
    monkeypatch.setattr(pipeline.sec, "load_cik_map", lambda *a, **k: {})
    monkeypatch.setattr(
        pipeline.sec, "assess_symbol",
        lambda s, **k: {"pending_offering": 0, "catalysts": []},
    )

    res = pipeline._process_batch(["ABCD"], "60d")
    assert len(res["snap_rows"]) == 1
    assert len(res["surge_rows"]) == 1
    assert round(res["surge_rows"][0]["surge_pct"]) == 100
    assert res["surge_rows"][0]["prev_date"] == "2026-06-01"
    assert res["shortlist_n"] == 1  # doubled → shortlisted → Stage-2 ran


def test_update_sustained(db, monkeypatch):
    _seed_securities(db, ["FADE", "HOLD"])
    with connect(db) as conn:
        upsert(conn, "surge_events", [
            {"symbol": "FADE", "event_date": "2026-06-01", "prev_date": "2026-05-29",
             "surge_pct": 120, "intraday_high_pct": 150, "label_type": "close_to_close",
             "sustained": None, "captured_at": "x"},
            {"symbol": "HOLD", "event_date": "2026-06-01", "prev_date": "2026-05-29",
             "surge_pct": 110, "intraday_high_pct": 130, "label_type": "close_to_close",
             "sustained": None, "captured_at": "x"},
        ])

    def fake_download(syms, period="3mo"):
        sym = syms[0]
        if sym == "FADE":  # collapses next day
            return _fake_ohlcv("FADE", ["2026-06-01", "2026-06-02"], [10.0, 4.0])
        return _fake_ohlcv("HOLD", ["2026-06-01", "2026-06-02"], [10.0, 11.0])
    monkeypatch.setattr(pipeline.market, "download_ohlcv", fake_download)

    n = pipeline.update_sustained()
    assert n == 2
    with connect(db) as conn:
        res = {r["symbol"]: r["sustained"]
               for r in conn.execute("SELECT symbol, sustained FROM surge_events")}
    assert res["FADE"] == 0
    assert res["HOLD"] == 1
