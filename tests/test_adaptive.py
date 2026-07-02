"""Adaptive walk-forward engine + gap guard tests — deterministic, offline."""

import numpy as np
import pandas as pd
import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.duel import adaptive
from surge.duel import backtest as dbt
from surge.duel import data as duel_data
from surge.duel import live as dlive
from surge.duel.decide import decide, decide_adaptive, guard_triggered

PAIR = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS",
        "underlying": "SOXX"}


# ── ridge + Platt ────────────────────────────────────────────────────────────
def test_fit_learns_a_real_relationship():
    rng = np.random.default_rng(7)
    n = 300
    X = rng.normal(size=(n, len(adaptive.FEATURES))).clip(-1, 1).tolist()
    labels = [0.01 * x[0] + 0.001 * rng.normal() for x in X]  # feature 0 drives
    model = adaptive.fit(X, labels, min_train=120)
    assert model is not None
    # learned weight on the driving feature dominates the noise features
    w = model.weights
    assert abs(w[adaptive.FEATURES[0]]) > 3 * np.mean(
        [abs(w[f]) for f in adaptive.FEATURES[1:]])
    # calibrated probability is monotone in the driving feature
    lo = model.prob_up([-1.0] + [0.0] * (len(adaptive.FEATURES) - 1))
    hi = model.prob_up([+1.0] + [0.0] * (len(adaptive.FEATURES) - 1))
    assert lo < 0.5 < hi


def test_fit_requires_min_train():
    X = [[0.0] * len(adaptive.FEATURES)] * 50
    assert adaptive.fit(X, [0.01] * 50, min_train=120) is None


def test_walk_forward_is_out_of_sample():
    rng = np.random.default_rng(11)
    n = 400
    # pure noise: any accuracy meaningfully above chance would indicate leakage
    X = rng.normal(size=(n, len(adaptive.FEATURES))).clip(-1, 1).tolist()
    labels = rng.normal(size=n).tolist()
    probs = adaptive.walk_forward_probs(X, labels, min_train=120)
    assert all(p is None for p in probs[:120])           # warmup honored
    scored = [(p, lab) for p, lab in zip(probs[120:], labels[120:], strict=False)
              if p is not None]
    acc = np.mean([(p > 0.5) == (lab > 0) for p, lab in scored])
    assert 0.38 <= acc <= 0.62                            # chance, not memorized


def test_walk_forward_learns_predictive_feature():
    rng = np.random.default_rng(3)
    n = 400
    X = rng.normal(size=(n, len(adaptive.FEATURES))).clip(-1, 1).tolist()
    labels = [0.01 * x[2] for x in X]                     # deterministic driver
    probs = adaptive.walk_forward_probs(X, labels, min_train=120)
    scored = [(p, lab) for p, lab in zip(probs, labels, strict=False)
              if p is not None]
    acc = np.mean([(p > 0.5) == (lab > 0) for p, lab in scored])
    assert acc > 0.9


def test_feature_vector_order_and_absent_reads():
    ctx = {"und_vol20": 0.02, "und_oc_mom5": None, "und_oc1": 0.01,
           "und_gap1": -0.01}
    comps = [{"name": "trend", "value": 0.5, "weight": 0.15}]
    v = adaptive.feature_vector(ctx, comps)
    assert len(v) == len(adaptive.FEATURES)
    assert v[adaptive.FEATURES.index("trend")] == 0.5
    assert v[adaptive.FEATURES.index("asia_lead")] == 0.0   # absent → neutral
    assert v[adaptive.FEATURES.index("oc_mom5")] == 0.0     # None → neutral
    assert v[adaptive.FEATURES.index("oc1")] > 0
    assert v[adaptive.FEATURES.index("gap1")] < 0


