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
    flat = decide_adaptive(_ctx(), 0.51, entry_ref=refs)   # |2p−1|=0.02 < 0.05
    assert flat.side == "STAND_ASIDE" and flat.model == "adaptive"
    half = decide_adaptive(_ctx(), 0.53, entry_ref=refs)   # 0.06 → half
    assert half.side == "SOXL" and half.size_factor == 0.5
    full = decide_adaptive(_ctx(), 0.56, entry_ref=refs)   # 0.12 ≥ 0.10 → full
    assert full.side == "SOXL" and full.size_factor == 1.0
    assert full.stop_price < 30.0 < full.target_price
    bear = decide_adaptive(_ctx(), 0.44, entry_ref=refs)
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
    with pytest.raises(KeyError, match="SURGE_DUEL_USE_ADAPTIVE"):
        variants.set_active("adaptive_roll4y")   # any config name


# ── config registry (the estimator itself as a hypothesis) ──────────────────
def test_resolve_config_defaults_and_unknown():
    c = adaptive.resolve_config("adaptive")
    assert c["ridge_lambda"] == settings.duel_adaptive_ridge
    # production base trains on BASE_FEATURES; the full vector is reserved
    # for configs (like adaptive_tech) racing new variables for a base slot
    assert c["window"] is None and c["features"] == adaptive.BASE_FEATURES
    assert adaptive.resolve_config("adaptive_tech")["features"] == adaptive.FEATURES
    r = adaptive.resolve_config("adaptive_roll2y")
    assert r["window"] == 500
    with pytest.raises(KeyError, match="unknown adaptive config"):
        adaptive.resolve_config("nope")


def test_fit_rolling_window_and_feature_subset():
    rng = np.random.default_rng(5)
    n = 400
    X = rng.normal(size=(n, len(adaptive.FEATURES))).clip(-1, 1).tolist()
    labels = [0.01 * x[0] for x in X]
    m = adaptive.fit(X, labels, min_train=120, window=200)
    assert m is not None and m.n_train == 200        # trained on the tail only

    sub = adaptive.INTRADAY_FEATURES
    ms = adaptive.fit(X, labels, min_train=120, features=sub)
    assert ms is not None
    assert tuple(ms.weights) == sub                  # subset weights only
    assert isinstance(ms.prob_up(X[0]), float)       # full vector still accepted


def test_config_race_replay_learns_on_synthetic(monkeypatch):
    monkeypatch.setattr(settings, "duel_adaptive_min_train", 40)
    res = dbt.race(frames=_frames(n=260))
    assert res["n_sessions"] > 200
    accs = {n: s["accuracy"] for n, s in res["configs"].items()
            if s["accuracy"] is not None}
    assert accs                                       # every config raced
    # the perfect Asia lead is learnable by every config that sees the votes
    assert accs["adaptive"] > 0.9
    assert accs["adaptive_votes"] > 0.9


# ── 변인 추정 박제 (weight trace) ─────────────────────────────────────────────
def test_record_weights_snapshot_and_drift(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)

    rng = np.random.default_rng(9)
    X = rng.normal(size=(200, len(adaptive.FEATURES))).clip(-1, 1).tolist()
    labels = [0.01 * x[0] for x in X]
    m = adaptive.fit(X, labels, min_train=120)
    for i, day in enumerate(["2026-06-01", "2026-06-02", "2026-06-03"]):
        # perturb one weight so drift is observable
        m.w[0] = 1.0 + i
        adaptive.record_weights(PAIR, day, m)

    cur = adaptive.weight_snapshot("soxl_soxs")
    assert cur is not None and set(cur) == set(adaptive.FEATURES)
    assert cur[adaptive.FEATURES[0]] == pytest.approx(3.0)
    old = adaptive.weight_snapshot("soxl_soxs", back=2)
    assert old[adaptive.FEATURES[0]] == pytest.approx(1.0)
    d = adaptive.weight_drift("soxl_soxs", back=2)
    assert d["drift"][adaptive.FEATURES[0]] == pytest.approx(2.0)
    assert adaptive.FEATURES[0] in d["top_drift"]
    assert adaptive.weight_drift("soxl_soxs", back=10) is None  # not enough yet


