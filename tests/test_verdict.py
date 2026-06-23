"""Truth-gate tests — the verdict logic must be statistically honest."""

import pytest

from surge import verdict as V
from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "v.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    monkeypatch.setattr(V, "MIN_N", 30)
    return path


def _seed_duel(conn, n, wins, up):
    """n scored bets: `wins` correct, `up` days the underlying rose."""
    rows = []
    for i in range(n):
        rows.append({
            "pair": "soxl_soxs", "decision_date": f"2026-01-{i+1:02d}",
            "side": "SOXL", "score": 0.3, "captured_at": "x", "evaluated_at": "x",
            "soxx_oc_ret": 0.01 if i < up else -0.01,
            "correct": 1 if i < wins else 0,
        })
    db_upsert(conn, "duel_decisions", rows)


def test_insufficient_data_below_min_n(db):
    with connect(db) as conn:
        _seed_duel(conn, n=10, wins=9, up=5)     # great acc but tiny n
    d = V._duel()
    assert d["n"] == 10
    assert d["mark"] == "🔴"                      # n < MIN_N → no verdict


def test_no_edge_when_not_beating_baseline(db):
    # 40 bets, 60% correct, but the underlying rose 60% of days → baseline 60%.
    with connect(db) as conn:
        _seed_duel(conn, n=40, wins=24, up=24)
    d = V._duel()
    assert d["n"] == 40
    assert round(d["baseline"], 2) == 0.60       # always-bull baseline
    assert d["excess"] <= 0
    assert d["mark"] == "🟡"                      # accuracy == baseline → no edge


def test_real_edge_beats_baseline_significantly(db):
    # 60 bets, 90% correct, underlying rose only 50% → strong excess.
    with connect(db) as conn:
        _seed_duel(conn, n=60, wins=54, up=30)
    d = V._duel()
    assert d["excess"] > 0.3
    assert d["z"] >= 1.64
    assert d["mark"] == "🟢"


def test_headline_no_edge_when_empty(db):
    h = V.headline()
    assert "검증된 엣지: 없음" in h               # nothing scored → honest no


def test_signal_grade_requires_all_five(db):
    import datetime as _dt

    # 67% over n=201 vs a 0.5 baseline, spread across months, both halves > 0.5,
    # net +ve → the ANYTIME-VALID e-value clears the threshold AND every guard holds
    # → ⭐ signal. (A 60% sequence would NOT cross the peek-safe e-value — correctly
    # harder than the old daily-peeked CI.)
    outcomes = [1, 1, 0] * 67                 # 67% win rate, interleaved (n=201)
    n = len(outcomes)
    dates = [(_dt.date(2026, 1, 1) + _dt.timedelta(days=i)).isoformat()
             for i in range(n)]               # spans ~6 months → many ISO weeks
    ok, checks = V._signal_grade(outcomes, dates, base_p=0.5, net=0.03)
    assert ok
    assert all(c["ok"] for c in checks)

    # Same edge but tiny n → SAMPLE/EVIDENCE fail → not a signal.
    ok2, _ = V._signal_grade(outcomes[:20], dates[:20], base_p=0.5, net=0.03)
    assert not ok2

    # Big n but negative net-of-cost (won on direction, lost on slippage) → fails.
    ok3, checks3 = V._signal_grade(outcomes, dates, base_p=0.5, net=-0.01)
    assert not ok3
    assert next(c for c in checks3 if "순익" in c["k"])["ok"] is False

    # DECAYED edge — great first half, dead second half → split-half STABILITY
    # fails even though the overall rate looks fine. This is the quality guard
    # that replaced the slow 3-month calendar floor.
    decayed = [1] * 100 + [0] * 100         # 50% overall, but front-loaded
    ok4, checks4 = V._signal_grade(decayed, dates, base_p=0.4, net=0.03)
    assert not ok4
    assert next(c for c in checks4 if "전·후반" in c["k"])["ok"] is False


def test_edge_is_not_yet_a_signal_without_cost_data(db):
    # 60 bets, 90% correct → 🟢 edge, but net-of-cost is unmeasured (no pnl) →
    # the ECONOMIC condition can't be confirmed → NOT ⭐ (conservative).
    with connect(db) as conn:
        _seed_duel(conn, n=60, wins=54, up=30)
    d = V._duel()
    assert d["mark"] == "🟢"          # statistically an edge
    assert d["signal"] is False        # …but not yet a signal


