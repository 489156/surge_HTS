"""Watch module tests — adapters mocked, deterministic. Verifies the watch layer
computes sane levels and NEVER imports/alters the prediction loops."""

import pandas as pd

from surge.config import settings
from surge.db import connect, init_db
from surge.watch import engine
from surge.watch import targets as T


def _df(n=260, start=100.0, step=0.1, atr_amp=0.02):
    closes = [start + step * i for i in range(n)]
    return pd.DataFrame({
        "date": pd.date_range("2025-06-01", periods=n, freq="D").astype(str),
        "open": closes, "high": [c * (1 + atr_amp) for c in closes],
        "low": [c * (1 - atr_amp) for c in closes], "close": closes,
        "volume": [1_000_000] * n})


def test_targets_registry_horizons():
    assert any(t["t"] == "ASTS" for t in T.TARGETS)
    longs = {t["t"] for t in T.by_horizon("long")}
    assert "ASTS" in longs and "OKLO" in longs
    assert "NVDA" not in longs                      # mega-cap not a 10x candidate


def test_short_levels_bracket_ordering(monkeypatch):
    monkeypatch.setattr(engine, "_ohlcv", lambda tg: _df())
    monkeypatch.setattr(engine, "_kr_smart", lambda tg: 0.0)
    lv = engine._levels({"t": "X", "name": "X", "mkt": "us"}, "short")
    assert lv["stop"] < lv["ref"] < lv["target"]    # long bracket
    assert lv["buy_low"] <= lv["ref"]
    assert lv["rr"] > 0
    assert 0 <= lv["score"] <= 100


def test_swing_uses_ma_regime(monkeypatch):
    monkeypatch.setattr(engine, "_ohlcv", lambda tg: _df(step=0.3))  # uptrend
    monkeypatch.setattr(engine, "_kr_smart", lambda tg: 0.0)
    lv = engine._levels({"t": "X", "name": "X", "mkt": "us"}, "swing")
    assert lv["trend"] == "up"                       # ref > ma50 > ma200
    assert lv["target"] >= lv["ref"] + 0  # target above ref


def test_levels_ranks_and_no_crash_on_missing(monkeypatch):
    def fake(tg):
        return _df() if tg["t"] in ("NVDA", "OKLO") else pd.DataFrame()
    monkeypatch.setattr(engine, "_ohlcv", fake)
    monkeypatch.setattr(engine, "_kr_smart", lambda tg: 0.0)
    rows = engine.levels("short")
    assert all(r["ticker"] in ("NVDA", "OKLO") for r in rows)   # empties skipped


def test_multibagger_no_probability_and_room(monkeypatch):
    monkeypatch.setattr(engine, "_ohlcv", lambda tg: _df())
    monkeypatch.setattr(engine, "_us_cap", lambda t: 3e9)
    rows = engine.multibagger()
    assert rows
    r = rows[0]
    assert "tenx" in r and "score" in r
    assert not any("prob" in k.lower() for k in r)   # no probability field
    assert 0 <= r["score"] <= 100


def test_persist_journal(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(engine, "_ohlcv", lambda tg: _df())
    monkeypatch.setattr(engine, "_kr_smart", lambda tg: 0.0)
    engine.levels("short", persist=True)
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) n FROM watch_levels").fetchone()["n"]
    assert n > 0


def test_watch_does_not_touch_prediction_loops():
    import surge.watch.engine as we
    src = open(we.__file__, encoding="utf-8").read()
    # the watch engine must not import duel/rotation prediction or eval
    assert "duel" not in src and "rotation" not in src