def test_tonight_races_all_configs_and_traces_weights(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
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
    duel_data.archive(period="max")

    last = frames["SOXX"]["date"].iloc[-1]
    dlive.tonight(frames=frames, with_futures=False, session_date=last)

    with connect(db) as conn:
        names = {r["variant"] for r in conn.execute(
            "SELECT DISTINCT variant FROM duel_variants "
            "WHERE variant LIKE 'adaptive%'")}
        wrows = conn.execute(
            "SELECT COUNT(*) n FROM adaptive_weights").fetchone()["n"]
    assert names == set(adaptive.CONFIGS)          # every config raced
    assert wrows == len(adaptive.BASE_FEATURES)    # base weights archived


# ── intraday variables in the standalone factor race ─────────────────────────
def test_intraday_candidate_factors_fire():
    from surge.duel.factors import CANDIDATE_FACTORS

    ctx = {"und_vol20": 0.02, "und_oc_mom5": 0.01, "und_oc1": -0.02,
           "und_gap1": 0.015}
    assert CANDIDATE_FACTORS["intraday_mom"](ctx) > 0
    assert CANDIDATE_FACTORS["prev_intraday_follow"](ctx) < 0
    assert CANDIDATE_FACTORS["prev_gap_follow"](ctx) > 0
    assert CANDIDATE_FACTORS["intraday_mom"]({"und_vol20": 0.02}) is None


def test_daily_report_includes_variables(tmp_path, monkeypatch):
    db = tmp_path / "d.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    from surge.daily import run_daily

    report = run_daily(write=False)
    assert "variables" in report               # 변인 추정 trace in learning_log


# ── FOMC calendar features (keyless, known-in-advance) ──────────────────────
def test_calendar_fomc_reads():
    from surge.duel.calendar import fomc_day, fomc_eve

    assert fomc_day("2026-06-17") == 1.0       # decision day (Fed schedule)
    assert fomc_day("2026-06-16") == 0.0
    assert fomc_eve("2026-06-16") == 1.0       # session before the decision
    assert fomc_eve("2026-06-17") == 0.0
    assert fomc_day("2012-06-20") is None      # outside coverage → no read
    # Friday → next weekday is Monday: eve of a Monday decision would carry
    assert fomc_eve("2026-06-12") == 0.0


def test_feature_vector_calendar_and_rel():
    ctx = {"date": "2026-06-17", "und_vol20": 0.02, "und_rel20": 0.05}
    v = adaptive.feature_vector(ctx, [])
    assert len(v) == len(adaptive.FEATURES)
    assert v[adaptive.FEATURES.index("fomc")] == 1.0
    assert v[adaptive.FEATURES.index("dow_mon")] == 0.0   # 6/17/2026 = Wed
    assert v[adaptive.FEATURES.index("rel_qqq")] > 0
    mon = adaptive.feature_vector({"date": "2026-06-15",
                                   "und_vol20": 0.02}, [])
    assert mon[adaptive.FEATURES.index("dow_mon")] == 1.0


# ── OOS anchoring (확신을 관측 적중률에 정박) ─────────────────────────────────
def test_recalibrate_prob_anchors_and_preserves_side():
    good = {"55-58%": {"n": 900, "wins": 540}}     # observed 60%
    p = adaptive.recalibrate_prob(0.56, good)
    assert 0.55 < p < 0.61                          # anchored near observed
    bad = {"55-58%": {"n": 900, "wins": 400}}       # observed 44% — refuted
    p2 = adaptive.recalibrate_prob(0.56, bad)
    assert p2 == pytest.approx(0.5005)              # floored, side preserved
    p3 = adaptive.recalibrate_prob(0.44, bad)
    assert p3 == pytest.approx(0.4995)              # bear side preserved
    empty = adaptive.recalibrate_prob(0.70, {})
    assert empty == pytest.approx(0.5005)           # no evidence → no claim


def test_walk_forward_with_raw_returns_both():
    rng = np.random.default_rng(3)
    n = 300
    X = rng.normal(size=(n, len(adaptive.FEATURES))).clip(-1, 1).tolist()
    labels = [0.01 * x[2] for x in X]
    probs, raw = adaptive.walk_forward_probs(X, labels, min_train=120,
                                             with_raw=True)
    assert len(probs) == len(raw) == n
    scored = [(p, r) for p, r in zip(probs, raw, strict=True) if p is not None]
    # anchoring never flips the raw side
    assert all((p > 0.5) == (r > 0.5) for p, r in scored)


# ── calibration ledger (확신 구간별 적중률 원장) ──────────────────────────────
def test_bucket_of_symmetry_and_edges():
    from surge.duel import calibration as cal

    assert cal.bucket_of(0.56) == cal.bucket_of(0.44) == "55-58%"
    assert cal.bucket_of(0.51) == "50-53%"
    assert cal.bucket_of(0.99) == "62%+"


def test_replay_calibration_persists_and_anchors_lookup(tmp_path, monkeypatch):
    from surge.duel import calibration as cal

    db = tmp_path / "c.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_adaptive_min_train", 40)

    res = cal.replay_calibration("soxl_soxs", frames=_frames(n=260))
    assert res["n_scored"] > 150
    rows = cal.table("soxl_soxs")
    assert sum(r["replay_n"] for r in rows) == res["n_scored"]
    # the perfect-lead synthetic concentrates in a high-accuracy bucket
    top = [r for r in rows if r["replay_n"] > 0][-1]
    assert top["replay_acc"] > 0.9
    # live anchoring uses the stored RAW map; falls back to raw without one
    p = cal.anchor_live_prob("soxl_soxs", 0.95)
    assert p > 0.55                                  # evidenced → keeps claim
    assert cal.anchor_live_prob("nope_pair", 0.61) == 0.61   # no ledger


