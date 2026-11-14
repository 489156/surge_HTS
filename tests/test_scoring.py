from surge.scoring import rank_candidates, setup_score


def test_low_float_high_short_scores_high():
    snap = {
        "symbol": "AAA", "shares_float": 5_000_000, "short_pct_float": 0.25,
        "float_rotation": 1.5, "rvol": 6, "pct_change": 45,
    }
    score, reasons = setup_score(snap)
    # 3 (float) + 2 (short) + 2 (rotation) + 2 (rvol) - 2 (post-spike chase) = 7.0
    assert score == 7.0
    assert any("극저유동" in r for r in reasons)
    assert any("추격 금지" in r for r in reasons)


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


def test_pre_ignition_compression_and_catalyst_bonus():
    snap = {
        "symbol": "COIL", "snapshot_date": "2026-06-10",
        "shares_float": 5_000_000, "rvol": 2.0, "pct_change": 2.5, "range_pct": 3.0,
    }
    cats_ok = [{"event_date": "2026-06-15", "event_type": "earnings", "detail": "Q2"}]
    score, reasons = setup_score(snap, catalysts=cats_ok)
    # 3 (float) + 1 (quiet rvol 2.0) + 1.5 (range contraction <=5%) + 2 (catalyst <=14d)
    assert score == 7.5
    assert any("변동성 수축" in r for r in reasons)
    assert any("예정 촉매" in r for r in reasons)

    cats_dilute = [{"event_date": "2026-06-15", "event_type": "offering", "detail": "S-3"}]
    score_no_cat, _ = setup_score(snap, catalysts=cats_dilute)
    assert score_no_cat == 5.5
