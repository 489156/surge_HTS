"""Duel engine tests — deterministic and offline (no network)."""

import json

import pandas as pd
import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.duel import backtest as dbt
from surge.duel import live as dlive
from surge.duel import signals
from surge.duel.decide import decide


# ── signal components ────────────────────────────────────────────────────────
def test_asia_lead_sign_and_renormalization():
    up = signals.asia_lead({"TSMC": {"ret": 0.03, "vol": 0.015, "weight": 0.4}})
    assert up.value > 0.5
    down = signals.asia_lead({"TSMC": {"ret": -0.03, "vol": 0.015, "weight": 0.4}})
    assert down.value < -0.5
    assert signals.asia_lead({}) is None  # holiday → component absent


def test_rates_inversion_and_clip():
    c = signals.rates(0.20)        # +20bp yield spike
    assert c.value == -1.0         # clipped, bearish for semis
    assert signals.rates(None) is None


def test_mean_reversion_only_on_extremes():
    quiet = signals.mean_reversion(0.005, 0.02)   # 0.25σ
    assert quiet.value == 0.0
    blowoff = signals.mean_reversion(0.08, 0.02)  # +4σ day → fade
    assert blowoff.value < 0


def test_compute_signal_renormalizes_missing():
    ctx = {"und_ret1": 0.0, "und_ret5": 0.0, "und_vol20": 0.02,
           "und_sma50_dist": 0.0, "vix_level": None, "vix_chg": None,
           "tnx_chg": None, "asia": {}, "futures_ret": None}
    sig = signals.compute_signal(ctx)
    assert sig["score"] == pytest.approx(0.0)
    present = {c.name for c in sig["components"]}
    assert "asia_lead" not in present and "vix_regime" not in present


