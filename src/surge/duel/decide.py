"""Duel decision — side, abstain, brackets, sizing.

Both legs are LONG (SOXL = bull, SOXS = bear), so brackets are always long-side:
stop below entry, target above, time-exit at the close (no overnight 3x).
Abstention is a first-class output: low conviction or crisis volatility means
the EV-correct trade is no trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import settings
from . import volstate
from .signals import Component, compute_signal


@dataclass
class DuelDecision:
    date: str
    side: str                 # bull leg | bear leg | "STAND_ASIDE"
    score: float
    conviction: float
    size_factor: float        # 0 / 0.5 / 1.0
    size_pct: float           # effective notional fraction of equity
    pair_id: str = "soxl_soxs"
    entry_ref: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    atr_pct: float | None = None
    components: list[Component] = field(default_factory=list)
    abstain_reason: str | None = None
    # Committed-at-evening, executed-at-open condition: if the underlying's
    # open gap in the call's direction is ≥ this (return units), DO NOT enter
    # (the signal is already pre-priced). None = no guard.
    gap_guard: float | None = None
    model: str = "champion"   # which engine produced this call (ledger honesty)
    # The adaptive engine's calibrated P(up) for this session (set at call
    # time for the card's conviction-with-evidence line; not persisted — the
    # shadow variant row carries it into the forward ledger).
    shadow_prob: float | None = None
    rvol_damped: bool = False   # size capped by underlying realized vol
    forced: bool = False        # mandatory-pick override (would have abstained)
    vol_state: float = 0.0      # leading vol-regime stress ∈ [0,1] (volstate.py)

    @property
    def reasons(self) -> list[str]:
        out = [f"{c.name} {c.value:+.2f}×{c.weight:g}: {c.note}"
               for c in self.components]
        if self.gap_guard is not None and self.side != "STAND_ASIDE":
            out.append(f"갭 가드: 시가 갭이 콜 방향으로 {self.gap_guard*100:+.2f}%"
                       " 이상이면 진입 취소(선반영)")
        if self.rvol_damped:
            out.append("변동성 감쇠: 기초 실현변동성 높음 → 사이즈 절반으로 제한")
        if self.rvol_damped and self.vol_state >= settings.duel_volstate_dampen:
            out.append(f"선행 변동성 레짐 감쇠: vol_state {self.vol_state:.2f} "
                       f"≥ {settings.duel_volstate_dampen:g} "
                       "(VIX 기간구조 백워데이션·σ5/σ20 가속·SKEW)")
        if self.forced:
            out.append("필수매수 제약: 오늘 밤 최고확신 종목으로 강제 선정"
                       " (관망이 +EV였음 — 별도 채점)")
        if self.model != "champion":
            out.append(f"모델: {self.model}")
        if self.abstain_reason:
            out.append(f"기권 사유: {self.abstain_reason}")
        return out


def _size_factor(conviction: float) -> float:
    if conviction >= 0.35:
        return 1.0
    if conviction >= settings.duel_abstain_threshold:
        return 0.5
    return 0.0


def _rvol_cap(ctx: dict) -> float:
    """Max size factor allowed by the underlying's realized volatility (a risk
    layer independent of the VIX crisis abstain). 1.0 = no cap; 0.5 when the
    annualized σ20 is at/above the dampen threshold."""
    thr = settings.duel_rvol_dampen_annual
    vol = ctx.get("und_vol20")
    if thr <= 0 or not vol:
        return 1.0
    annual = float(vol) * math.sqrt(252)
    return 0.5 if annual >= thr else 1.0


def _brackets(ctx: dict, side: str,
              entry_ref: dict[str, float] | None) -> tuple:
    atr = (ctx.get("atr_pct") or {}).get(side)
    ref = (entry_ref or {}).get(side)
    stop = target = None
    if ref and atr:
        stop = round(ref * (1 - settings.duel_stop_atr * atr), 4)
        target = round(ref * (1 + settings.duel_target_atr * atr), 4)
    return ref, stop, target, atr


def decide(ctx: dict, entry_ref: dict[str, float] | None = None,
           mult: dict[str, float] | None = None) -> DuelDecision:
    """`entry_ref`: optional {leg: reference price} (live last price or open).
    `mult`: active champion multipliers (None = base weights)."""
    sig = compute_signal(ctx, mult)
    score, conviction = sig["score"], sig["conviction"]
    comps = sig["components"]
    date = ctx["date"]
    pair = ctx.get("pair") or {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}
    pid = pair["id"]

    # Crisis regime: a 3x product in panic vol is gambling — abstain outright.
    vix = ctx.get("vix_level")
    if vix is not None and vix >= settings.duel_crisis_vix:
        return DuelDecision(date=date, pair_id=pid, side="STAND_ASIDE", score=score,
                            conviction=conviction, size_factor=0.0, size_pct=0.0,
                            components=comps,
                            abstain_reason=f"VIX {vix:.0f} ≥ {settings.duel_crisis_vix:g}"
                                           " (위기 변동성 — 3배 레버리지 베팅 금지)")

    sf = _size_factor(conviction)
    if sf == 0.0:
        # entry_ref/atr are populated even on abstain so a session-level
        # mandatory pick can promote this to a directional call without refetch
        aside_side = pair["bull"] if score > 0 else pair["bear"]
        ref, stop, target, atr = _brackets(ctx, aside_side, entry_ref)
        return DuelDecision(date=date, pair_id=pid, side="STAND_ASIDE", score=score,
                            conviction=conviction, size_factor=0.0, size_pct=0.0,
                            entry_ref=ref, stop_price=stop, target_price=target,
                            atr_pct=atr, components=comps,
                            abstain_reason=f"확신도 {conviction:.2f} < "
                                           f"{settings.duel_abstain_threshold:g}"
                                           " (신호 불충분 — 관망이 +EV)")

    cap = min(_rvol_cap(ctx), volstate.vol_state_cap(ctx))
    sf = min(sf, cap)
    side = pair["bull"] if score > 0 else pair["bear"]
    ref, stop, target, atr = _brackets(ctx, side, entry_ref)

    return DuelDecision(
        date=date, pair_id=pid, side=side, score=score, conviction=conviction,
        size_factor=sf, size_pct=round(settings.duel_size_pct * sf, 4),
        entry_ref=ref, stop_price=stop, target_price=target, atr_pct=atr,
        components=comps, gap_guard=_gap_guard(ctx), rvol_damped=cap < 1.0,
        vol_state=volstate.vol_state(ctx),
    )


def _gap_guard(ctx: dict) -> float | None:
    """Guard threshold in RETURN units (z·σ20 of the underlying), or None."""
    z = settings.duel_gap_guard_z
    vol = ctx.get("und_vol20")
    if z <= 0 or not vol:
        return None
    return round(z * float(vol), 5)


def guard_triggered(side: str, pair: dict, gap_guard: float | None,
                    gap_ret: float | None) -> bool:
    """Mechanical open-time check of the committed condition: the realized open
    gap already covers ≥ the guard IN the call's direction → do not enter."""
    if gap_guard is None or gap_ret is None or side == "STAND_ASIDE":
        return False
    bullish = side == pair["bull"]
    return (gap_ret > 0) == bullish and abs(gap_ret) >= gap_guard


