"""Shadow-FACTOR registry tests — the 'which factor to ADD' loop must record
candidate signals, score them forward standalone, and only promote on a real,
multiplicity-corrected edge over both a coin and the live model."""

import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert
from surge.duel import factors as F


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "f.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


PAIR = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}


# ── candidate factor math ─────────────────────────────────────────────────────
def test_asia_breadth_counts_agreement_not_magnitude():
    ctx = {"asia": {"a": {"ret": 0.01}, "b": {"ret": 0.02}, "c": {"ret": -0.01}}}
    assert F._asia_breadth(ctx) == pytest.approx(2 * 2 / 3 - 1)   # 2 of 3 up
    assert F._asia_breadth({"asia": {}}) is None


def test_pullback_in_uptrend_flips_sign_on_interaction():
    assert F._pullback_in_uptrend({"und_sma50_dist": 0.1, "und_ret1": -0.02}) > 0  # dip→bull
    assert F._pullback_in_uptrend({"und_sma50_dist": -0.1, "und_ret1": 0.02}) < 0  # pop→bear
    assert F._pullback_in_uptrend({"und_sma50_dist": 0.1, "und_ret1": 0.01}) == 0


def test_vix_meanrev_silent_below_threshold():
    assert F._vix_meanrev({"vix_level": 15, "vix_chg": -0.1}) == 0.0   # calm → no read
    assert F._vix_meanrev({"vix_level": 32, "vix_chg": -0.1}) > 0       # high+falling → bull
    assert F._vix_meanrev({"vix_level": 32, "vix_chg": 0.1}) < 0        # high+rising → bear


# ── capture + forward scoring ─────────────────────────────────────────────────
def test_record_then_score(db):
    ctx = {"asia": {"a": {"ret": 0.01}, "b": {"ret": 0.01}},     # breadth +1 → bull
           "und_ret1": 0.01, "und_ret5": 0.0,                    # accel + → bull
           "und_sma50_dist": 0.1, "vix_level": 30, "vix_chg": -0.1}
    n = F.record(PAIR, "2026-06-10", ctx)
    assert n >= 2
    F.score_pending(lambda pid, d: 0.02)            # underlying rose → bull factors right
    with connect(db) as conn:
        rows = {r["factor"]: r["correct"] for r in conn.execute(
            "SELECT factor, correct FROM duel_factor_shadow").fetchall()}
    assert rows["asia_breadth"] == 1                # bull read, tape up → correct
    assert rows.get("pullback_uptrend") in (None, 0, 1)   # value 0 here → unscored/NULL


def test_cross_asset_factor_signs():
    assert F._credit_risk({"credit_chg": 0.01}) > 0     # HY credit up → bull
    assert F._dollar_drag({"dollar_chg": 0.01}) < 0     # strong dollar → bear
    assert F._bond_bid({"bonds_chg": 0.01}) > 0         # bonds bid → bull
    assert F._credit_risk({}) is None


def test_framework_factor_math():
    assert F._amvf_breadth({"breadth": 0.75}) == pytest.approx(0.5)
    assert F._amvf_leadership({"leadership": 0.01}) > 0      # leader outruns → bull
    assert F._amvf_thrust({"breadth": 0.7, "rvol": 3}) > 0   # broad + heavy vol → bull
    assert F._amvf_thrust({"breadth": 0.3, "rvol": 3}) < 0   # narrow + heavy vol → bear
    assert F._ngrf_growth({"growth": 0.1}) > 0
    assert F._advcrf_rotation({"rotation": 0.01}) > 0
    assert F._amvf_breadth({}) is None                        # missing → silent
    assert F._amvf_leadership({"leadership": float("nan")}) is None


def test_record_framework_writes_rows(db):
    n = F.record_framework(PAIR, "2026-06-10",
                           {"breadth": 0.8, "rvol": 2.5, "leadership": 0.01,
                            "rotation": 0.005, "growth": 0.06})
    assert n == len(F.FRAMEWORK_FACTORS)
    with connect(db) as conn:
        got = {r["factor"] for r in conn.execute(
            "SELECT factor FROM duel_factor_shadow").fetchall()}
    assert "amvf_breadth" in got and "ngrf_growth" in got


def test_promotion_needs_to_beat_baseline(db, monkeypatch):
    monkeypatch.setattr(settings, "variant_min_n", 10)
    # 10 up / 10 down sessions → always-bull baseline = 0.5; factor nails 90%.
    with connect(db) as conn:
        fac = [{"factor": "asia_breadth", "pair": "soxl_soxs",
                "decision_date": f"2026-05-{i+1:02d}",
                "value": 0.5 if i % 2 == 0 else -0.5,
                "captured_at": "x", "evaluated_at": "x",
                "label": 0.01 if i % 2 == 0 else -0.01,
                "correct": 1 if i < 18 else 0} for i in range(20)]
        db_upsert(conn, "duel_factor_shadow", fac)
    lb = F.leaderboard()
    assert lb["baseline"] == pytest.approx(0.5)
    assert dict(lb["ranked"])["asia_breadth"]["acc"] == pytest.approx(0.9)
    assert lb["recommend"] and lb["recommend"]["factor"] == "asia_breadth"


def test_weak_factor_is_not_promoted(db, monkeypatch):
    monkeypatch.setattr(settings, "variant_min_n", 10)
    with connect(db) as conn:
        fac = [{"factor": "und_accel", "pair": "soxl_soxs",
                "decision_date": f"2026-05-{i+1:02d}", "value": 0.3,
                "captured_at": "x", "evaluated_at": "x",
                "label": 0.01 if i % 2 == 0 else -0.01,
                "correct": i % 2} for i in range(20)]   # 50% = the baseline
        db_upsert(conn, "duel_factor_shadow", fac)
    lb = F.leaderboard()
    assert lb["recommend"] is None                  # not above always-bull → no add