# ── decision ─────────────────────────────────────────────────────────────────
def _ctx(**kw):
    base = {"date": "2026-06-09", "und_ret1": 0.0, "und_ret5": 0.0,
            "und_vol20": 0.02, "und_sma50_dist": 0.0, "vix_level": 16.0,
            "vix_chg": 0.0, "tnx_chg": 0.0, "futures_ret": None,
            "underlying": "SOXX",
            "pair": {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"},
            "asia": {"TSMC": {"ret": 0.03, "vol": 0.012, "weight": 0.4}},
            "atr_pct": {"SOXL": 0.04, "SOXS": 0.04}}
    base.update(kw)
    return base


def test_decide_strong_asia_up_picks_soxl_with_brackets():
    d = decide(_ctx(), entry_ref={"SOXL": 30.0, "SOXS": 5.0})
    assert d.side == "SOXL"
    assert d.stop_price < 30.0 < d.target_price          # long-leg bracket
    assert d.stop_price == pytest.approx(30.0 * (1 - settings.duel_stop_atr * 0.04))
    assert d.size_pct > 0


def test_decide_abstains_on_weak_signal():
    d = decide(_ctx(asia={}), entry_ref={"SOXL": 30.0, "SOXS": 5.0})
    assert d.side == "STAND_ASIDE"
    assert d.size_pct == 0.0
    assert "신호 불충분" in (d.abstain_reason or "")


def test_decide_crisis_vix_forces_abstain_even_with_strong_signal():
    d = decide(_ctx(vix_level=45.0), entry_ref={"SOXL": 30.0, "SOXS": 5.0})
    assert d.side == "STAND_ASIDE"
    assert "VIX" in d.abstain_reason


# ── bracket simulation ───────────────────────────────────────────────────────
def test_simulate_bracket_stop_first_when_both_hit():
    px, reason = dbt.simulate_bracket(10.0, 11.5, 9.0, 10.5, stop=9.5,
                                      target=11.0, slip_bps=0)
    assert reason == "stop" and px == 9.5


def test_simulate_bracket_gap_through_stop_fills_at_open():
    px, reason = dbt.simulate_bracket(9.0, 9.2, 8.8, 9.1, stop=9.5,
                                      target=11.0, slip_bps=0)
    assert reason == "stop" and px == 9.0


def test_simulate_bracket_target_then_time_exit():
    px, reason = dbt.simulate_bracket(10.0, 11.2, 9.8, 10.4, stop=9.5,
                                      target=11.0, slip_bps=0)
    assert reason == "target" and px == 11.0
    px, reason = dbt.simulate_bracket(10.0, 10.4, 9.8, 10.2, stop=9.5,
                                      target=11.0, slip_bps=0)
    assert reason == "close" and px == 10.2


# ── synthetic end-to-end backtest (Asia perfectly leads US) ──────────────────
def _frames(n=80, lead=+1):
    """Asia moves ±2% on date D; SOXX (and the levered legs) follow with the
    same sign on date D's US session. lead=+1 → up days."""
    dates = pd.bdate_range("2026-01-01", periods=n).date.astype(str)

    def frame(opens, closes, amp=0.001):
        return pd.DataFrame({
            "date": dates, "open": opens, "close": closes,
            "high": [max(o, c) * (1 + amp) for o, c in zip(opens, closes)],
            "low": [min(o, c) * (1 - amp) for o, c in zip(opens, closes)],
            "volume": [1_000_000] * n,
        })

    asia_close, soxx_o, soxx_c, soxl_o, soxl_c, soxs_o, soxs_c = [], [], [], [], [], [], []
    a, sx, sl, ss = 100.0, 100.0, 30.0, 30.0
    for i in range(n):
        # alternate magnitudes so rolling vol > 0 (constant returns would
        # silently drop the asia component and explode mean-reversion z-scores)
        wig = 0.005 if i % 2 == 0 else -0.005
        a *= 1 + (0.02 + wig) * lead
        asia_close.append(a)
        us = (0.015 + wig) * lead
        soxx_o.append(sx)
        sx *= 1 + us
        soxx_c.append(sx)
        soxl_o.append(sl)
        sl *= 1 + 3 * us
        soxl_c.append(sl)
        soxs_o.append(ss)
        ss *= 1 - 3 * us
        soxs_c.append(ss)

    flat = [15.0] * n
    return {
        "SOXX": frame(soxx_o, soxx_c),
        "SOXL": frame(soxl_o, soxl_c, amp=0.08),   # roomy bars: target reachable
        "SOXS": frame(soxs_o, soxs_c, amp=0.08),
        "^VIX": frame(flat, flat),
        "^TNX": frame([42.0] * n, [42.0] * n),
        "TSMC": frame(asia_close, asia_close),
    }


def test_backtest_perfect_asia_lead_is_accurate_and_profitable():
    res = dbt.run(frames=_frames(lead=+1))
    assert res["n_traded"] > 10
    assert res["accuracy"] >= 0.9
    assert res["metrics"]["total_return"] > 0
    assert res["ic"].get("asia_lead", 0) != 0


def test_backtest_down_lead_picks_soxs():
    res = dbt.run(frames=_frames(lead=-1))
    assert res["n_traded"] > 10
    assert res["accuracy"] >= 0.9       # correct = picked SOXS on down days


# ── persistence + eval loop ──────────────────────────────────────────────────
def test_tonight_persists_and_eval_scores(tmp_path, monkeypatch):
    db = tmp_path / "d.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)

    frames = _frames(lead=+1)
    last = frames["SOXX"]["date"].iloc[-1]
    d = dlive.tonight(frames=frames, with_futures=False, session_date=last)
    assert d.side == "SOXL"

    with connect(db) as conn:
        row = conn.execute("SELECT * FROM duel_decisions").fetchone()
    assert row["side"] == "SOXL"
    assert json.loads(row["reasons"])

    # the stored session date is in the past relative to "today" → scorable
    tally = dlive.eval_outcomes(frames=frames)
    assert tally["evaluated"] == 1
    assert tally["wins"] == 1
    assert tally["accuracy"] == 1.0
    assert tally["pairs"]["soxl_soxs"]["wins"] == 1


# ── multi-pair + migration + archive ─────────────────────────────────────────
def test_pairs_registry():
    from surge.duel.pairs import PAIRS, all_symbols, get_pair

    assert {"soxl_soxs", "tqqq_sqqq", "tecl_tecs", "labu_labd"} <= set(PAIRS)
    assert {"tna_tza", "fas_faz"} <= set(PAIRS)        # diversifying rate-lift legs
    assert "nvdl_nvd" in set(PAIRS)                    # screened single-stock exception
    p = get_pair("tqqq_sqqq")
    assert p["bull"] == "TQQQ" and p["bear"] == "SQQQ" and p["underlying"] == "QQQ"
    nvda = get_pair("nvdl_nvd")
    assert nvda["bull"] == "NVDL" and nvda["bear"] == "NVD" and nvda["underlying"] == "NVDA"
    syms = all_symbols()
    assert "SOXL" in syms and "QQQ" in syms and len(syms) == len(set(syms))
    with pytest.raises(KeyError):
        get_pair("nope")


def test_nvdl_nvd_has_no_basket_but_has_attention_leader():
    """NVDA is the underlying itself — a basket entry would be circular
    (leadership vs its own basket), so this pair intentionally has none, but
    it DOES get the direct-leader attention read (see pairs.py docstring)."""
    from surge.duel.attention import LEADERS
    from surge.duel.baskets import BASKETS, framework_features

    assert "nvdl_nvd" not in BASKETS
    assert framework_features("nvdl_nvd").empty     # graceful, not a crash
    assert LEADERS["nvdl_nvd"] == "NVDA"


def test_decide_routes_to_pair_legs():
    ctx = _ctx(pair={"id": "tqqq_sqqq", "bull": "TQQQ", "bear": "SQQQ"},
               underlying="QQQ",
               atr_pct={"TQQQ": 0.03, "SQQQ": 0.03})
    d = decide(ctx, entry_ref={"TQQQ": 60.0, "SQQQ": 20.0})
    assert d.side == "TQQQ"            # strong asia-up → bull leg of THIS pair
    assert d.pair_id == "tqqq_sqqq"
    assert d.stop_price < 60.0 < d.target_price


def _nvda_frames(n=200, lead=+1):
    """Same synthetic-lead construction as `_frames`, but keyed for the
    nvdl_nvd registry entry (NVDA/NVDL/NVD, 2x legs — NOT 3x like the index
    pairs) to verify the pipeline is genuinely registry-driven rather than
    hardcoded to the original 4-6 pairs."""
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

    asia_c, nv_o, nv_c, nl_o, nl_c, nd_o, nd_c = [], [], [], [], [], [], []
    a, nv, nl, nd = 100.0, 100.0, 30.0, 30.0
    for i in range(n):
        wig = 0.005 if i % 2 == 0 else -0.005
        a *= 1 + (0.02 + wig) * lead
        asia_c.append(a)
        us = (0.015 + wig) * lead
        nv_o.append(nv)
        nv *= 1 + us
        nv_c.append(nv)
        nl_o.append(nl)
        nl *= 1 + 2 * us          # NVDL: 2x, not 3x
        nl_c.append(nl)
        nd_o.append(nd)
        nd *= 1 - 2 * us          # NVD: -2x
        nd_c.append(nd)

    flat = [15.0] * n
    return {
        "NVDA": frame(nv_o, nv_c),
        "NVDL": frame(nl_o, nl_c, amp=0.08),
        "NVD": frame(nd_o, nd_c, amp=0.08),
        "^VIX": frame(flat, flat),
        "^TNX": frame([42.0] * n, [42.0] * n),
        "TSMC": frame(asia_c, asia_c),
    }


def test_nvdl_nvd_backtest_and_live_call_flow(tmp_path, monkeypatch):
    """End-to-end proof the pipeline is registry-driven, not hardcoded to the
    original pairs: 2x (not 3x) legs, no basket, single-stock underlying."""
    db = tmp_path / "nvdl.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)

    res = dbt.run(frames=_nvda_frames(), pair_id="nvdl_nvd", gap_guard_z=0)
    assert res["bull"] == "NVDL" and res["bear"] == "NVD"
    assert res["n_traded"] > 10
    assert res["accuracy"] >= 0.9

    frames = _nvda_frames(n=120)
    last = frames["NVDA"]["date"].iloc[-1]
    d = dlive.tonight(frames=frames, with_futures=False, session_date=last,
                      pair_id="nvdl_nvd")
    assert d.pair_id == "nvdl_nvd" and d.side == "NVDL"


def test_migration_adds_pair_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:   # simulate the v1 single-pair table
        conn.execute(
            "CREATE TABLE duel_decisions ("
            "decision_date TEXT PRIMARY KEY, side TEXT NOT NULL, score REAL, "
            "conviction REAL, size_factor REAL, entry_ref REAL, stop_price REAL, "
            "target_price REAL, reasons TEXT, entry_fill REAL, exit_fill REAL, "
            "exit_reason TEXT, pnl_pct REAL, soxx_oc_ret REAL, correct INTEGER, "
            "captured_at TEXT NOT NULL, evaluated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO duel_decisions (decision_date, side, captured_at) "
            "VALUES ('2026-06-10', 'STAND_ASIDE', 'x')"
        )
    init_db(db)                          # runs the migration
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM duel_decisions").fetchone()
        assert row["pair"] == "soxl_soxs"           # backfilled
        assert row["decision_date"] == "2026-06-10"  # data preserved
        cols = [r[1] for r in conn.execute("PRAGMA table_info(duel_decisions)")]
        assert "pair" in cols


def test_upsert_immutable_preserves_captured_at(tmp_path, monkeypatch):
    from surge.db import upsert as db_upsert

    db = tmp_path / "i.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    row = {"pair": "soxl_soxs", "decision_date": "2026-06-11", "side": "SOXL",
           "score": 0.2, "captured_at": "FIRST"}
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", [row], immutable=("captured_at",))
        db_upsert(conn, "duel_decisions",
                  [{**row, "side": "SOXS", "captured_at": "SECOND"}],
                  immutable=("captured_at",))
        got = conn.execute("SELECT side, captured_at FROM duel_decisions").fetchone()
    assert got["side"] == "SOXS"            # refresh updated the call…
    assert got["captured_at"] == "FIRST"    # …but the audit timestamp is write-once