# ── intraday-aware features are leak-safe ────────────────────────────────────
def test_prepare_intraday_features_are_shifted():
    n = 60
    dates = pd.bdate_range("2026-01-01", periods=n).date.astype(str)
    opens = [100.0 + i for i in range(n)]
    closes = [o * (1.01 if i % 2 == 0 else 0.99) for i, o in enumerate(opens)]
    df = pd.DataFrame({"date": dates, "open": opens, "close": closes,
                       "high": [max(o, c) for o, c in zip(opens, closes)],
                       "low": [min(o, c) for o, c in zip(opens, closes)],
                       "volume": [1] * n})
    prep = duel_data.prepare({"SOXX": df}, PAIR)
    s = prep["SOXX"]
    d = s.index[30]
    prev = s.index[29]
    # f_oc1 at D equals the PRIOR session's open→close — never today's
    assert s.loc[d, "f_oc1"] == pytest.approx(
        s.loc[prev, "close"] / s.loc[prev, "open"] - 1)
    assert s.loc[d, "f_gap1"] == pytest.approx(
        s.loc[prev, "open"] / s.loc[s.index[28], "close"] - 1)
    assert s.loc[d, "f_oc_mom5"] == pytest.approx(
        np.mean([s.loc[s.index[i], "oc_ret"] for i in range(25, 30)]))
    # gap_ret is the SAME-day open gap (execution-time info, guard only)
    assert s.loc[d, "gap_ret"] == pytest.approx(
        s.loc[d, "open"] / s.loc[prev, "close"] - 1)


# ── decide_adaptive bands + gap guard ────────────────────────────────────────
def _ctx(**kw):
    base = {"date": "2026-06-09", "und_ret1": 0.0, "und_ret5": 0.0,
            "und_vol20": 0.02, "und_sma50_dist": 0.0, "vix_level": 16.0,
            "vix_chg": 0.0, "tnx_chg": 0.0, "futures_ret": None,
            "underlying": "SOXX", "pair": PAIR, "asia": {},
            "atr_pct": {"SOXL": 0.04, "SOXS": 0.04}}
    base.update(kw)
    return base


def test_decide_adaptive_bands():
    refs = {"SOXL": 30.0, "SOXS": 5.0}
    flat = decide_adaptive(_ctx(), 0.51, entry_ref=refs)
    assert flat.side == "STAND_ASIDE" and flat.model == "adaptive"
    half = decide_adaptive(_ctx(), 0.56, entry_ref=refs)
    assert half.side == "SOXL" and half.size_factor == 0.5
    full = decide_adaptive(_ctx(), 0.70, entry_ref=refs)
    assert full.side == "SOXL" and full.size_factor == 1.0
    assert full.stop_price < 30.0 < full.target_price
    bear = decide_adaptive(_ctx(), 0.30, entry_ref=refs)
    assert bear.side == "SOXS"


def test_decide_adaptive_crisis_vix_abstains():
    d = decide_adaptive(_ctx(vix_level=45.0), 0.9)
    assert d.side == "STAND_ASIDE"
    assert "VIX" in d.abstain_reason


def test_guard_triggered_logic():
    # bull call, gap up beyond threshold → triggered
    assert guard_triggered("SOXL", PAIR, 0.02, 0.025)
    # bull call, gap DOWN → not triggered (gap against the call is fine)
    assert not guard_triggered("SOXL", PAIR, 0.02, -0.025)
    # bear call, gap down beyond threshold → triggered
    assert guard_triggered("SOXS", PAIR, 0.02, -0.025)
    # below threshold / no guard / abstain → never
    assert not guard_triggered("SOXL", PAIR, 0.02, 0.01)
    assert not guard_triggered("SOXL", PAIR, None, 0.05)
    assert not guard_triggered("STAND_ASIDE", PAIR, 0.02, 0.05)


def test_decide_stores_guard_threshold_when_enabled(monkeypatch):
    ctx = _ctx(asia={"TSMC": {"ret": 0.03, "vol": 0.012, "weight": 0.4}})
    monkeypatch.setattr(settings, "duel_gap_guard_z", 1.5)
    d = decide(ctx, entry_ref={"SOXL": 30.0, "SOXS": 5.0})
    assert d.side == "SOXL"
    assert d.gap_guard == pytest.approx(1.5 * 0.02, abs=1e-5)
    assert any("갭 가드" in r for r in d.reasons)
    monkeypatch.setattr(settings, "duel_gap_guard_z", 0.0)
    d2 = decide(ctx, entry_ref={"SOXL": 30.0, "SOXS": 5.0})
    assert d2.gap_guard is None            # production default: guard off