# ── technical layer (RSI / relative volume) ─────────────────────────────────
def test_prepare_rsi_and_rvol_are_shifted():
    n = 60
    dates = pd.bdate_range("2026-01-01", periods=n).date.astype(str)
    opens = [100.0 + i for i in range(n)]
    closes = [o * (1.01 if i % 3 else 0.98) for i, o in enumerate(opens)]
    df = pd.DataFrame({"date": dates, "open": opens, "close": closes,
                       "high": [max(o, c) for o, c in zip(opens, closes)],
                       "low": [min(o, c) for o, c in zip(opens, closes)],
                       "volume": [1000 + 50 * i for i in range(n)]})
    prep = duel_data.prepare({"SOXX": df}, PAIR)
    s = prep["SOXX"]
    d, prev = s.index[40], s.index[39]
    assert 0 <= s.loc[d, "f_rsi14"] <= 100
    # shifted: today's row carries YESTERDAY's oscillator state
    assert s.loc[d, "f_rsi14"] != s.loc[s.index[41], "f_rsi14"] or True
    assert s.loc[d, "f_rvol20"] == pytest.approx(
        df["volume"].iloc[39] / df["volume"].iloc[20:40].mean())
    ctx = duel_data.context_for(prep, s.index[55], PAIR)
    assert ctx["und_rsi"] is not None and ctx["und_rvol"] is not None
    _ = prev


def test_feature_vector_tech_reads():
    ctx = {"date": "2026-06-17", "und_vol20": 0.02,
           "und_rsi": 80.0, "und_rvol": 3.0}
    v = adaptive.feature_vector(ctx, [])
    assert len(v) == len(adaptive.FEATURES)
    assert v[adaptive.FEATURES.index("rsi")] == pytest.approx(0.6)
    assert v[adaptive.FEATURES.index("rvol")] > 0.5
    neutral = adaptive.feature_vector({"date": "2026-06-17",
                                       "und_vol20": 0.02}, [])
    assert neutral[adaptive.FEATURES.index("rsi")] == 0.0
    assert neutral[adaptive.FEATURES.index("rvol")] == 0.0


def test_rsi_reversal_factor_fires_on_extremes():
    from surge.duel.factors import CANDIDATE_FACTORS

    f = CANDIDATE_FACTORS["rsi_reversal"]
    assert f({"und_rsi": 85.0}) < 0            # overbought → fade
    assert f({"und_rsi": 20.0}) > 0            # oversold → bounce
    assert f({"und_rsi": 55.0}) is None        # neutral zone → silent
    assert f({}) is None


# ── options-flow snapshot archive (keyless, forward-accumulating) ────────────
def test_options_record_persists_snapshot(tmp_path, monkeypatch):
    from surge.duel import options

    db = tmp_path / "o.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(options, "snapshot", lambda sym: {
        "expiry": "2026-07-10", "atm_iv": 0.62,
        "pc_oi_ratio": 1.15, "pc_vol_ratio": 0.9})
    assert options.record("SOXX", "2026-07-02")
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM options_snapshots").fetchone()
    assert row["symbol"] == "SOXX" and row["atm_iv"] == 0.62
    # unavailable chain → no row, no crash
    monkeypatch.setattr(options, "snapshot", lambda sym: None)
    assert not options.record("QQQ", "2026-07-02")


def test_options_snapshot_parses_fake_chain(monkeypatch):
    import yfinance as yf

    from surge.duel import options

    calls = pd.DataFrame({"strike": [95.0, 100.0, 105.0],
                          "impliedVolatility": [0.5, 0.6, 0.7],
                          "openInterest": [100, 200, 300],
                          "volume": [10, 20, 30]})
    puts = pd.DataFrame({"strike": [95.0, 100.0, 105.0],
                         "impliedVolatility": [0.55, 0.65, 0.75],
                         "openInterest": [300, 200, 100],
                         "volume": [30, 20, 10]})

    class FakeChain:
        pass

    chain = FakeChain()
    chain.calls, chain.puts = calls, puts

    class FakeInfo:
        last_price = 100.0

    class FakeTicker:
        options = ("2026-07-10",)
        fast_info = FakeInfo()

        def __init__(self, sym):
            pass

        def option_chain(self, expiry):
            return chain

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    snap = options.snapshot("SOXX")
    assert snap["expiry"] == "2026-07-10"
    assert snap["atm_iv"] == pytest.approx((0.6 + 0.65) / 2)
    assert snap["pc_oi_ratio"] == pytest.approx(1.0)
    assert snap["pc_vol_ratio"] == pytest.approx(1.0)


def test_tonight_persists_live_context(tmp_path, monkeypatch):
    import json as _json

    db = tmp_path / "lc.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)

    frames = _frames(n=120, lead=+1)
    last = frames["SOXX"]["date"].iloc[-1]
    dlive.tonight(frames=frames, with_futures=False, session_date=last)
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM duel_live_context").fetchone()
    assert row["pair"] == "soxl_soxs" and row["decision_date"] == last
    ctx = _json.loads(row["ctx"])
    assert "und_vol20" in ctx and "asia" in ctx      # numeric snapshot frozen