def test_evalue_anytime_valid_properties():
    e = V.evalue_bernoulli
    assert e([], 0.5) == 0.0                  # empty
    assert e([1, 1], None) == 0.0             # no baseline
    assert e([1] * 5, 1.0) == 0.0             # invalid p0 guarded
    assert e([1] * 8, 0.5) >= 20              # 8 straight wins → decisive evidence
    assert e([1, 0] * 100, 0.5) < 2.0         # a coin → no evidence (e≈1, far from 20)
    assert e([0] * 50 + [1] * 10, 0.5) < 2.0  # one-sided: a BELOW-baseline run accrues none
    # a noise sequence at the null must (almost surely) stay below 1/α — the
    # peek-safety guarantee (Ville): this is why daily monitoring is honest.
    import random
    random.seed(7)
    crossed = sum(e([1 if random.random() < 0.5 else 0 for _ in range(300)], 0.5) >= 20
                  for _ in range(50))
    assert crossed <= 3                        # ≤ ~α·N false alarms


def test_evalue_returns_continuous():
    er = V.evalue_returns
    assert er([]) == 0.0
    assert er([0.05] * 200) >= 20             # steady positive mean → decisive
    assert er([0.03] * 200) > 10              # a smaller mean still accumulates strongly
    assert er([0.0] * 200) < 2.0              # zero mean → ≈1, no evidence
    assert er([-0.02] * 200) < 2.0            # one-sided: a negative mean accrues none
    # fat-tail honesty: one +500% lottery among small losses must NOT fake a mean-edge
    assert er([5.0] + [-0.05] * 199) < 2.0
    # Type-I peek-safety under the null (mean 0): rarely crosses 1/α over many seeds
    import random
    random.seed(11)
    crossed = sum(er([random.gauss(0, 0.05) for _ in range(300)]) >= 20 for _ in range(50))
    assert crossed <= 4                       # ~α·N false alarms (Ville)


def test_paired_evalue_mcnemar():
    pe = V.paired_evalue
    assert pe([1] * 10, [0] * 10) >= 20       # strategy wins every discordant pair
    assert pe([1, 0, 1, 0], [1, 0, 1, 0]) == 0.0   # no discordant pairs → no evidence
    assert pe([0] * 10, [1] * 10) < 2.0       # strategy loses every pair → none (one-sided)


def test_gate_is_binary_not_diluted_by_weak_diagnostics(db):
    # F1 regression guard: the EVIDENCE gate is the single BINARY e-value. A strong
    # binary edge must clear it even when the continuous/paired DIAGNOSTICS are weak —
    # an earlier 'average the tracks' gate let a weak track drag a strong edge below
    # threshold, making ⭐ harder. The gate value must equal the binary value, not a mean.
    import datetime as _dt

    outcomes = [1, 1, 0] * 67                       # 67% → binary e well over 20
    n = len(outcomes)
    dates = [(_dt.date(2026, 1, 1) + _dt.timedelta(days=i)).isoformat()
             for i in range(n)]
    d = {"n": n, "mark": "🟢", "verdict": "x"}
    out = V._finish(d, outcomes, dates, base_p=0.5, net=0.03, returns=[0.0] * n)
    binary_alone = V.evalue_bernoulli(outcomes, 0.5)
    assert abs(out["evalue"] - round(binary_alone, 2)) < 0.01   # gate == binary, NOT averaged
    assert out["evalue_ret"] < 2.0                              # weak continuous diagnostic…
    assert out["signal"] is True                                # …does not block graduation


def test_paired_evalue_rejects_length_mismatch():
    import pytest as _pt
    with _pt.raises(ValueError):
        V.paired_evalue([1, 0, 1], [1, 0])         # F6: silent zip-truncation forbidden


def test_assess_covers_all_strategies(db):
    rows = V.assess()
    names = [r["strategy"] for r in rows]
    assert any("duel" in n for n in names)
    assert any("rotation" in n for n in names)
    assert any("surge" in n for n in names)
    for r in rows:                               # every row carries a verdict mark
        assert r["mark"] in ("🟢", "🟡", "🔴", "⛔")