# ── synthetic frames (Asia leads perfectly) — same shape as test_duel's ─────
def _frames(n=200, lead=+1, gap=0.0):
    """Asia moves ±2% on date D; SOXX (and the levered legs) follow with the
    same sign on date D's US session. `gap` opens each bar that fraction in
    the trend direction (0 = open at prior close, like test_duel's frames)."""
    dates = pd.bdate_range("2026-01-01", periods=n).date.astype(str)

    def frame(opens, closes, amp=0.001):
        return pd.DataFrame({
            "date": dates, "open": opens, "close": closes,
            "high": [max(o, c) * (1 + amp) for o, c in zip(opens, closes,
                                                           strict=False)],
            "low": [min(o, c) * (1 - amp) for o, c in zip(opens, closes,
                                                          strict=False)],
            "volume": [1_000_000] * n,
        })

    asia_c, sx_o, sx_c, sl_o, sl_c, ss_o, ss_c = [], [], [], [], [], [], []
    a, sx, sl, ss = 100.0, 100.0, 30.0, 30.0
    for i in range(n):
        wig = 0.005 if i % 2 == 0 else -0.005   # keep rolling vol > 0
        a *= 1 + (0.02 + wig) * lead
        asia_c.append(a)
        us = (0.015 + wig) * lead
        sx_o.append(sx * (1 + gap * lead))
        sx *= 1 + us
        sx_c.append(sx)
        sl_o.append(sl * (1 + 3 * gap * lead))
        sl *= 1 + 3 * us
        sl_c.append(sl)
        ss_o.append(ss * (1 - 3 * gap * lead))
        ss *= 1 - 3 * us
        ss_c.append(ss)

    flat = [15.0] * n
    return {
        "SOXX": frame(sx_o, sx_c),
        "SOXL": frame(sl_o, sl_c, amp=0.08),    # roomy bars: target reachable
        "SOXS": frame(ss_o, ss_c, amp=0.08),
        "^VIX": frame(flat, flat),
        "^TNX": frame([42.0] * n, [42.0] * n),
        "TSMC": frame(asia_c, asia_c),
    }


def test_backtest_adaptive_mode_scores_oos(monkeypatch):
    monkeypatch.setattr(settings, "duel_adaptive_min_train", 40)
    res = dbt.run(frames=_frames(n=200), mode="adaptive", gap_guard_z=0)
    assert res["mode"] == "adaptive"
    assert res["n_warmup"] >= 40           # warmup sessions excluded, visible
    assert res["n_traded"] > 20
    assert res["accuracy"] >= 0.9          # perfect lead is learnable OOS
    assert res["metrics"]["total_return"] > 0


def test_backtest_gap_guard_blocks_and_reports(monkeypatch):
    # 1% same-direction opening gaps + a hair-trigger z → every directional
    # entry blocks; blocked trades are tracked, not silently dropped
    res_off = dbt.run(frames=_frames(n=120, gap=0.01), gap_guard_z=0)
    res_on = dbt.run(frames=_frames(n=120, gap=0.01), gap_guard_z=0.01)
    assert res_on["n_gap_guard"] > 0
    assert res_on["n_traded"] + res_on["n_gap_guard"] == res_off["n_traded"]
    assert res_on["guard_blocked_accuracy"] is not None


def test_eval_outcomes_marks_gap_guard(tmp_path, monkeypatch):
    db = tmp_path / "g.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_gap_guard_z", 0.001)  # hair trigger

    frames = _frames(n=120, lead=+1, gap=0.01)   # opens gap INTO the up call
    last = frames["SOXX"]["date"].iloc[-1]
    d = dlive.tonight(frames=frames, with_futures=False, session_date=last)
    assert d.side == "SOXL" and d.gap_guard is not None

    tally = dlive.eval_outcomes(frames=frames)
    with connect(db) as conn:
        row = conn.execute(
            "SELECT exit_reason, correct, pnl_pct FROM duel_decisions").fetchone()
    # the up-lead synthetic gaps UP into an up call → guard cancels the entry
    assert row["exit_reason"] == "gap_guard"
    assert row["correct"] is None and row["pnl_pct"] is None
    assert tally["evaluated"] == 1 and tally["wins"] == 0


