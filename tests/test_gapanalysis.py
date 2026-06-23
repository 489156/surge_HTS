"""Gap-cause analysis tests (offline, seeded DB)."""

import json

import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert
from surge.duel import gapanalysis as ga


# ── classify (pure taxonomy) ─────────────────────────────────────────────────
def _comps(**kv):
    return [{"name": k, "value": v, "weight": 0.2} for k, v in kv.items()]


def test_classify_gap_absorption():
    causes = ga.classify(
        side="SOXL", correct=0, pnl_pct=-0.02, exit_reason="close", score=0.4, conviction=0.4,
        gap_ret=0.012, label=-0.008, comps=_comps(asia_lead=0.8))
    assert any(c.startswith("갭 선반영") for c in causes)
    assert any("주범 신호: asia_lead" in c for c in causes)


def test_classify_whipsaw_right_direction_stop_loss():
    causes = ga.classify(
        side="SOXL", correct=1, pnl_pct=-0.04, exit_reason="stop", score=0.4, conviction=0.4,
        gap_ret=0.0, label=0.01, comps=_comps(trend=0.9))
    assert any(c.startswith("휩쏘 스탑아웃") for c in causes)


def test_classify_low_conviction_and_culprit():
    causes = ga.classify(
        side="SOXL", correct=0, pnl_pct=-0.01, exit_reason="close", score=0.16, conviction=0.16,
        gap_ret=-0.001, label=-0.01,
        comps=_comps(futures=0.9, vix_regime=-0.2))
    assert any("주범 신호: futures" in c for c in causes)
    assert any("저확신" in c for c in causes)


def test_classify_abstain_opportunity():
    causes = ga.classify(
        side="STAND_ASIDE", correct=None, pnl_pct=None, exit_reason=None, score=0.05, conviction=0.05,
        gap_ret=None, label=0.021, comps=[])
    assert any("관망" in c and "큰 움직임 놓침" in c for c in causes)
    causes2 = ga.classify(
        side="STAND_ASIDE", correct=None, pnl_pct=None, exit_reason=None, score=0.05, conviction=0.05,
        gap_ret=None, label=0.001, comps=[])
    assert any("관망 정당" in c for c in causes2)


def test_classify_correct_names_carrier():
    causes = ga.classify(
        side="SOXL", correct=1, pnl_pct=0.03, exit_reason="close", score=0.4, conviction=0.4,
        gap_ret=0.0, label=0.015, comps=_comps(asia_lead=0.7, rates=-0.1))
    assert any("적중 견인: asia_lead" in c for c in causes)


# ── parsing fallback ─────────────────────────────────────────────────────────
def test_parse_components_structured_and_regex():
    structured = {"components": json.dumps(
        [{"name": "trend", "value": 0.5, "weight": 0.15}]), "reasons": None}
    assert ga.parse_components(structured)[0]["name"] == "trend"

    legacy = {"components": None, "reasons": json.dumps(
        ["asia_lead +0.01×0.35: 아시아 반도체 선행: TSMC -0.2%",
         "기권 사유: 확신도 0.12 < 0.15"])}
    parsed = ga.parse_components(legacy)
    assert parsed == [{"name": "asia_lead", "value": 0.01, "weight": 0.35}]


# ── end-to-end analyze with seeded DB ────────────────────────────────────────
@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "g.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def test_analyze_aggregates(db):
    with connect(db) as conn:
        # underlying history for the gap computation (SOXX gap up then fade)
        db_upsert(conn, "price_history", [
            {"symbol": "SOXX", "date": "2026-06-09", "open": 100.0, "high": 101,
             "low": 99, "close": 100.0, "volume": 1, "captured_at": "x"},
            {"symbol": "SOXX", "date": "2026-06-10", "open": 101.5, "high": 102,
             "low": 100, "close": 100.7, "volume": 1, "captured_at": "x"},
        ])
        db_upsert(conn, "duel_decisions", [{
            "pair": "soxl_soxs", "decision_date": "2026-06-10", "side": "SOXL",
            "score": 0.4, "conviction": 0.4, "size_factor": 1.0,
            "components": json.dumps([
                {"name": "asia_lead", "value": 0.8, "weight": 0.35},
                {"name": "vix_regime", "value": -0.3, "weight": 0.15}]),
            "reasons": "[]", "captured_at": "x", "evaluated_at": "x",
            "soxx_oc_ret": -0.0079, "correct": 0, "pnl_pct": -0.025,
            "exit_reason": "close",
        }])
    res = ga.analyze("soxl_soxs")
    assert res["n_bets"] == 1 and res["n_wrong"] == 1
    assert res["gap_absorbed"] == 1        # score +, gap +1.5%, label −0.79%
    call = res["calls"][0]
    assert call["gap_ret"] == pytest.approx(0.015)
    assert any("갭 선반영" in c for c in call["causes"])
    # component sign accuracy: asia_lead wrong (agree 0/1), vix right (1/1)
    acc = res["component_accuracy"]
    assert acc["asia_lead"]["rate"] == 0.0
    assert acc["vix_regime"]["rate"] == 1.0