"""Duel decision — side, abstain, brackets, sizing.

Both legs are LONG (SOXL = bull, SOXS = bear), so brackets are always long-side:
stop below entry, target above, time-exit at the close (no overnight 3x).
Abstention is a first-class output: low conviction or crisis volatility means
the EV-correct trade is no trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
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

    @property
    def reasons(self) -> list[str]:
        out = [f"{c.name} {c.value:+.2f}×{c.weight:g}: {c.note}"
               for c in self.components]
        if self.abstain_reason:
            out.append(f"기권 사유: {self.abstain_reason}")
        return out


def _size_factor(conviction: float) -> float:
    if conviction >= 0.35:
        return 1.0
    if conviction >= settings.duel_abstain_threshold:
        return 0.5
    return 0.0


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
        return DuelDecision(date=date, pair_id=pid, side="STAND_ASIDE", score=score,
                            conviction=conviction, size_factor=0.0, size_pct=0.0,
                            components=comps,
                            abstain_reason=f"확신도 {conviction:.2f} < "
                                           f"{settings.duel_abstain_threshold:g}"
                                           " (신호 불충분 — 관망이 +EV)")

    side = pair["bull"] if score > 0 else pair["bear"]
    atr = (ctx.get("atr_pct") or {}).get(side)
    ref = (entry_ref or {}).get(side)
    stop = target = None
    if ref and atr:
        stop = round(ref * (1 - settings.duel_stop_atr * atr), 4)
        target = round(ref * (1 + settings.duel_target_atr * atr), 4)

    return DuelDecision(
        date=date, pair_id=pid, side=side, score=score, conviction=conviction,
        size_factor=sf, size_pct=round(settings.duel_size_pct * sf, 4),
        entry_ref=ref, stop_price=stop, target_price=target, atr_pct=atr,
        components=comps,
    )