def test_tonight_records_adaptive_shadow_from_archive(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_adaptive_min_train", 40)

    frames = _frames(n=200, lead=+1)

    def fake_download(syms, period="max"):
        sym = syms[0]
        key = {"2330.TW": "TSMC"}.get(sym, sym)
        if key in frames:
            df = frames[key].copy()
            df["symbol"] = sym
            return df
        return pd.DataFrame()
    monkeypatch.setattr(duel_data.market, "download_ohlcv", fake_download)
    duel_data.archive(period="max")        # populate price_history for training

    last = frames["SOXX"]["date"].iloc[-1]
    d = dlive.tonight(frames=frames, with_futures=False, session_date=last)
    assert d.model == "champion"           # gate off → production stays champion

    with connect(db) as conn:
        row = conn.execute(
            "SELECT side, score FROM duel_variants WHERE variant='adaptive'"
        ).fetchone()
    assert row is not None                 # shadow row committed for the A/B
    assert row["side"] == "SOXL"           # learner sees the perfect up-lead
    assert row["score"] > 0


def test_tonight_uses_adaptive_when_gated_on(tmp_path, monkeypatch):
    db = tmp_path / "u.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_adaptive_min_train", 40)
    monkeypatch.setattr(settings, "duel_use_adaptive", True)

    frames = _frames(n=200, lead=+1)

    def fake_download(syms, period="max"):
        sym = syms[0]
        key = {"2330.TW": "TSMC"}.get(sym, sym)
        if key in frames:
            df = frames[key].copy()
            df["symbol"] = sym
            return df
        return pd.DataFrame()
    monkeypatch.setattr(duel_data.market, "download_ohlcv", fake_download)
    duel_data.archive(period="max")

    last = frames["SOXX"]["date"].iloc[-1]
    d = dlive.tonight(frames=frames, with_futures=False, session_date=last)
    assert d.model == "adaptive"
    assert d.side == "SOXL"
    with connect(db) as conn:
        row = conn.execute(
            "SELECT model, side FROM duel_decisions").fetchone()
    assert row["model"] == "adaptive" and row["side"] == "SOXL"


def test_migration_adds_gap_guard_and_model(tmp_path):
    import sqlite3

    db = tmp_path / "m.db"
    with sqlite3.connect(db) as conn:      # simulate the pre-upgrade table
        conn.execute(
            "CREATE TABLE duel_decisions ("
            "pair TEXT NOT NULL DEFAULT 'soxl_soxs', decision_date TEXT NOT NULL, "
            "side TEXT NOT NULL, score REAL, conviction REAL, size_factor REAL, "
            "entry_ref REAL, stop_price REAL, target_price REAL, reasons TEXT, "
            "components TEXT, entry_fill REAL, exit_fill REAL, exit_reason TEXT, "
            "pnl_pct REAL, soxx_oc_ret REAL, correct INTEGER, "
            "captured_at TEXT NOT NULL, evaluated_at TEXT, "
            "PRIMARY KEY (pair, decision_date))")
        conn.execute(
            "INSERT INTO duel_decisions (pair, decision_date, side, captured_at)"
            " VALUES ('soxl_soxs', '2026-06-10', 'SOXL', 'x')")
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM duel_decisions").fetchone()
        assert row["gap_guard"] is None
        assert row["model"] == "champion"      # backfilled default
        assert row["side"] == "SOXL"           # data preserved


def test_set_active_rejects_adaptive_with_guidance():
    from surge.duel import variants

    with pytest.raises(KeyError, match="SURGE_DUEL_USE_ADAPTIVE"):
        variants.set_active("adaptive")
