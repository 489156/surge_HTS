"""Rotation engine tests — KRX adapter mocked, deterministic."""

import pandas as pd
import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.rotation import chains, engine


def test_chain_index_and_universe():
    idx = chains.ticker_index()
    assert idx["042700"]["name"] == "한미반도체"
    assert idx["095340"]["chain"] == "ai_memory_hbm"
    assert idx["095340"]["order"] > idx["042700"]["order"]   # test behind packaging
    assert "047810" in chains.universe()                     # KAI present


def test_chain_position_excludes_hot_and_rewards_back_node():
    idx = chains.ticker_index()
    # foundry(order0) is hot (strong mom); packaging(order1) is the back-1 node
    feats = {
        "000660": {"momentum": 0.20, "rvol": 5.0},   # foundry order0 — hot
        "042700": {"momentum": 0.02, "rvol": 2.0},   # packaging order1 — back-1
        "240810": {"momentum": 0.01, "rvol": 1.5},   # equipment order2 — back-2
        "095340": {"momentum": 0.01, "rvol": 1.5},   # test order3 — back-3
    }
    pos = engine._chain_position(idx, feats)
    assert pos["000660"]["chain_pos"] == 0.0          # don't buy the hottest
    assert pos["042700"]["chain_pos"] == 1.0          # back-1 favored
    assert pos["240810"]["chain_pos"] == 0.7          # back-2
    assert pos["095340"]["chain_pos"] == 0.3          # back-3 (further out)


def _fake_px(close, vol, n=40):
    import datetime as dt
    days = [(dt.date(2026, 5, 1) + dt.timedelta(days=i)).isoformat() for i in range(n)]
    return pd.DataFrame({"date": days, "open": close, "high": [c * 1.01 for c in close],
                         "low": close, "close": close, "volume": vol})


def test_screen_gate_and_ranking(monkeypatch):
    # back-1 node with strong flows + volume should pass; hot node should not
    import numpy as np
    base = list(np.linspace(100, 101, 40))
    def fake_ohlcv(t, s, e):
        if t == "000660":   # hot node: big momentum
            return _fake_px(list(np.linspace(100, 140, 40)), [1e6] * 39 + [3e6])
        if t == "042700":   # back-1: calm price, volume spike
            return _fake_px(base, [1e6] * 39 + [4e6])
        return _fake_px(base, [1e6] * 40)
    monkeypatch.setattr(engine.krx, "ohlcv", fake_ohlcv)
    flows = pd.DataFrame({"foreign_net": [1e5] * 20, "inst_net": [1e5] * 20})
    big = pd.DataFrame({"foreign_net": [1e4] * 19 + [5e6], "inst_net": [1e4] * 19 + [5e6]})
    monkeypatch.setattr(engine.krx, "investor_flows",
                        lambda t, **k: big if t == "042700" else flows)
    cands = engine.screen(["000660", "042700", "095340"])
    by = {c["ticker"]: c for c in cands}
    assert by["000660"]["passed"] is False            # hottest excluded
    assert by["042700"]["chain_pos"] == 1.0           # back-1 node


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "r.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def test_run_store_and_eval(db, monkeypatch):
    import numpy as np
    def fake_ohlcv(t, s, e):
        # decision window flat, then +12% by T+5 for the eval fetch
        return _fake_px(list(np.linspace(100, 112, 40)), [1e6] * 40)
    monkeypatch.setattr(engine.krx, "ohlcv", fake_ohlcv)
    monkeypatch.setattr(engine.krx, "investor_flows",
                        lambda t, **k: pd.DataFrame(
                            {"foreign_net": [1e5] * 20, "inst_net": [1e5] * 20}))
    cands = engine.run_and_store(["042700"])
    assert cands
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) n FROM rotation_decisions").fetchone()["n"]
    assert n == 1
    # eval: the stored decision_date is today, so nothing finalizes yet (no T+5)
    assert engine.evaluate() == 0


def test_next_kr_session_skips_weekend():
    import datetime as dt
    assert engine.next_kr_session(dt.date(2026, 6, 13)) == "2026-06-15"  # Sat→Mon
    assert engine.next_kr_session(dt.date(2026, 6, 15)) == "2026-06-16"  # Mon→Tue


def test_evaluate_uses_entry_open_not_decision_close(db, monkeypatch):
    from surge.db import upsert as db_upsert
    with connect(db) as conn:
        db_upsert(conn, "rotation_decisions", [{
            "decision_date": "2026-06-01", "ticker": "X", "ref_close": 90.0,
            "captured_at": "x"}])
    dates = [f"2026-06-0{i}" for i in range(1, 7)]   # 6 sessions from the target
    df = pd.DataFrame({"date": dates, "open": [100] * 6,
                       "high": [101, 104, 120, 110, 113, 116],   # day3 high → hit
                       "low": [99] * 6, "close": [100, 103, 106, 109, 112, 115],
                       "volume": [1] * 6})
    monkeypatch.setattr(engine.krx, "ohlcv", lambda t, s, e: df)
    assert engine.evaluate() == 1
    with connect(db) as conn:
        r = dict(conn.execute(
            "SELECT ret_t1, ret_t5, hit_t5 FROM rotation_decisions").fetchone())
    assert round(r["ret_t1"], 3) == 0.0      # vs entry OPEN 100 (not ref_close 90)
    assert round(r["ret_t5"], 3) == 0.12     # close[4]=112 / 100 − 1
    assert r["hit_t5"] == 1                   # max high 120 ≥ entry×1.10


