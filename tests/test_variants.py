"""Shadow-variant A/B engine tests (offline, seeded DB)."""

import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.duel import variants as V


def _comps(**kv):
    return [{"name": k, "value": v, "weight": 0.15} for k, v in kv.items()]


# ── pure scoring ─────────────────────────────────────────────────────────────
def test_champion_reproduces_weighted_average():
    comps = _comps(trend=0.5, momentum_5d=-0.8)
    assert V.score_variant(comps, {}) == pytest.approx((0.5 - 0.8) / 2)


def test_drop_and_invert_multipliers():
    comps = _comps(trend=0.5, momentum_5d=-0.8)
    assert V.score_variant(comps, {"momentum_5d": 0.0}) == pytest.approx(0.5)
    # invert the momentum vote → both now positive
    assert V.score_variant(comps, {"momentum_5d": -1.0}) == pytest.approx(0.65)


def test_only_subset_via_star_default():
    comps = _comps(trend=0.9, vix_regime=-0.4, futures=0.6)
    # vix_futures keeps only vix+futures
    got = V.score_variant(comps, V.VARIANTS["vix_futures"])
    assert got == pytest.approx((-0.4 + 0.6) / 2)


def test_side_for_commits_direction():
    assert V.side_for(0.01, "BULL", "BEAR") == "BULL"
    assert V.side_for(-0.01, "BULL", "BEAR") == "BEAR"
    assert V.side_for(0.0, "BULL", "BEAR") == "BULL"   # ties → bull


# ── persistence + eval ───────────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "v.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def test_capture_and_score_pending(db):
    pair = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}
    comps = _comps(trend=0.9, momentum_5d=-0.9)  # champion≈0; drop_mom strongly +
    n = V.capture(pair, "2026-06-10", comps)
    assert n == len(V.VARIANTS)

    # underlying fell that day → bull calls wrong, bear calls right
    V.score_pending(lambda pid, d: -0.02)
    with connect(db) as conn:
        rows = {r["variant"]: r for r in conn.execute(
            "SELECT variant, side, correct FROM duel_variants").fetchall()}
    # drop_momentum scored +0.9 → SOXL (bull) → wrong on a down day
    assert rows["drop_momentum"]["side"] == "SOXL"
    assert rows["drop_momentum"]["correct"] == 0
    # inv_momentum: trend +0.9, momentum flipped +0.9 → +0.9 → bull → wrong too
    assert rows["inv_momentum"]["correct"] == 0


def test_active_promotion_roundtrip(db):
    assert V.active_variant_name() == "champion"
    assert V.active_multipliers() == {}
    V.set_active("drop_momentum")
    assert V.active_variant_name() == "drop_momentum"
    assert V.active_multipliers() == {"momentum_5d": 0.0}
    with pytest.raises(KeyError):
        V.set_active("nope")
    V.set_active("champion")
    assert V.active_multipliers() == {}


def test_leaderboard_promotion_gate(db, monkeypatch):
    monkeypatch.setattr(settings, "variant_min_n", 10)
    monkeypatch.setattr(settings, "variant_promote_z", 1.64)
    pair = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}
    # 20 days: 'drop_momentum' nails direction, champion is a coin flip
    for i in range(20):
        up = i % 2 == 0
        comps = _comps(trend=(0.9 if up else -0.9),     # drop_mom follows reality
                       momentum_5d=(-0.9 if up else 0.9))  # champion ~cancels → noisy
        V.capture(pair, f"2026-05-{i+1:02d}", comps)
        V.score_pending(lambda pid, d, up=up: 0.02 if up else -0.02)
    lb = V.leaderboard()
    accs = dict((n, s["acc"]) for n, s in lb["ranked"])
    assert accs["drop_momentum"] == 1.0          # perfect directional skill
    assert accs["champion"] == pytest.approx(0.5)  # coin flip
    assert lb["recommend"] is not None
    # recommend the strongest challenger that clears the gate, beating champion
    assert accs[lb["recommend"]["variant"]] > accs["champion"]
    assert lb["recommend"]["z"] >= 1.64


def test_backfill_from_components(db):
    import json
    from surge.db import upsert as db_upsert

    pair_id = "soxl_soxs"
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", [{
            "pair": pair_id, "decision_date": "2026-06-10", "side": "STAND_ASIDE",
            "score": 0.05, "captured_at": "x", "evaluated_at": "x",
            "soxx_oc_ret": 0.015,
            "components": json.dumps(_comps(trend=0.8, momentum_5d=-0.2)),
        }])
    made = V.backfill()
    assert made == 1
    with connect(db) as conn:
        scored = conn.execute(
            "SELECT COUNT(*) n FROM duel_variants WHERE correct IS NOT NULL"
        ).fetchone()["n"]
    assert scored == len(V.VARIANTS)   # every variant scored against label +1.5%