def decide_adaptive(ctx: dict, prob_up: float,
                    entry_ref: dict[str, float] | None = None,
                    components: list[Component] | None = None) -> DuelDecision:
    """Decision from the walk-forward learner's CALIBRATED P(up). Conviction is
    |2p−1| — a probability, not a vote sum — so the bands mean what they say
    (the static champion's bands demonstrably inverted). Crisis-VIX abstain and
    the gap guard apply unchanged."""
    date = ctx["date"]
    pair = ctx.get("pair") or {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}
    pid = pair["id"]
    score = 2.0 * prob_up - 1.0
    conviction = abs(score)
    comps = components or []

    vix = ctx.get("vix_level")
    if vix is not None and vix >= settings.duel_crisis_vix:
        return DuelDecision(date=date, pair_id=pid, side="STAND_ASIDE",
                            score=score, conviction=conviction, size_factor=0.0,
                            size_pct=0.0, components=comps, model="adaptive",
                            abstain_reason=f"VIX {vix:.0f} ≥ "
                                           f"{settings.duel_crisis_vix:g}"
                                           " (위기 변동성 — 3배 레버리지 베팅 금지)")

    if conviction < settings.duel_adaptive_band:
        aside_side = pair["bull"] if score > 0 else pair["bear"]
        ref, stop, target, atr = _brackets(ctx, aside_side, entry_ref)
        return DuelDecision(date=date, pair_id=pid, side="STAND_ASIDE",
                            score=score, conviction=conviction, size_factor=0.0,
                            size_pct=0.0, entry_ref=ref, stop_price=stop,
                            target_price=target, atr_pct=atr, components=comps,
                            model="adaptive",
                            abstain_reason=f"P(상승) {prob_up:.1%} — 엣지 "
                                           f"|2p−1| {conviction:.2f} < "
                                           f"{settings.duel_adaptive_band:g}"
                                           " (관망이 +EV)")
    cap = min(_rvol_cap(ctx), volstate.vol_state_cap(ctx))
    sf = min(1.0 if conviction >= settings.duel_adaptive_full else 0.5, cap)

    side = pair["bull"] if score > 0 else pair["bear"]
    ref, stop, target, atr = _brackets(ctx, side, entry_ref)

    return DuelDecision(
        date=date, pair_id=pid, side=side, score=score, conviction=conviction,
        size_factor=sf, size_pct=round(settings.duel_size_pct * sf, 4),
        entry_ref=ref, stop_price=stop, target_price=target, atr_pct=atr,
        components=comps, gap_guard=_gap_guard(ctx), model="adaptive",
        rvol_damped=cap < 1.0, vol_state=volstate.vol_state(ctx),
    )