def test_variant_score_subsets():
    comps = {"smart_money": 0.8, "chain_pos": 1.0, "rvol": 0.5, "momentum": -0.5}
    champ = engine._variant_score(comps, {})
    # chain_only keeps only chain_pos → equals its normalized value 1.0
    assert engine._variant_score(comps, engine.VARIANTS["chain_only"]) == 1.0
    # no_momentum drops the negative momentum → score rises vs champion
    assert engine._variant_score(comps, {"momentum": 0.0}) > champ


def test_variant_leaderboard_ranks_by_forward_t5(db):
    import json as _json
    from surge.db import upsert as db_upsert
    # 2 days; on each, the high-chain_pos name wins T+5 → chain_only should lead
    rows = []
    for i, d in enumerate(["2026-06-01", "2026-06-02"]):
        rows += [
            {"decision_date": d, "ticker": "WIN", "ref_close": 1.0,
             "components": _json.dumps({"smart_money": 0.1, "chain_pos": 1.0,
                                        "rvol": 0.1, "momentum": 0.0}),
             "ret_t5": 0.15, "hit_t5": 1, "evaluated_at": "x", "captured_at": "x"},
            {"decision_date": d, "ticker": "LOSE", "ref_close": 1.0,
             "components": _json.dumps({"smart_money": 0.9, "chain_pos": 0.0,
                                        "rvol": 0.9, "momentum": 0.5}),
             "ret_t5": -0.05, "hit_t5": 0, "evaluated_at": "x", "captured_at": "x"},
        ]
    with connect(db) as conn:
        db_upsert(conn, "rotation_decisions", rows)
    lb = engine.variant_leaderboard(top_k=1)
    ranked = dict(lb["ranked"])
    assert ranked["chain_only"]["mean_t5"] == pytest.approx(0.15)  # picks WIN
    assert ranked["flow_heavy"]["mean_t5"] == pytest.approx(-0.05)  # picks LOSE


def test_smart_money_none_renormalizes_and_ranked_gate(monkeypatch):
    # 1. Missing smart_money (None) is excluded from denominator, never 0.0-penalized
    s_none = engine._variant_score(
        {"smart_money": None, "chain_pos": 1.0, "rvol": 0.5, "momentum": 0.5}, {}
    )
    s_present = engine._variant_score(
        {"chain_pos": 1.0, "rvol": 0.5, "momentum": 0.5}, {}
    )
    assert s_none == pytest.approx(s_present)

    # 2. Screen handles empty flow data cleanly and applies ranked gate cleanly
    dates = pd.date_range("2026-05-01", periods=25, freq="B").strftime("%Y-%m-%d").tolist()
    vols = [100_000] * 24 + [300_000]  # rvol = 3.0 on last bar

    def fake_ohlcv(ticker, start, end):
        # 1 lead stock (hot) vs 5 back-node stocks
        closes = [100.0] * 19 + [100, 102, 105, 110, 118, 120] if ticker == "HOT1" else (
            [100.0] * 24 + ([112.0] if ticker == "BACK1" else [101.0])
        )
        return pd.DataFrame({
            "date": dates, "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": vols,
        })

    monkeypatch.setattr(engine.krx, "ohlcv", fake_ohlcv)
    monkeypatch.setattr(engine.krx, "investor_flows", lambda *a, **k: pd.DataFrame())
    fake_idx = {
        "HOT1": {"chain": "hbm", "order": 1, "node": "lead", "name": "Hot"},
        "BACK1": {"chain": "hbm", "order": 2, "node": "back", "name": "Back1"},
        "BACK2": {"chain": "hbm", "order": 2, "node": "back", "name": "Back2"},
        "BACK3": {"chain": "hbm", "order": 2, "node": "back", "name": "Back3"},
        "BACK4": {"chain": "hbm", "order": 2, "node": "back", "name": "Back4"},
        "BACK5": {"chain": "hbm", "order": 3, "node": "deep", "name": "Back5"},
    }
    monkeypatch.setattr(engine.chains, "ticker_index", lambda: fake_idx)
    cands = engine.screen(universe=list(fake_idx))
    by_t = {c["ticker"]: c for c in cands}
    assert by_t["BACK1"]["smart_money"] is None
    assert by_t["BACK1"]["passed"] is True
    assert by_t["HOT1"]["passed"] is False