def test_set_active_discovered_and_auto_manage_promote_rollback(db, monkeypatch):
    import json as _json
    from datetime import date, timedelta

    with connect(db) as conn:
        conn.execute(
            "INSERT INTO model_state (key, value, updated_at) VALUES "
            "('discovered_variants', ?, 'x') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_json.dumps({"disc_test1": {"*": 1.0, "asia": 2.0}}),),
        )
    # 1. set_active now permits discovered variants
    V.set_active("disc_test1")
    assert V.active_variant_name() == "disc_test1"

    # 2. Trailing window lag vs champion triggers automatic rollback
    monkeypatch.setattr(settings, "variant_rollback_window", 10)
    monkeypatch.setattr(settings, "variant_rollback_margin", 0.05)
    start = date(2026, 6, 1)
    rows = []
    for i in range(12):
        d = (start + timedelta(days=i)).isoformat()
        rows.append({
            "pair": "soxl_soxs", "decision_date": d, "variant": "champion",
            "side": "SOXL", "score": 0.5, "label": 0.02, "correct": 1,
            "captured_at": "x", "evaluated_at": "x",
        })
        rows.append({
            "pair": "soxl_soxs", "decision_date": d, "variant": "disc_test1",
            "side": "SOXS", "score": -0.5, "label": 0.02, "correct": 0,
            "captured_at": "x", "evaluated_at": "x",
        })
    from surge.db import upsert as db_upsert
    with connect(db) as conn:
        db_upsert(conn, "duel_variants", rows)

    rb = V.auto_manage()
    assert rb["action"] == "rollback"
    assert rb["to"] == "champion"
    assert V.active_variant_name() == "champion"

    # 3. Clear and seed winning challenger over baseline -> auto-promote when n >= floor
    monkeypatch.setattr(settings, "variant_auto_promote", True)
    monkeypatch.setattr(settings, "variant_auto_min_n", 40)
    monkeypatch.setattr(settings, "variant_min_n", 40)
    with connect(db) as conn:
        conn.execute("DELETE FROM duel_variants")
        win_rows = []
        for i in range(45):
            d = (start + timedelta(days=i)).isoformat()
            lab = 0.02 if i % 2 == 0 else -0.02
            win_rows.append({
                "pair": "soxl_soxs", "decision_date": d, "variant": "champion",
                "side": "SOXL", "score": 0.2, "label": lab,
                "correct": 1 if lab > 0 else 0,
                "captured_at": "x", "evaluated_at": "x",
            })
            win_rows.append({
                "pair": "soxl_soxs", "decision_date": d, "variant": "disc_test1",
                "side": "SOXL" if lab > 0 else "SOXS",
                "score": 0.5 if lab > 0 else -0.5, "label": lab, "correct": 1,
                "captured_at": "x", "evaluated_at": "x",
            })
        db_upsert(conn, "duel_variants", win_rows)

    pr = V.auto_manage(min_n=40)
    assert pr["action"] == "promote"
    assert pr["to"] == "disc_test1"
    assert V.active_variant_name() == "disc_test1"

    # 4. Append 10 trailing losses so cumulative record still wins, but trailing lags:
    # must rollback to champion and NOT ping-pong re-promote on next call.
    with connect(db) as conn:
        trail = []
        for i in range(45, 55):
            d = (start + timedelta(days=i)).isoformat()
            trail.append({
                "pair": "soxl_soxs", "decision_date": d, "variant": "champion",
                "side": "SOXL", "score": 0.2, "label": 0.02, "correct": 1,
                "captured_at": "x", "evaluated_at": "x",
            })
            trail.append({
                "pair": "soxl_soxs", "decision_date": d, "variant": "disc_test1",
                "side": "SOXS", "score": -0.2, "label": 0.02, "correct": 0,
                "captured_at": "x", "evaluated_at": "x",
            })
        db_upsert(conn, "duel_variants", trail)

    rb2 = V.auto_manage(min_n=40)
    assert rb2["action"] == "rollback" and rb2["to"] == "champion"
    noop = V.auto_manage(min_n=40)
    assert noop["action"] == "noop" and V.active_variant_name() == "champion"
