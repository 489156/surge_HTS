from surge.reversion import rank_reversions, reversion_score


def test_big_pop_weak_close_scores_high():
    # +120% day that closed weak off the high + exhausted + offering
    snap = {
        "symbol": "FADE", "pct_change": 120, "close_strength": 0.1,
        "float_rotation": 8, "rvol": 25,
    }
    trap = {"exhausted": 1, "pending_offering": 1}
    score, reasons = reversion_score(snap, trap)
    # 3 (pop) + 2 (weak close) + 1 (rotation) + 1 (climax vol) + 2 (exhausted) + 2 (offering)
    assert score == 11
    assert any("폭등" in r for r in reasons)


def test_strong_close_pop_scores_lower():
    # popped but closed strong (held) → far less fade signal
    snap = {"symbol": "HOLD", "pct_change": 60, "close_strength": 0.95}
    score, _ = reversion_score(snap)
    assert score == 2  # just the +50% magnitude tier


def test_gap_fade_signal():
    snap = {"symbol": "GAP", "pct_change": 35, "gap_pct": 20,
            "open": 10.0, "close": 9.0}  # gapped up then closed red
    score, reasons = reversion_score(snap)
    assert any("갭 소멸" in r for r in reasons)


def test_rank_excludes_non_popped():
    snaps = [
        {"symbol": "POP", "pct_change": 120, "close_strength": 0.1},  # fade cand
        {"symbol": "FLAT", "pct_change": 5},                          # not popped
    ]
    ranked = rank_reversions(snaps, min_score=2.0)
    syms = [r["snap"]["symbol"] for r in ranked]
    assert syms == ["POP"]