def test_migration_recovers_stranded_v1(tmp_path):
    import sqlite3

    db = tmp_path / "r.db"
    with sqlite3.connect(db) as conn:  # simulate a crash mid-migration:
        conn.execute(                  # only the renamed v1 table exists
            "CREATE TABLE duel_decisions_v1 ("
            "decision_date TEXT PRIMARY KEY, side TEXT NOT NULL, "
            "captured_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO duel_decisions_v1 VALUES "
                     "('2026-06-10','STAND_ASIDE','x')")
    init_db(db)                        # recovery path must resume the copy
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM duel_decisions").fetchone()
        assert row["pair"] == "soxl_soxs" and row["side"] == "STAND_ASIDE"
        assert conn.execute("SELECT name FROM sqlite_master WHERE "
                            "name='duel_decisions_v1'").fetchone() is None


def test_eval_defers_rows_when_fetch_fails(tmp_path, monkeypatch):
    from surge.db import upsert as db_upsert

    db = tmp_path / "e.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", [{
            "pair": "soxl_soxs", "decision_date": "2026-01-05", "side": "SOXL",
            "score": 0.3, "captured_at": "x",
        }])
    # underlying fetch fails (empty frames) → row must stay un-stamped for retry
    monkeypatch.setattr(dlive.ddata, "fetch_frames", lambda *a, **k: {})
    monkeypatch.setattr(dlive.ddata, "fetch_shared", lambda *a, **k: {})
    dlive.eval_outcomes()
    with connect(db) as conn:
        row = conn.execute("SELECT evaluated_at FROM duel_decisions").fetchone()
    assert row["evaluated_at"] is None     # NOT permanently lost


