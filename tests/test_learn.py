"""Learning-core tests — the promotion gate must reject noise and the evolution
step must turn anti-predictive components into raceable hypotheses."""

import json

import pytest

from surge import learn
from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "l.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


# ── significance primitives ──────────────────────────────────────────────────
def test_sidak_raises_the_bar_with_more_variants():
    # one comparison → unchanged; seven → meaningfully stricter (≈2.4 not 1.64)
    assert learn.corrected_z(1.64, 1) == pytest.approx(1.64)
    z7 = learn.corrected_z(1.64, 7)
    assert z7 > 2.3
    assert learn.corrected_z(1.64, 20) > z7        # monotonic in k


def test_gate_blocks_below_min_n():
    g = learn.gate(20, 20, 10, 20, base_p=0.5, k=1, min_n=30)
    assert not g.promote and "표본 부족" in g.reason


def test_gate_requires_beating_baseline_not_just_champion():
    # Challenger 62% vs a coin-flip champion 50% on big n → beats CHAMPION,
    # but the always-bull baseline is also 62% → no real edge → must NOT promote.
    g = learn.gate(chal_wins=124, chal_n=200, champ_wins=100, champ_n=200,
                   base_p=0.62, k=1, min_n=30)
    assert g.z_vs_champ > 1.64                      # it does beat champion
    assert not g.promote                            # …but not the baseline
    assert "기준선" in g.reason


def test_gate_promotes_only_a_genuine_edge():
    # 75% over n=200 vs 50% champion AND vs 0.5 baseline → clears even corrected z
    g = learn.gate(chal_wins=150, chal_n=200, champ_wins=100, champ_n=200,
                   base_p=0.5, k=7, min_n=30)
    assert g.promote
    assert g.z_vs_champ >= g.z_required and g.z_vs_base >= g.z_required


def test_gate_refuses_when_no_baseline():
    g = learn.gate(150, 200, 100, 200, base_p=None, k=1, min_n=30)
    assert not g.promote and "기준선 산출 불가" in g.reason


def test_multiple_testing_correction_blocks_a_marginal_winner():
    # z≈1.9 beats champion at the naive 1.64 bar, but NOT at the k=7 corrected
    # bar — exactly the false promote the old loop allowed.
    base = learn.gate(118, 200, 100, 200, base_p=0.5, k=1, min_n=30)
    corrected = learn.gate(118, 200, 100, 200, base_p=0.5, k=7, min_n=30)
    assert base.z_vs_champ == corrected.z_vs_champ
    assert corrected.z_required > base.z_required
    assert base.promote and not corrected.promote   # correction flips it to reject


# ── evolution: discover anti-signal hypotheses from forward diagnostics ───────
def _seed_calls(conn, n, comp_value, label_sign):
    rows = []
    for i in range(n):
        rows.append({
            "pair": "soxl_soxs", "decision_date": f"2026-01-{i+1:02d}",
            "side": "SOXL", "score": 0.2, "captured_at": "x", "evaluated_at": "x",
            "soxx_oc_ret": 0.02 * label_sign,
            "components": json.dumps([
                {"name": "badsig", "value": comp_value, "weight": 0.2},
                {"name": "goodsig", "value": label_sign * 0.5, "weight": 0.2},
            ]),
        })
    db_upsert(conn, "duel_decisions", rows)


def _seed_component(conn, comp, n, correct):
    """n calls, tape always UP; `comp` votes up (correct) on `correct` of them →
    sign accuracy == correct/n. Lets a test target a precise accuracy band."""
    rows = []
    for i in range(n):
        rows.append({
            "pair": "soxl_soxs", "decision_date": f"2026-02-{i+1:02d}",
            "side": "SOXL", "score": 0.2, "captured_at": "x", "evaluated_at": "x",
            "soxx_oc_ret": 0.02,
            "components": json.dumps(
                [{"name": comp, "value": 0.5 if i < correct else -0.5,
                  "weight": 0.2}]),
        })
    db_upsert(conn, "duel_decisions", rows)


def test_component_sign_accuracy_and_proposal(db):
    # 'badsig' always points UP while the tape goes DOWN → 0% sign accuracy →
    # strongly anti-predictive → proposer emits an INVERT hypothesis. 'goodsig'
    # tracks the tape → left alone.
    with connect(db) as conn:
        _seed_calls(conn, n=40, comp_value=0.8, label_sign=-1)
    acc = learn.component_sign_accuracy()
    assert acc["badsig"]["acc"] == 0.0
    assert acc["goodsig"]["acc"] == 1.0
    proposals = learn.propose_challengers()
    assert proposals["disc_inv_badsig"] == {"badsig": -1.0}
    assert "disc_inv_goodsig" not in proposals       # a good signal is left alone
    assert "disc_drop_goodsig" not in proposals


def test_near_coin_component_is_dropped_not_inverted(db):
    # 42.5% sign accuracy: barely anti-predictive → inverting would over-claim it
    # is reliably backwards; the calibrated remedy is DROP (weight 0).
    with connect(db) as conn:
        _seed_component(conn, "midsig", n=40, correct=17)   # 0.425
    proposals = learn.propose_challengers()
    assert proposals == {"disc_drop_midsig": {"midsig": 0.0}}


def test_proposal_deduped_against_existing_static_variant(db):
    # momentum_5d at 10% acc would invert to {momentum_5d:-1.0} — but the static
    # 'inv_momentum' variant already IS that map, so register must NOT add a
    # behaviourally identical disc_ twin (would waste a row + inflate Šidák k).
    with connect(db) as conn:
        _seed_component(conn, "momentum_5d", n=40, correct=4)   # 0.10
    fresh = learn.register_discovered()
    assert "disc_inv_momentum_5d" not in fresh
    assert "disc_inv_momentum_5d" not in learn.discovered_variants()


def test_register_discovered_is_idempotent(db):
    with connect(db) as conn:
        _seed_calls(conn, n=40, comp_value=0.8, label_sign=-1)
    first = learn.register_discovered()
    assert "disc_inv_badsig" in first
    again = learn.register_discovered()              # second run adds nothing new
    assert again == {}
    assert "disc_inv_badsig" in learn.discovered_variants()
