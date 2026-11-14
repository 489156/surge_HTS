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


def _has_upcoming_catalyst(snap: Mapping, catalysts: list | None) -> bool:
    if snap.get("has_upcoming_catalyst"):
        return True
    if not catalysts:
        return False
    from datetime import date

    ref_raw = snap.get("snapshot_date")
    try:
        ref = date.fromisoformat(str(ref_raw)[:10]) if ref_raw else date.today()
    except ValueError:
        ref = date.today()

    for item in catalysts:
        if isinstance(item, Mapping):
            cdate = item.get("event_date")
            ctype = str(item.get("event_type") or "").lower()
        elif isinstance(item, tuple | list) and len(item) >= 2:
            cdate, ctype = item[0], str(item[1] or "").lower()
        else:
            continue
        if ctype in ("offering", "rsplit", "dilution") or not cdate:
            continue
        try:
            d = date.fromisoformat(str(cdate)[:10])
        except ValueError:
            continue
        if 0 <= (d - ref).days <= 14:
            return True
    return False


def setup_score(
    snap: Mapping, trap: Mapping | None = None, catalysts: list | None = None
) -> tuple[float, list[str]]:
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
    pct = snap.get("pct_change") or 0
    rvol = snap.get("rvol")
    if rvol:
        if rvol >= 5:
            add(2, f"거래량 폭증 RVOL {rvol:.1f}")
        elif rvol >= 3:
            add(1, f"거래량 증가 RVOL {rvol:.1f}")
        elif 1.5 <= rvol < 3 and abs(pct) < 15:
            add(1, f"조용한 매집 RVOL {rvol:.1f}")

    # ── Pre-ignition compression & non-dilution catalyst ───────────────────
    range_pct = snap.get("range_pct")
    if range_pct is not None and 0 < range_pct <= 5.0 and abs(pct) < 15:
        add(1.5, f"변동성 수축(일중폭 {range_pct:.1f}%)")
    if _has_upcoming_catalyst(snap, catalysts):
        add(2.0, "14일내 예정 촉매(비희석)")

    # ── Options positioning ────────────────────────────────────────────────
    if snap.get("opt_has_chain"):
        cpr = snap.get("call_put_ratio")
        if cpr and cpr >= 2:
            add(1, f"콜 편중 C/P {cpr:.1f}")

    # ── Momentum / persistence & post-spike guard ──────────────────────────
    from .config import settings

    if pct >= settings.near_surge_pct:
        add(-2, f"기급등 추격 금지({pct:+.0f}%)")
    if pct < settings.near_surge_pct and (snap.get("consec_up_days") or 0) >= 4:
        add(1, f"{snap['consec_up_days']}일 연속 상승")
    cs = snap.get("close_strength")
    if pct < settings.near_surge_pct and cs is not None and cs >= 0.8:
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
