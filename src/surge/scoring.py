"""Transparent, rule-based surge-setup scorer.

Per the product design, the score is NOT a black box: every point comes with a
plain-language reason, so the user sees *why* a name is on the watchlist. This
ranks the Stage-2 shortlist into a Top-K candidate list — the realistic goal is
to lift the candidate base rate, not to "predict" a single winner.

Signals encode the structural pre-conditions of explosive low-float moves:
low float, squeeze fuel (short interest, rotation), volume pre-heating, options
positioning, momentum persistence — minus the trap filters (offering, exhaustion,
illiquidity) that turn a "catch" into a -EV trade.
"""

from __future__ import annotations

from collections.abc import Mapping


def setup_score(snap: Mapping, trap: Mapping | None = None) -> tuple[float, list[str]]:
    """Return (score, reasons). `snap` is a daily_snapshot row; `trap` a
    trap_flags row (optional). Higher score = stronger surge setup."""
    trap = trap or {}
    score = 0.0
    reasons: list[str] = []

    def add(points: float, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{'+' if points >= 0 else ''}{points:g} {reason}")

    # ── Float (the master lever) ───────────────────────────────────────────
    fl = snap.get("shares_float")
    if fl:
        if fl < 10_000_000:
            add(3, f"극저유동 float {fl/1e6:.1f}M")
        elif fl < 30_000_000:
            add(2, f"저유동 float {fl/1e6:.1f}M")
        elif fl < 50_000_000:
            add(1, f"낮은 float {fl/1e6:.1f}M")

    # ── Squeeze fuel ───────────────────────────────────────────────────────
    sp = snap.get("short_pct_float")
    if sp:
        if sp >= 0.20:
            add(2, f"고공매도 {sp*100:.0f}% of float")
        elif sp >= 0.10:
            add(1, f"공매도 {sp*100:.0f}% of float")
    fr = snap.get("float_rotation")
    if fr and fr >= 1.0:
        add(2, f"float 완전회전 ×{fr:.1f}")

    # ── Volume pre-heating ─────────────────────────────────────────────────
    rvol = snap.get("rvol")
    if rvol:
        if rvol >= 5:
            add(2, f"거래량 폭증 RVOL {rvol:.1f}")
        elif rvol >= 3:
            add(1, f"거래량 증가 RVOL {rvol:.1f}")

    # ── Options positioning ────────────────────────────────────────────────
    if snap.get("opt_has_chain"):
        cpr = snap.get("call_put_ratio")
        if cpr and cpr >= 2:
            add(1, f"콜 편중 C/P {cpr:.1f}")

    # ── Momentum / persistence ─────────────────────────────────────────────
    pct = snap.get("pct_change") or 0
    if 30 <= pct < 100:
        add(1, f"당일 +{pct:.0f}% 모멘텀")
    gap = snap.get("gap_pct") or 0
    if abs(gap) >= 10:
        add(1, f"갭 {gap:+.0f}%")
    if (snap.get("consec_up_days") or 0) >= 4:
        add(1, f"{snap['consec_up_days']}일 연속 상승")
    cs = snap.get("close_strength")
    if cs is not None and cs >= 0.8:
        add(1, "고가권 강한 마감")

    # ── Reverse-split squeeze setup (dual-use; also a trap below) ──────────
    if trap.get("recent_rsplit"):
        add(1, "최근 리버스 스플릿(저유동 셋업)")

    # ── Trap filters (lower probability / -EV) ────────────────────────────
    if trap.get("pending_offering"):
        add(-3, "발행 임박(상단 캡·희석 위험)")
    if trap.get("exhausted"):
        add(-3, "이미 과열(소진 위험)")
    if trap.get("illiquid"):
        add(-2, "유동성 부족(체결 위험)")

    return round(score, 2), reasons


def rank_candidates(
    snaps: list[Mapping],
    traps: Mapping[str, Mapping] | None = None,
    *,
    min_score: float = 1.0,
) -> list[dict]:
    """Score a list of snapshot rows, keep those above min_score, sort desc."""
    traps = traps or {}
    out = []
    for s in snaps:
        score, reasons = setup_score(s, traps.get(s["symbol"]))
        if score >= min_score:
            out.append({"snap": s, "score": score, "reasons": reasons})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out
