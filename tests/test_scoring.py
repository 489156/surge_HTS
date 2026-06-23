from surge.scoring import rank_candidates, setup_score


def test_low_float_high_short_scores_high():
    snap = {
        "symbol": "AAA", "shares_float": 5_000_000, "short_pct_float": 0.25,
        "float_rotation": 1.5, "rvol": 6, "pct_change": 45,
    }
    score, reasons = setup_score(snap)
    # 3 (float) + 2 (short) + 2 (rotation) + 2 (rvol) + 1 (momentum) = 10
    assert score == 10
    assert any("극저유동" in r for r in reasons)


def test_traps_penalize():
    snap = {"symbol": "BBB", "shares_float": 5_000_000}  # +3
    trap = {"pending_offering": 1, "exhausted": 1}       # -3 -3
    score, reasons = setup_score(snap, trap)
    assert score == -3
    assert any("발행 임박" in r for r in reasons)


def test_reasons_are_transparent():
    snap = {"symbol": "CCC", "rvol": 4}
    score, reasons = setup_score(snap)
    assert score == 1
    assert reasons == ["+1 거래량 증가 RVOL 4.0"]


def test_rank_orders_and_filters():
    snaps = [
        {"symbol": "HI", "shares_float": 5_000_000, "rvol": 6},   # high
        {"symbol": "LO", "rvol": 1},                              # below min_score
        {"symbol": "MID", "shares_float": 40_000_000},           # +1
    ]
    ranked = rank_candidates(snaps, min_score=1.0)
    syms = [r["snap"]["symbol"] for r in ranked]
    assert syms[0] == "HI"
    assert "LO" not in syms
    assert ranked[0]["score"] > ranked[-1]["score"]