def test_archive_and_offline_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)

    frames = _frames(lead=+1)

    def fake_download(syms, period="max"):
        sym = syms[0]
        key = {"2330.TW": "TSMC"}.get(sym, sym)
        if key in frames:
            df = frames[key].copy()
            df["symbol"] = sym
            return df
        return pd.DataFrame()
    from surge.duel import data as duel_data
    monkeypatch.setattr(duel_data.market, "download_ohlcv", fake_download)

    res = duel_data.archive(period="max")
    assert res["total_rows"] > 0
    assert res["symbols"]["SOXL"] == 80

    # offline frames rebuilt from the archive drive a backtest
    off = duel_data.frames_from_archive()
    assert "SOXX" in off and "TSMC" in off
    bt = dbt.run(frames=off)
    assert bt["n_traded"] > 10
    assert bt["accuracy"] >= 0.9


def test_eval_persists_atr_ratchet_and_defers_lagging_bar(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_ratchet_atr", 0.5)

    frames = _frames(n=80, lead=+1)
    # Last session triggers ratchet (high reaches +10% >= trigger) and then reverses to low <= open
    last = frames["SOXX"]["date"].iloc[-1]
    d = dlive.tonight(frames=frames, with_futures=False, session_date=last)
    assert d.side == "SOXL" and d.atr_pct

    tally = dlive.eval_outcomes(frames=frames)
    assert tally["evaluated"] == 1
    with connect(db) as conn:
        row = conn.execute("SELECT atr_pct, exit_reason FROM duel_decisions WHERE decision_date=?", (last,)).fetchone()
    assert row["atr_pct"] == pytest.approx(d.atr_pct, abs=1e-5)
    assert row["exit_reason"] == "ratchet_stop"

    # Lagging bar: decision_date is past today, yet AFTER latest frame date → stay NULL for retry
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO duel_decisions (pair, decision_date, side, score, captured_at) "
            "VALUES ('soxl_soxs', '2026-06-01', 'SOXL', 0.3, 'x')"
        )
    dlive.eval_outcomes(frames=frames)
    with connect(db) as conn:
        lag_row = conn.execute(
            "SELECT evaluated_at FROM duel_decisions WHERE decision_date='2026-06-01'"
        ).fetchone()
    assert lag_row["evaluated_at"] is None


