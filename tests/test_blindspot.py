"""Blind-spot loop tests — 관망 원인 진단 + fill 변인, deterministic/offline."""

import json

from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert
from surge.duel import blindspot
from surge.duel.factors import CANDIDATE_FACTORS

# ── classification taxonomy ──────────────────────────────────────────────────
def _comp(name, value, weight=0.15):
    return {"name": name, "value": value, "weight": weight}


def test_classify_silent_when_few_reads():
    comps = [_comp("trend", 0.9), _comp("momentum_5d", 0.1),
             _comp("mean_reversion", 0.0)]
    assert blindspot.classify(comps) == "SILENT"          # 3 < MIN_PRESENT


def test_classify_conflict_when_strong_reads_cancel():
    comps = [_comp("asia_lead", -0.5, 0.35), _comp("trend", 1.0, 0.15),
             _comp("momentum_5d", 0.0), _comp("vix_regime", 0.1),
             _comp("rates", 0.0, 0.10), _comp("mean_reversion", 0.0, 0.10)]
    assert blindspot.classify(comps) == "CONFLICT"


def test_classify_weak_when_everything_small():
    comps = [_comp(n, v) for n, v in (
        ("asia_lead", 0.05), ("trend", 0.1), ("momentum_5d", -0.05),
        ("vix_regime", 0.2), ("rates", 0.0), ("mean_reversion", 0.0))]
    assert blindspot.classify(comps) == "WEAK"


def test_classify_crisis_from_reason():
    assert blindspot.classify([], ["기권 사유: VIX 44 ≥ 35 (위기 변동성 — …)"]) \
        == "CRISIS"


def test_crisis_has_no_fill_by_design():
    assert blindspot.FILLS["CRISIS"] == ()


# ── diagnose(): would-have from the always-committing shadow ─────────────────
def test_diagnose_measures_would_have(tmp_path, monkeypatch):
    db = tmp_path / "b.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    conflict = [_comp("asia_lead", -0.5, 0.35), _comp("trend", 1.0, 0.15),
                _comp("momentum_5d", 0.0), _comp("vix_regime", 0.1),
                _comp("rates", 0.0, 0.10), _comp("mean_reversion", 0.0, 0.10)]
    with connect(db) as conn:
        for i, would in enumerate([1, 1, 0]):           # shadow: 2/3 correct
            date = f"2026-07-{10+i:02d}"
            db_upsert(conn, "duel_decisions", [{
                "pair": "soxl_soxs", "decision_date": date,
                "side": "STAND_ASIDE", "score": 0.02, "conviction": 0.02,
                "components": json.dumps(conflict), "reasons": json.dumps([]),
                "soxx_oc_ret": 0.01, "captured_at": "x", "evaluated_at": "y",
            }], immutable=("captured_at",))
            db_upsert(conn, "duel_variants", [{
                "variant": "champion", "pair": "soxl_soxs",
                "decision_date": date, "side": "SOXL", "score": 0.02,
                "conviction": 0.02, "label": 0.01, "correct": would,
                "captured_at": "x", "evaluated_at": "y",
            }], immutable=("captured_at",))
    d = blindspot.diagnose()
    assert d["n_abstained"] == 3
    b = d["causes"]["CONFLICT"]
    assert b["n"] == 3 and b["would_n"] == 3
    assert b["would_acc"] == (2 / 3)
    assert b["fills"] == ["conflict_asia_tiebreak"]

    r = blindspot.report()
    assert r["n_abstained"] == 3 and "fill_records" in r


# ── fill factors fire ONLY on their blind-spot populations ───────────────────
def _ctx(**kw):
    base = {"date": "2026-07-15", "und_ret1": 0.0, "und_ret5": 0.0,
            "und_vol20": 0.02, "und_sma50_dist": 0.0, "vix_level": 16.0,
            "vix_chg": 0.0, "tnx_chg": 0.0, "futures_ret": None,
            "underlying": "SOXX", "asia": {}, "und_gap1": 0.01}
    base.update(kw)
    return base


def test_weak_drift_fires_only_on_weak():
    weak = _ctx(asia={"TSMC": {"ret": 0.0005, "vol": 0.012, "weight": 0.4}})
    assert CANDIDATE_FACTORS["weak_drift"](weak) == 0.4
    assert CANDIDATE_FACTORS["conflict_asia_tiebreak"](weak) is None
    assert CANDIDATE_FACTORS["silent_gap_follow"](weak) is None


def test_conflict_tiebreak_follows_asia():
    conflict = _ctx(und_sma50_dist=0.06,                    # trend +1.0
                    asia={"TSMC": {"ret": -0.012, "vol": 0.012, "weight": 0.4}})
    v = CANDIDATE_FACTORS["conflict_asia_tiebreak"](conflict)
    assert v is not None and v < 0                          # follows the Asia side
    assert CANDIDATE_FACTORS["weak_drift"](conflict) is None


