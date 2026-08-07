"""Honesty benchmarks — long-bias comparison + regime buckets."""
from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert
from surge.duel import benchmark


def _seed(db, rows):
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", rows, immutable=("captured_at",))


def test_long_bias_champion_beats_long(tmp_path, monkeypatch):
    db = tmp_path / "b.db"; init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    # 4 sessions: champion right on all; underlying down on 3 (always-long wrong on 3)
    _seed(db, [
        {"pair": f"p{i}", "decision_date": "2026-07-21", "side": "X",
         "correct": 1, "soxx_oc_ret": (-0.01 if i < 3 else 0.01),
         "evaluated_at": "y", "captured_at": "x"} for i in range(4)])
    r = benchmark.long_bias_comparison()
    assert r["n"] == 4 and r["champion_acc"] == 1.0
    assert r["always_long_acc"] == 0.25          # only 1/4 up days
    assert r["champ_minus_long"] == 0.75


def test_long_bias_empty(tmp_path, monkeypatch):
    db = tmp_path / "e.db"; init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    assert benchmark.long_bias_comparison() == {}
    assert benchmark.summary()["long_bias"] == {}      # degrade-safe


def test_regime_buckets(tmp_path, monkeypatch):
    import json
    db = tmp_path / "r.db"; init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", [
            {"pair": "soxl_soxs", "decision_date": "2026-07-21", "side": "X",
             "correct": 1, "evaluated_at": "y", "captured_at": "x"}],
            immutable=("captured_at",))
        # und_vol20 0.01 → annualized ~15.9% → LOW bucket
        db_upsert(conn, "duel_live_context", [
            {"pair": "soxl_soxs", "decision_date": "2026-07-21",
             "ctx": json.dumps({"und_vol20": 0.01}), "captured_at": "x"}],
            immutable=("captured_at",))
    rg = benchmark.regime_accuracy()
    assert rg["buckets"]["low"]["n"] == 1 and rg["buckets"]["low"]["acc"] == 1.0
    assert rg["buckets"]["high"]["n"] == 0
