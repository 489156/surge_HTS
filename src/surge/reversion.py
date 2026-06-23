"""Reversion (fade) scorer — the empirically-supported edge.

The 2026-06-05 live test showed ignition prediction fails (0/16 hit +100%), but
already-popped low-float names reliably FADE the next day (mean −3.8%; the top
ignition pick EDHL spiked +17% intraday then closed flat). This module scores
*already-popped* names by how likely they are to reverse down tomorrow — a
"fade / avoid / short-watch" list — using blow-off, distribution, exhaustion,
and dilution signals.

It is the mirror of `scoring.setup_score`: setup_score ranks *pre-ignition*
candidates; reversion_score ranks *post-ignition* fade candidates. Kept
rule-based and transparent for the same reasons.
"""

from __future__ import annotations

from collections.abc import Mapping

from .config import settings


def reversion_score(snap: Mapping, trap: Mapping | None = None) -> tuple[float, list[str]]:
    """Return (score, reasons). Only meaningful for names that already popped
    (pct_change >= near_surge_pct); higher = more likely to reverse down."""
    trap = trap or {}
    score = 0.0
    reasons: list[str] = []

    def add(points: float, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"+{points:g} {reason}")

    pct = snap.get("pct_change") or 0
    # Magnitude of the pop — the further it ran, the more mean-reversion pull.
    if pct >= 100:
        add(3, f"당일 +{pct:.0f}% 폭등(되돌림 위험 큼)")
    elif pct >= 50:
        add(2, f"당일 +{pct:.0f}% 급등")
    elif pct >= settings.near_surge_pct:
        add(1, f"당일 +{pct:.0f}% 상승")

    # Distribution: a big up day that closed weak off its high = sellers in control.
    cs = snap.get("close_strength")
    if pct >= settings.near_surge_pct and cs is not None and cs <= 0.3:
        add(2, "고점 대비 약한 마감(분산/블로우오프)")

    # Gap-up that already faded intraday (close < open after a big gap).
    gap = snap.get("gap_pct") or 0
    op, clp = snap.get("open"), snap.get("close")
    if gap >= 15 and op and clp and clp < op:
        add(1, f"갭업 +{gap:.0f}% 후 음봉(갭 소멸)")

    # Demand exhaustion: extreme float rotation / climax volume.
    fr = snap.get("float_rotation")
    if fr and fr >= 5:
        add(1, f"극단 float 회전 ×{fr:.0f}(수요 소진)")
    rvol = snap.get("rvol")
    if rvol and rvol >= 20 and pct >= settings.near_surge_pct:
        add(1, f"클라이맥스 거래량 RVOL {rvol:.0f}")

    # Structural fade pressure.
    if trap.get("exhausted"):
        add(2, "다일 누적 과열(소진)")
    if trap.get("pending_offering"):
        add(2, "급등 중 발행 임박(희석 압력)")

    return round(score, 2), reasons


def rank_reversions(
    snaps: list[Mapping],
    traps: Mapping[str, Mapping] | None = None,
    *,
    min_score: float = 2.0,
) -> list[dict]:
    """Score already-popped names and return fade candidates sorted desc."""
    traps = traps or {}
    out = []
    for s in snaps:
        if (s.get("pct_change") or 0) < settings.near_surge_pct:
            continue  # only post-pop names are reversion candidates
        score, reasons = reversion_score(s, traps.get(s["symbol"]))
        if score >= min_score:
            out.append({"snap": s, "score": score, "reasons": reasons})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out
