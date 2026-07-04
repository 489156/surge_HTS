"""Rapid-verification tests — deterministic, offline.

The two claims that matter: (1) the accelerators cut sessions-to-verify when a
real edge exists, and (2) they do NOT false-positive on a null (the speedup is
statistical power, not optional-stopping inflation). Both are asserted here.
"""

import random
import statistics as st

import pytest

from surge import verdict
from surge.config import settings
from surge.db import connect, init_db
from surge.duel import verify

THR = 20.0


def _synth(rate, n, seed):
    r = random.Random(seed)
    return [1 if r.random() < rate else 0 for _ in range(n)]


# ── warm-start is a valid e-process (reduces to the existing flat test) ──────
def test_warmstart_reduces_to_flat_when_prior_weight_zero():
    seq = [1, 0, 1, 1, 0, 1, 1, 1, 0, 1] * 6
    a = verify.warmstart_evalue(seq, 0.5, None, 0.0)
    b = verdict.evalue_bernoulli(seq, 0.5)
    assert a == pytest.approx(b, rel=1e-12)
    # a good prior only ever helps (crosses no later); check it never explodes
    warm = verify.warmstart_evalue(seq, 0.5, 0.7, 15.0)
    assert warm > 0


def test_warmstart_empty_and_degenerate_inputs():
    assert verify.warmstart_evalue([], 0.5, 0.6, 15.0) == 0.0
    assert verify.warmstart_evalue([1, 1], None, 0.6, 15.0) == 0.0
    assert verify.warmstart_evalue([1, 1], 1.5, 0.6, 15.0) == 0.0
    # prior_rate out of range falls back to 0.5 (no crash)
    assert verify.warmstart_evalue([1, 0, 1], 0.5, 1.4, 15.0) > 0


def test_sessions_to_cross_none_when_never():
    # a true-coin stream must (almost) never cross 20 — anytime-valid guarantee
    assert verify.sessions_to_cross(_synth(0.5, 200, 1), 0.5, THR, 0.5, 15.0) is None
    # a strong-edge stream crosses, and returns a positive index
    c = verify.sessions_to_cross(_synth(0.75, 400, 1), 0.5, THR, 0.7, 15.0)
    assert c is not None and 0 < c <= 400


# ── HONESTY: null false-positive stays ≤ α, real edge gets power ─────────────
def _pooled(rate, seed, prior_weight, npair=7, n=400):
    r = random.Random(seed)
    streams = [[1 if r.random() < rate else 0 for _ in range(n)]
               for _ in range(npair)]
    pooled = [streams[p][t] for t in range(n) for p in range(npair)]
    return verify.sessions_to_cross(pooled, 0.5, THR, rate, prior_weight)


def test_null_pooled_false_positive_within_alpha():
    """A pooled null (true rate 0.5) must cross ≤ ~α of the time — proof the
    acceleration is power, not optional-stopping inflation."""
    fp = sum(1 for s in range(200) if _pooled(0.50, s, 15.0) is not None)
    assert fp / 200 <= 0.08          # α=0.05 + Monte-Carlo slack


def test_real_edge_pooled_has_power():
    tp = sum(1 for s in range(120) if _pooled(0.55, s, 15.0) is not None)
    assert tp / 120 >= 0.90          # a genuine 0.55 edge should verify


def test_pooling_beats_per_pair_speed():
    """Cross-sectional pooling verifies the family in far fewer CALENDAR
    sessions than a single pair verifies itself, at the same real edge."""
    # single pair, from-flat: median sessions to cross at 0.55
    singles = [verify.sessions_to_cross(_synth(0.55, 3000, s), 0.5, THR, None, 0.0)
               for s in range(40)]
    singles = [x for x in singles if x]
    per_pair_med = st.median(singles)
    # pooled 7 pairs: crossing measured in obs → calendar = obs / 7
    cals = []
    for s in range(40):
        c = _pooled(0.55, s, 15.0, npair=7, n=600)
        if c:
            cals.append(-(-c // 7))
    pooled_med = st.median(cals)
    assert pooled_med * 3 < per_pair_med   # at least a 3× calendar speedup


# ── pooled_paired_evalue: interleaving + discordant filtering ────────────────
def test_pooled_paired_evalue_uses_discordant_only():
    # pair A: strat always right, baseline always wrong → all discordant wins
    a = [(f"2026-01-{i:02d}", 1, 0) for i in range(1, 21)]
    # pair B: strat == baseline every day → NO discordant sessions contributed
    b = [(f"2026-01-{i:02d}", 1, 1) for i in range(1, 21)]
    e_both = verify.pooled_paired_evalue([a, b], 0.5, 0.0)
    e_a = verify.pooled_paired_evalue([a], 0.5, 0.0)
    assert e_both == pytest.approx(e_a)     # concordant pair adds nothing
    assert e_both > THR                      # 20 discordant wins → decisive


def test_pooled_paired_evalue_empty():
    assert verify.pooled_paired_evalue([], 0.5, 0.0) == 0.0
    assert verify.pooled_paired_evalue([[("d", 1, 1)]], 0.5, 0.0) == 0.0


# ── DB-backed status/confidence shape ────────────────────────────────────────
def _seed_forward(db, pair, sides_labels):
    """Insert adaptive shadow rows (variant='adaptive') with realized labels."""
    from surge.db import upsert as db_upsert

    rows = []
    for i, (side, label) in enumerate(sides_labels):
        rows.append({
            "variant": "adaptive", "pair": pair,
            "decision_date": f"2026-02-{i+1:02d}", "side": side,
            "score": 0.2, "conviction": 0.2, "label": label, "correct": 1,
            "captured_at": "x", "evaluated_at": "y"})
    with connect(db) as conn:
        db_upsert(conn, "duel_variants", rows, immutable=("captured_at",))


def test_pair_and_family_confidence_from_db(tmp_path, monkeypatch):
    db = tmp_path / "v.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)

    # SOXL always called correctly vs an up-drifting label (strat beats majority
    # only on the down days — construct a clear discordant edge)
    seq = []
    for i in range(40):
        up = i % 3 != 0                      # ~2/3 up days (majority = up)
        # strat: right every day; on down days it disagrees with majority
        side = "SOXL" if up else "SOXS"
        seq.append((side, 0.01 if up else -0.01))
    _seed_forward(db, "soxl_soxs", seq)

    pc = verify.pair_confidence("soxl_soxs")
    assert pc["n_forward"] == 40
    assert pc["n_discordant"] > 0            # down days are discordant with majority
    assert pc["e_warm"] > 0

    fam = verify.family_confidence()
    assert fam["n_pairs"] == 1 and fam["e_pooled"] > 0

    s = verify.status()
    assert "family" in s and len(s["pairs"]) == len(__import__(
        "surge.duel.pairs", fromlist=["PAIRS"]).PAIRS)


def test_status_empty_db_is_graceful(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    s = verify.status()
    assert s["family"]["e_pooled"] == 0.0
    assert all(p["n_forward"] == 0 for p in s["pairs"])
    assert not s["family"]["verified"]