def test_fas_faz_deactivated_from_active_pairs():
    from surge.duel.pairs import PAIRS, active_pair_ids

    assert "fas_faz" in PAIRS
    assert "fas_faz" not in active_pair_ids()


def test_vix9d_fallback_and_options_context(tmp_path, monkeypatch):
    from datetime import date, timedelta
    from surge.db import upsert as db_upsert
    from surge.duel import data as ddata, factors as dfac

    db = tmp_path / "opt.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)

    start = date(2026, 6, 1)
    opt_rows = []
    for i in range(10):
        d = (start + timedelta(days=i)).isoformat()
        opt_rows.append({
            "symbol": "SOXX", "date": d, "atm_iv": 0.30,
            "pc_oi_ratio": 0.8 + i * 0.02, "pc_vol_ratio": 0.9, "captured_at": "x",
        })
    opt_rows.append({
        "symbol": "SOXX", "date": "2026-06-20", "atm_iv": 0.99,
        "pc_oi_ratio": 3.5, "pc_vol_ratio": 2.0, "captured_at": "x",
    })
    with connect(db) as conn:
        db_upsert(conn, "options_snapshots", opt_rows)

    ctx_opt = ddata.latest_options_context("SOXX", "2026-06-20")
    assert ctx_opt["opt_pc_oi"] == pytest.approx(0.98)  # leak-safe: < before_date
    assert ctx_opt.get("opt_pc_oi_z") is not None
    assert dfac._opt_pc_contrarian({"opt_pc_oi": 1.4}) > 0

    dates = pd.date_range("2026-03-01", periods=60, freq="B").strftime("%Y-%m-%d").tolist()
    soxx_df = pd.DataFrame({
        "open": [100.0 + i * 0.2 for i in range(60)],
        "high": [101.0 + i * 0.2 for i in range(60)],
        "low": [99.0 + i * 0.2 for i in range(60)],
        "close": [100.5 + i * 0.2 for i in range(60)],
        "volume": [1_000_000] * 60,
    }, index=dates)
    vix9d_df = pd.DataFrame({
        "open": [19.0] * 60, "high": [19.5] * 60, "low": [18.5] * 60,
        "close": [19.0 + (i % 3) * 0.5 for i in range(60)], "volume": [0] * 60,
    }, index=dates)
    prep = ddata.prepare({"SOXX": soxx_df, "vix9d": vix9d_df})
    lctx = ddata.latest_context(prep, "2026-06-01")
    hctx = ddata.context_for(prep, dates[-1])
    assert lctx is not None and lctx["vix_level"] is not None
    assert hctx is not None and hctx["vix_level"] is not None


