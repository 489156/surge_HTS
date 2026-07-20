"""Realized-vol sizing dampener + mandatory-pick constraint tests."""

import math

from surge.config import settings
from surge.db import connect, init_db
from surge.duel import live as dlive
from surge.duel.decide import (
    DuelDecision, _rvol_cap, decide, decide_adaptive, promote_forced,
)

PAIR = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}


def _ctx(**kw):
    base = {"date": "2026-07-20", "und_ret1": 0.0, "und_ret5": 0.0,
            "und_vol20": 0.015, "und_sma50_dist": 0.0, "vix_level": 16.0,
            "vix_chg": 0.0, "tnx_chg": 0.0, "futures_ret": None,
            "underlying": "SOXX", "pair": PAIR,
            "asia": {"TSMC": {"ret": 0.03, "vol": 0.012, "weight": 0.4}},
            "atr_pct": {"SOXL": 0.04, "SOXS": 0.04}}
    base.update(kw)
    return base


# ── realized-vol dampener ────────────────────────────────────────────────────
def test_rvol_cap_threshold():
    # threshold is annualized; σ20 daily → *sqrt(252)
    daily_hi = 0.55 / math.sqrt(252) + 0.001    # just above 55% annual
    daily_lo = 0.40 / math.sqrt(252)
    assert _rvol_cap({"und_vol20": daily_hi}) == 0.5
    assert _rvol_cap({"und_vol20": daily_lo}) == 1.0
    assert _rvol_cap({"und_vol20": None}) == 1.0     # missing → no cap


