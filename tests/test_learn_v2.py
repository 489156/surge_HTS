import json
import pytest
from surge import learn
from surge.config import settings
from surge.db import connect, init_db
from surge.rotation import engine as rot_engine


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    path = tmp_path / "test_v2.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def test_culprit_rates_and_challengers(test_db):
    """Test culprit detection in wrong bets and hypothesis generation."""
    with connect() as conn:
        # Seed duel decisions where trend was the culprit in incorrect calls
        rows = [
            ("soxl_soxs", f"2026-06-{i:02d}", "SOXL", 0.5, 0.5, "eod", -0.02, -0.02, 0,
             json.dumps([{"name": "trend", "value": 1.0, "weight": 0.2},
                         {"name": "futures", "value": -0.5, "weight": 0.2}]),
             "2026-06-01T00:00:00Z")
            for i in range(1, 25)
        ]
        conn.executemany("""
            INSERT INTO duel_decisions
            (pair, decision_date, side, score, conviction, exit_reason, pnl_pct,
             soxx_oc_ret, correct, components, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

    rates = learn.component_culprit_rates(min_n=10)
    assert "trend" in rates
    assert rates["trend"] == 1.0  # 100% of wrong calls had trend as culprit

    proposals = learn.propose_culprit_challengers(min_rate=0.15)
    assert "disc_culprit_drop_trend" in proposals
    assert proposals["disc_culprit_drop_trend"] == {"trend": 0.0, "vix_regime": 1.5}
    assert "disc_culprit_inv_trend" in proposals
    assert proposals["disc_culprit_inv_trend"] == {"trend": -1.0, "futures": 1.5}


def test_conflict_challengers(test_db):
    """Test hypothesis proposal when asia_lead and futures conflict."""
    with connect() as conn:
        # Seed sessions where asia_lead > 0 and futures < 0, and market fell (soxx_oc_ret < 0)
        # So futures was right and asia_lead was wrong. Distinct dates.
        rows = [
            ("soxl_soxs", f"2026-07-{i:02d}", "SOXL", 0.5, 0.5,
             json.dumps([{"name": "asia_lead", "value": 1.0}, {"name": "futures", "value": -1.0}]),
             -0.02, "2026-07-01T00:00:00Z")
            for i in range(1, 21)
        ]
        conn.executemany("""
            INSERT INTO duel_decisions (pair, decision_date, side, score, conviction, components, soxx_oc_ret, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

    proposals = learn.propose_conflict_challengers(min_n=15)
    assert "disc_conflict_trust_futures" in proposals
    assert proposals["disc_conflict_trust_futures"] == {"futures": 2.0, "asia_lead": 0.3}


def test_rotation_challengers_and_registration(test_db):
    """Test Korean rotation hypothesis proposal and registration."""
    with connect() as conn:
        # Seed rotation decisions where smart_money had strong positive correlation
        rows = [
            ("2026-07-01", f"00{i:04d}", "Test", "ai_memory_hbm", "node1", 1, 0.8, 1,
             0.05, 1, json.dumps({"smart_money": 1.0, "momentum": -0.5}), "2026-07-01T00:00:00Z")
            for i in range(20)
        ]
        conn.executemany("""
            INSERT INTO rotation_decisions
            (decision_date, ticker, name, chain, node, back_steps, score, passed_filter, ret_t5, hit_t5, components, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

    proposals = learn.propose_rotation_challengers(min_n=15)
    assert "disc_kr_boost_smart_money" in proposals
    assert proposals["disc_kr_boost_smart_money"] == {"smart_money": 2.5}

    # Test registration into model_state
    added = learn.register_rotation_discovered(min_n=15)
    assert "disc_kr_boost_smart_money" in added

    # Verify rotation engine all_variants sees it
    all_rot = rot_engine.all_variants()
    assert "disc_kr_boost_smart_money" in all_rot
    assert all_rot["disc_kr_boost_smart_money"] == {"smart_money": 2.5}


def test_pruning_stale_discovered_variants(test_db):
    """Test pruning underperforming discovered variants to control family-wise error."""
    with connect() as conn:
        # Set existing discovered variants
        conn.execute("""
            INSERT INTO model_state (key, value, updated_at)
            VALUES ('discovered_variants', ?, '2026-07-01T00:00:00Z')
        """, (json.dumps({
            "disc_loser": {"trend": -1.0},
            "disc_winner": {"futures": 2.0}
        }),))

        # Seed duel_variants with scores: disc_loser has 5 wins out of 40 (p=0.125, z << -0.5)
        # disc_winner has 30 wins out of 40 (p=0.75, z >> 0)
        loser_rows = [("disc_loser", "soxl_soxs", f"2026-08-{i:02d}", 1 if i <= 5 else 0) for i in range(1, 41)]
        winner_rows = [("disc_winner", "soxl_soxs", f"2026-08-{i:02d}", 1 if i <= 30 else 0) for i in range(1, 41)]
        conn.executemany("""
            INSERT INTO duel_variants (variant, pair, decision_date, correct, score, conviction, side, captured_at)
            VALUES (?, ?, ?, ?, 0.5, 0.5, 'SOXL', '2026-08-01T00:00:00Z')
        """, loser_rows + winner_rows)

    pruned = learn.prune_stale_discovered(min_evals=35, z_cutoff=-0.5)
    assert "disc_loser" in pruned
    assert "disc_winner" not in pruned

    # Check retained variants
    current = learn.discovered_variants()
    assert "disc_loser" not in current
    assert "disc_winner" in current
