"""Investor risk-discipline self-diagnosis → personalized sizing dampener."""

import json

from surge.config import settings
from surge.dashboard import export
from surge.db import connect, init_db
from surge.db import upsert as db_upsert
from surge.duel import discipline as D
from surge.duel.decide import DuelDecision, decide, decide_adaptive, promote_forced

PAIR = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}


def _ctx(**kw):
    base = {"date": "2026-07-22", "und_ret1": 0.01, "und_ret5": 0.02,
            "und_vol20": 0.015, "und_sma50_dist": 0.03, "vix_level": 16.0,
            "vix_chg": 0.0, "tnx_chg": 0.0, "futures_ret": None,
            "underlying": "SOXX", "pair": PAIR,
            "asia": {"TSMC": {"ret": 0.03, "vol": 0.012, "weight": 0.4}},
            "atr_pct": {"SOXL": 0.04, "SOXS": 0.04}}
    base.update(kw)
    return base


# ── pure factor / ceiling ────────────────────────────────────────────────────
def test_factor_bounds():
    assert D.factor_from_scores([3, 3, 3, 3, 3]) == 1.0            # fully disciplined
    assert D.factor_from_scores([0, 0, 0, 0, 0]) == settings.duel_discipline_floor
    mid = D.factor_from_scores([2, 1, 3, 2, 1])
    assert settings.duel_discipline_floor < mid < 1.0


def test_factor_clamps_out_of_range_scores():
    # scores above 3 or below 0 are clamped, never inflating past 1.0
    assert D.factor_from_scores([9, 9, 9, 9, 9]) == 1.0
    assert D.factor_from_scores([-5, -5, -5, -5, -5]) == settings.duel_discipline_floor


def test_equity_ceiling():
    assert D.equity_ceiling(0.15) == 0.15
    assert D.equity_ceiling(None) is None
    assert D.equity_ceiling(1.9) == 1.0                            # clamped
    assert D.equity_ceiling(-0.2) == 0.0


# ── persistence roundtrip + degrade safety ───────────────────────────────────
def test_record_latest_active_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "d.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    assert D.active_factor() == 1.0 and D.active_ceiling() is None   # none on file
    row = D.record([1, 1, 1, 1, 1], life_share=0.12)
    assert row["source"] == "self"
    assert D.active_factor() == row["factor"] < 1.0
    assert D.active_ceiling() == 0.12
    assert D.summary()["shrinks"] is True