def test_silent_gap_follow_fires_when_desks_dark():
    silent = _ctx(asia={}, vix_level=None, vix_chg=None, tnx_chg=None)
    v = CANDIDATE_FACTORS["silent_gap_follow"](silent)
    assert v is not None and v > 0                          # follows +gap
    assert CANDIDATE_FACTORS["weak_drift"](silent) is None


# ── SELF-GENERATION: 재발 패턴 → 새 변인 자동 등록 ────────────────────────────
def _seed_conflict_archive(db, n, gap_predicts=True):
    """n CONFLICT abstains with archived ctx where und_gap1 (anti-)predicts."""
    conflict = [_comp("asia_lead", -0.5, 0.35), _comp("trend", 1.0, 0.15),
                _comp("momentum_5d", 0.0), _comp("vix_regime", 0.1),
                _comp("rates", 0.0, 0.10), _comp("mean_reversion", 0.0, 0.10)]
    with connect(db) as conn:
        for i in range(n):
            date = f"2026-06-{i+1:02d}"
            label = 0.01 if i % 2 == 0 else -0.01
            gap = label if gap_predicts else -label   # follow vs inverted
            db_upsert(conn, "duel_decisions", [{
                "pair": "soxl_soxs", "decision_date": date,
                "side": "STAND_ASIDE", "score": 0.02, "conviction": 0.02,
                "components": json.dumps(conflict), "reasons": json.dumps([]),
                "soxx_oc_ret": label, "captured_at": "x", "evaluated_at": "y",
            }], immutable=("captured_at",))
            db_upsert(conn, "duel_live_context", [{
                "pair": "soxl_soxs", "decision_date": date,
                "ctx": json.dumps({"und_gap1": gap, "und_vol20": 0.02}),
                "captured_at": "x",
            }], immutable=("captured_at",))


def test_generate_fills_registers_and_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "g.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    _seed_conflict_archive(db, 20, gap_predicts=True)

    new = blindspot.generate_fills()
    assert "bs_conflict_gap_follow" in new            # direct read screened in
    spec = blindspot.discovered_fills()["bs_conflict_gap_follow"]
    assert spec["cause"] == "CONFLICT" and not spec["invert"]
    assert spec["screen_acc"] >= 0.58
    assert blindspot.generate_fills() == []           # idempotent


def test_generate_fills_inversion_rule(tmp_path, monkeypatch):
    """An anti-predictive template is itself a hypothesis, backwards —
    mirrors learn.propose_challengers."""
    db = tmp_path / "gi.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    _seed_conflict_archive(db, 20, gap_predicts=False)

    new = blindspot.generate_fills()
    assert "bs_conflict_gap_follow_inv" in new
    assert blindspot.discovered_fills()["bs_conflict_gap_follow_inv"]["invert"]


def test_generate_fills_respects_sample_floor(tmp_path, monkeypatch):
    db = tmp_path / "gf.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    _seed_conflict_archive(db, blindspot.SCREEN_MIN_N - 2)
    assert blindspot.generate_fills() == []           # too few → no generation


def test_discovered_fill_races_and_fires_on_cause_only(tmp_path, monkeypatch):
    db = tmp_path / "gr.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    _seed_conflict_archive(db, 20, gap_predicts=True)
    blindspot.generate_fills()

    from surge.duel.factors import all_factors
    af = all_factors()
    assert "bs_conflict_gap_follow" in af             # merged into the race
    conflict_ctx = _ctx(und_sma50_dist=0.06,
                        asia={"TSMC": {"ret": -0.012, "vol": 0.012,
                                       "weight": 0.4}})
    v = af["bs_conflict_gap_follow"](conflict_ctx)
    assert v is not None and v > 0                    # follows +gap on CONFLICT
    weak_ctx = _ctx(asia={"TSMC": {"ret": 0.0005, "vol": 0.012, "weight": 0.4}})
    assert af["bs_conflict_gap_follow"](weak_ctx) is None   # silent off-cause
    # inverted spec flips the read
    assert blindspot.eval_discovered(
        "x", {"cause": "CONFLICT", "template": "gap_follow", "invert": True},
        conflict_ctx) < 0
    # fill_records covers discovered names once scored rows exist
    assert set(blindspot.FILLS["CONFLICT"]) <= set(af)


def test_daily_report_includes_blindspot(tmp_path, monkeypatch):
    db = tmp_path / "d.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    from surge.daily import run_daily

    report = run_daily(write=False)
    assert "blindspot" in report