def test_simulate_bracket_intraday_chronological():
    bars_target_first = [
        (10.0, 10.6, 9.9, 10.5),  # hits target=10.5 first
        (10.5, 10.5, 9.4, 9.5),   # later drops through stop=9.5
    ]
    px1, r1 = dbt.simulate_bracket_intraday(bars_target_first, stop=9.5, target=10.5, slip_bps=0)
    assert r1 == "target" and px1 == pytest.approx(10.5)

    bars_stop_first = [
        (10.0, 10.2, 9.4, 9.6),   # hits stop=9.5 first
        (9.6, 10.7, 9.6, 10.6),   # later bounces past target=10.5
    ]
    px2, r2 = dbt.simulate_bracket_intraday(bars_stop_first, stop=9.5, target=10.5, slip_bps=0)
    assert r2 == "stop" and px2 == pytest.approx(9.5)


def test_eval_outcomes_uses_intraday_for_recent_calls(tmp_path, monkeypatch):
    from datetime import date, timedelta
    from surge.db import upsert as db_upsert
    from surge.duel import data as ddata

    db = tmp_path / "intra.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_slippage_bps", 0.0)
    monkeypatch.setattr(settings, "duel_ratchet_atr", 0.0)

    idx = pd.bdate_range(end=date.today() - timedelta(days=2), periods=65).strftime("%Y-%m-%d").tolist()
    d_recent = idx[-1]
    base_df = pd.DataFrame({
        "open": [10.0] * 65, "high": [10.2] * 64 + [11.0],
        "low": [9.8] * 64 + [9.0], "close": [10.0] * 64 + [10.2],
        "volume": [100_000] * 65,
    }, index=idx)
    raw = {"SOXX": base_df.copy(), "SOXL": base_df.copy(), "SOXS": base_df.copy(), "SPY": base_df.copy()}

    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", [{
            "pair": "soxl_soxs", "decision_date": d_recent, "side": "SOXL",
            "score": 0.4, "conviction": 0.4, "size_factor": 1.0,
            "entry_ref": 10.0, "stop_price": 9.5, "target_price": 10.5,
            "captured_at": "x",
        }])

    monkeypatch.setattr(ddata, "fetch_frames", lambda period, pair, shared=None: raw)
    # 15m intraday bars: target=10.5 hits on bar 1 before stop=9.5 hits on bar 3
    monkeypatch.setattr(
        dlive, "_intraday_session_map",
        lambda sides, dates: {("SOXL", d_recent): [
            (10.0, 10.6, 9.9, 10.5),
            (10.5, 10.5, 10.1, 10.2),
            (10.2, 10.2, 9.4, 9.5),
            (9.5, 9.8, 9.4, 9.7),
        ]},
    )
    dlive.eval_outcomes()
    with connect(db) as conn:
        row = conn.execute(
            "SELECT exit_reason, exit_fill, correct FROM duel_decisions WHERE decision_date=?",
            (d_recent,),
        ).fetchone()
    assert row["exit_reason"] == "target"
    assert row["exit_fill"] == pytest.approx(10.5)
    assert row["correct"] == 1