def promote_forced(d: DuelDecision, pair: dict) -> DuelDecision:
    """Mandatory-pick override: turn an abstained decision into a directional
    half-size call. Direction = sign of the (still-computed) vote score; size
    is half, further capped by the realized-vol dampener; brackets reuse the
    entry_ref/atr the abstain already carried. Flagged `forced` so the ledger
    scores it apart from genuine (unforced) calls — the constraint's cost is
    measured, not hidden. A CRISIS abstain is NOT promoted (safety wins over
    the mandate: 3x in a VIX>crisis panic stays no-trade)."""
    if d.abstain_reason and "위기 변동성" in d.abstain_reason:
        return d
    side = pair["bull"] if d.score >= 0 else pair["bear"]
    sf = 0.5                 # forced picks are half size (the constraint is a
    #                          bet you didn't want — never full leverage)
    stop = target = None
    if d.entry_ref and d.atr_pct:
        stop = round(d.entry_ref * (1 - settings.duel_stop_atr * d.atr_pct), 4)
        target = round(d.entry_ref * (1 + settings.duel_target_atr * d.atr_pct), 4)
    return DuelDecision(
        date=d.date, pair_id=d.pair_id, side=side, score=d.score,
        conviction=d.conviction, size_factor=sf,
        size_pct=round(settings.duel_size_pct * sf, 4),
        entry_ref=d.entry_ref, stop_price=stop, target_price=target,
        atr_pct=d.atr_pct, components=d.components, model=d.model,
        gap_guard=d.gap_guard, rvol_damped=d.rvol_damped, forced=True,
        shadow_prob=d.shadow_prob, vol_state=d.vol_state,
    )