def test_dampener_caps_full_size_bet(monkeypatch):
    monkeypatch.setattr(settings, "duel_rvol_dampen_annual", 0.50)
    hi = _ctx(und_vol20=0.043)          # ≈68% annualized (real 2026-07 SOXX)
    d = decide(hi, entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side == "SOXL" and d.size_factor == 0.5 and d.rvol_damped
    assert any("변동성 감쇠" in r for r in d.reasons)
    lo = _ctx(und_vol20=0.012)
    d2 = decide(lo, entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d2.size_factor == 1.0 and not d2.rvol_damped


def test_dampener_applies_to_adaptive(monkeypatch):
    monkeypatch.setattr(settings, "duel_rvol_dampen_annual", 0.50)
    d = decide_adaptive(_ctx(und_vol20=0.05), 0.70,
                        entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side == "SOXL" and d.size_factor == 0.5 and d.rvol_damped


def test_dampener_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(settings, "duel_rvol_dampen_annual", 0.0)
    d = decide(_ctx(und_vol20=0.09), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.size_factor == 1.0 and not d.rvol_damped


# ── abstain carries entry_ref/atr for later forced promotion ─────────────────
def test_abstain_carries_brackets_material():
    d = decide(_ctx(asia={}), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side == "STAND_ASIDE"
    assert d.entry_ref == 54.85 and d.atr_pct == 0.04    # reusable by force


# ── promote_forced ───────────────────────────────────────────────────────────
def test_promote_forced_builds_directional_half_size():
    d = decide(_ctx(asia={}), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    f = promote_forced(d, PAIR)
    assert f.forced and f.side in ("SOXL", "SOXS") and f.size_factor == 0.5
    assert f.stop_price < f.entry_ref < f.target_price
    assert any("필수매수 제약" in r for r in f.reasons)


def test_promote_forced_direction_follows_score():
    down = decide(_ctx(asia={"TSMC": {"ret": -0.03, "vol": 0.012,
                                      "weight": 0.4}}, und_sma50_dist=-0.02),
                  entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    # weaken so it abstains but keeps a negative score
    down = DuelDecision(date="d", pair_id="soxl_soxs", side="STAND_ASIDE",
                        score=-0.08, conviction=0.08, size_factor=0.0,
                        size_pct=0.0, entry_ref=5.0, atr_pct=0.04)
    f = promote_forced(down, PAIR)
    assert f.side == "SOXS"                 # negative score → bear leg


def test_promote_forced_declines_on_crisis():
    d = decide(_ctx(vix_level=40.0), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side == "STAND_ASIDE" and "위기" in d.abstain_reason
    f = promote_forced(d, PAIR)
    assert not f.forced and f.side == "STAND_ASIDE"    # safety over mandate


# ── session-level mandatory pick ─────────────────────────────────────────────
def test_apply_mandatory_pick_forces_top_conviction(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_mandatory_pick", True)

    def _aside(pid, score):
        return DuelDecision(date="2026-07-20", pair_id=pid, side="STAND_ASIDE",
                            score=score, conviction=abs(score), size_factor=0.0,
                            size_pct=0.0, entry_ref=30.0, atr_pct=0.04,
                            abstain_reason="확신도 부족")
    decided = [({"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"},
                _aside("soxl_soxs", -0.05)),
               ({"id": "tqqq_sqqq", "bull": "TQQQ", "bear": "SQQQ"},
                _aside("tqqq_sqqq", -0.12))]      # highest |score|
    res = dlive.apply_mandatory_pick(decided)
    assert res is not None
    pair, f = res
    assert pair["id"] == "tqqq_sqqq" and f.side == "SQQQ" and f.forced
    with connect(db) as conn:
        row = conn.execute("SELECT side, forced FROM duel_decisions "
                           "WHERE pair='tqqq_sqqq'").fetchone()
    assert row["side"] == "SQQQ" and row["forced"] == 1


def test_mandatory_pick_noop_when_a_call_exists(tmp_path, monkeypatch):
    db = tmp_path / "n.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    live_call = DuelDecision(date="d", pair_id="soxl_soxs", side="SOXL",
                             score=0.4, conviction=0.4, size_factor=1.0,
                             size_pct=0.1)
    aside = DuelDecision(date="d", pair_id="tqqq_sqqq", side="STAND_ASIDE",
                         score=-0.05, conviction=0.05, size_factor=0.0,
                         size_pct=0.0)
    assert dlive.apply_mandatory_pick(
        [(PAIR, live_call), (PAIR, aside)]) is None


def test_mandatory_pick_declines_all_crisis(tmp_path, monkeypatch):
    db = tmp_path / "c.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    crisis = DuelDecision(date="d", pair_id="soxl_soxs", side="STAND_ASIDE",
                          score=0.3, conviction=0.3, size_factor=0.0,
                          size_pct=0.0,
                          abstain_reason="VIX 44 ≥ 35 (위기 변동성 …)")
    assert dlive.apply_mandatory_pick([(PAIR, crisis)]) is None


def test_mandatory_pick_disabled(monkeypatch):
    monkeypatch.setattr(settings, "duel_mandatory_pick", False)
    aside = DuelDecision(date="d", pair_id="soxl_soxs", side="STAND_ASIDE",
                         score=-0.05, conviction=0.05, size_factor=0.0,
                         size_pct=0.0, entry_ref=30.0, atr_pct=0.04)
    assert dlive.apply_mandatory_pick([(PAIR, aside)]) is None


# ── forced-cost tally + migration ────────────────────────────────────────────
def test_forced_tally_separates_forced(tmp_path, monkeypatch):
    from surge.db import upsert as db_upsert

    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", [
            {"pair": "soxl_soxs", "decision_date": "2026-07-01", "side": "SOXL",
             "forced": 1, "correct": 0, "pnl_pct": -0.05,
             "evaluated_at": "y", "captured_at": "x"},
            {"pair": "tqqq_sqqq", "decision_date": "2026-07-01", "side": "TQQQ",
             "forced": 0, "correct": 1, "pnl_pct": 0.03,
             "evaluated_at": "y", "captured_at": "x"},
        ], immutable=("captured_at",))
    t = dlive.forced_tally()
    assert t["forced_n"] == 1 and t["forced_acc"] == 0.0
    assert t["unforced_n"] == 1 and t["unforced_acc"] == 1.0


def test_migration_adds_forced_column(tmp_path):
    import sqlite3

    db = tmp_path / "mig.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE duel_decisions (pair TEXT NOT NULL DEFAULT 'soxl_soxs',"
            " decision_date TEXT NOT NULL, side TEXT NOT NULL, score REAL,"
            " conviction REAL, size_factor REAL, entry_ref REAL, stop_price REAL,"
            " target_price REAL, reasons TEXT, components TEXT, gap_guard REAL,"
            " model TEXT DEFAULT 'champion', entry_fill REAL, exit_fill REAL,"
            " exit_reason TEXT, pnl_pct REAL, soxx_oc_ret REAL, correct INTEGER,"
            " captured_at TEXT NOT NULL, evaluated_at TEXT,"
            " PRIMARY KEY (pair, decision_date))")
        conn.execute("INSERT INTO duel_decisions (pair, decision_date, side,"
                     " captured_at) VALUES ('soxl_soxs','2026-07-01','SOXL','x')")
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM duel_decisions").fetchone()
        assert row["forced"] == 0            # backfilled default