def test_active_factor_degrades_without_table(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"          # a DB file with NO user_discipline table
    db.write_bytes(b"")
    monkeypatch.setattr(settings, "db_path", db)
    assert D.active_factor() == 1.0     # never raises
    assert D.active_ceiling() is None
    assert D.summary() == {}


def test_trajectory_is_ascending(tmp_path, monkeypatch):
    import time

    db = tmp_path / "t.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    D.record([0, 0, 0, 0, 0])
    time.sleep(0.01)
    D.record([3, 3, 3, 3, 3])
    tj = D.trajectory()
    assert len(tj) == 2 and tj[0]["factor"] <= tj[1]["factor"]


# ── sizing hook (decide stays pure — scale is injected) ──────────────────────
def test_decide_size_scale_shrinks_only():
    full = decide(_ctx(), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    scaled = decide(_ctx(), entry_ref={"SOXL": 54.85, "SOXS": 5.0}, size_scale=0.4)
    assert scaled.side == full.side
    assert scaled.size_pct == round(full.size_pct * 0.4, 4)
    assert scaled.conviction == full.conviction        # conviction/direction unchanged
    assert any("리스크 규율 감쇠" in r for r in scaled.reasons)


def test_decide_size_scale_cannot_inflate():
    full = decide(_ctx(), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    up = decide(_ctx(), entry_ref={"SOXL": 54.85, "SOXS": 5.0}, size_scale=2.0)
    assert up.size_pct == full.size_pct                # clamped to 1.0


def test_decide_equity_ceiling_caps():
    d = decide(_ctx(), entry_ref={"SOXL": 54.85, "SOXS": 5.0}, size_ceiling=0.03)
    assert d.size_pct <= 0.03


def test_decide_adaptive_and_forced_respect_scale():
    a = decide_adaptive(_ctx(), 0.70, entry_ref={"SOXL": 54.85, "SOXS": 5.0},
                        size_scale=0.5)
    assert a.size_scale == 0.5 and a.size_pct == round(a.size_factor
                                                       * settings.duel_size_pct * 0.5, 4)
    aside = DuelDecision(date="d", pair_id="soxl_soxs", side="STAND_ASIDE",
                         score=-0.05, conviction=0.05, size_factor=0.0,
                         size_pct=0.0, entry_ref=30.0, atr_pct=0.04)
    f = promote_forced(aside, PAIR, size_scale=0.4)
    assert f.forced and f.size_pct == round(0.5 * settings.duel_size_pct * 0.4, 4)


# ── dashboard surfacing ──────────────────────────────────────────────────────
# ── phase 2: behavioral anchoring ────────────────────────────────────────────
def _seed_calls(db, pairs):
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions",
                  [{"pair": p, "decision_date": "2026-07-21", "side": "X",
                    "size_factor": 1.0, "captured_at": "x"} for p in pairs],
                  immutable=("captured_at",))


def test_record_adherence_computes_vs_recommended(tmp_path, monkeypatch):
    db = tmp_path / "adh.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    _seed_calls(db, ["soxl_soxs"])
    # size_factor 1.0 × duel_size_pct → recommended; deploy 2× that
    r = D.record_adherence("2026-07-21", "soxl_soxs",
                           2 * settings.duel_size_pct)
    assert r["recommended_pct"] == settings.duel_size_pct
    assert r["adherence"] == 2.0


def test_behavioral_none_below_min_n(tmp_path, monkeypatch):
    db = tmp_path / "b.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_discipline_min_n", 3)
    _seed_calls(db, ["a", "b"])
    D.record_adherence("2026-07-21", "a", settings.duel_size_pct)
    D.record_adherence("2026-07-21", "b", settings.duel_size_pct)
    assert D.behavioral_factor() is None            # 2 < 3 samples


def test_behavioral_override_penalises_oversizing(tmp_path, monkeypatch):
    db = tmp_path / "o.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_discipline_min_n", 3)
    _seed_calls(db, ["a", "b", "c"])
    D.record([3, 3, 3, 3, 3])                       # self-report: fully disciplined
    assert D.active_factor() == 1.0
    for p in ("a", "b", "c"):                       # but actually over-sizes 2×
        D.record_adherence("2026-07-21", p, 2 * settings.duel_size_pct)
    b = D.behavioral_factor()
    assert b["n"] == 3 and b["factor"] == 0.5        # 1 / mean_adherence(2.0)
    assert D.active_factor() == 0.5                  # behavioral overrides self
    assert D.summary()["source"] == "behavioral"


def test_behavioral_does_not_penalise_undersizing(tmp_path, monkeypatch):
    db = tmp_path / "u.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "duel_discipline_min_n", 2)
    _seed_calls(db, ["a", "b"])
    for p in ("a", "b"):                             # deploys HALF the recommendation
        D.record_adherence("2026-07-21", p, 0.5 * settings.duel_size_pct)
    assert D.behavioral_factor()["factor"] == 1.0    # under-sizing is not punished


def _embedded_data(html):
    """Pull the window.DATA payload the page renders client-side."""
    frag = html.split("window.DATA = ", 1)[1].split("\n", 1)[0].rstrip(";")
    return json.loads(frag)


def test_dashboard_carries_discipline_when_assessed(tmp_path, monkeypatch):
    db = tmp_path / "dash.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    D.record([1, 2, 1, 1, 2], life_share=0.10)
    data = export.collect()
    assert data["discipline"]["factor"] < 1.0
    assert data["discipline"]["equity_ceiling"] == 0.10
    assert data["discipline"]["trajectory"]                 # ≥1 point
    # the render embeds it in window.DATA for the client-side card
    assert _embedded_data(export.render_html(data))["discipline"]["factor"] < 1.0


def test_dashboard_omits_card_when_no_assessment(tmp_path, monkeypatch):
    db = tmp_path / "none.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    data = export.collect()
    assert data["discipline"] == {}
    assert _embedded_data(export.render_html(data))["discipline"] == {}
