"""Duel signal engine — a transparent weighted vote.

Each component maps the context to a value in [-1, +1] (positive = semis up →
SOXL) with a fixed weight and a human-readable note. The final score is the
weight-renormalized average over the components that are PRESENT (an Asian
holiday simply removes that component instead of polluting the vote).

No black box: the full component breakdown ships with every decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

WEIGHTS = {
    "asia_lead": 0.35,     # the structural time-zone edge
    "trend": 0.15,         # SOXX vs 50d MA
    "momentum_5d": 0.15,   # recent direction persistence
    "vix_regime": 0.15,    # risk appetite level + change
    "rates": 0.10,         # 10y yield impulse (semis = long duration)
    "mean_reversion": 0.10,  # fade only statistically extreme prior days
    "futures": 0.20,       # live-only overlay (NQ futures); absent in backtest
}


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class Component:
    name: str
    value: float        # -1..+1
    weight: float
    note: str


def asia_lead(asia: dict) -> Component | None:
    """Vol-normalized, weight-averaged same-day return of the Asian leaders."""
    if not asia:
        return None
    num = den = 0.0
    parts = []
    for name, d in asia.items():
        z = d["ret"] / max(d["vol"], 1e-6)
        v = _clip(math.tanh(z / 1.5))
        num += v * d["weight"]
        den += d["weight"]
        parts.append(f"{name} {d['ret']*100:+.1f}%")
    value = num / den
    return Component("asia_lead", value, WEIGHTS["asia_lead"],
                     "아시아 반도체 선행: " + ", ".join(parts))


def trend(sma50_dist: float, und: str = "기초지수") -> Component:
    v = _clip(sma50_dist / 0.05)  # ±5% from the 50d MA = full signal
    return Component("trend", v, WEIGHTS["trend"],
                     f"{und} 50일선 대비 {sma50_dist*100:+.1f}%")


def momentum_5d(ret5: float, vol20: float, und: str = "기초지수") -> Component:
    z = ret5 / max(vol20 * math.sqrt(5), 1e-6)
    v = _clip(math.tanh(z / 1.5))
    return Component("momentum_5d", v, WEIGHTS["momentum_5d"],
                     f"{und} 5일 모멘텀 {ret5*100:+.1f}%")


def vix_regime(level: float | None, chg: float | None) -> Component | None:
    if level is None:
        return None
    lvl = _clip((20.0 - level) / 10.0)          # VIX 20 neutral; 10→+1, 30→−1
    spike = _clip(-(chg or 0.0) / 0.10)         # +10% VIX jump = full −1
    v = _clip(0.5 * lvl + 0.5 * spike)
    return Component("vix_regime", v, WEIGHTS["vix_regime"],
                     f"VIX {level:.1f} ({(chg or 0)*100:+.0f}%)")


def rates(tnx_chg: float | None) -> Component | None:
    # ^TNX is quoted in percent (e.g. 4.54); Δ0.10 = 10bp = full signal.
    if tnx_chg is None:
        return None
    v = _clip(-tnx_chg / 0.10)                  # +10bp yield day = full −1
    return Component("rates", v, WEIGHTS["rates"],
                     f"미 10년물 {tnx_chg*100:+.0f}bp")


def mean_reversion(ret1: float, vol20: float) -> Component:
    """Fade only a statistically extreme prior day; silent otherwise."""
    z = ret1 / max(vol20, 1e-6)
    if abs(z) <= 2.0:
        return Component("mean_reversion", 0.0, WEIGHTS["mean_reversion"],
                         "전일 정상 범위(되돌림 신호 없음)")
    v = _clip(-math.copysign(min(1.0, (abs(z) - 2.0) / 2.0), z))
    return Component("mean_reversion", v, WEIGHTS["mean_reversion"],
                     f"전일 {ret1*100:+.1f}% ({z:+.1f}σ) 과열 → 되돌림")


def futures(futures_ret: float | None) -> Component | None:
    """Live-only: NQ futures change since prior settle (absent in backtests)."""
    if futures_ret is None:
        return None
    v = _clip(math.tanh(futures_ret / 0.005))   # ±0.5% NQ = strong
    return Component("futures", v, WEIGHTS["futures"],
                     f"나스닥 선물 {futures_ret*100:+.2f}% (라이브 전용)")


def compute_signal(ctx: dict, mult: dict[str, float] | None = None) -> dict:
    """→ {score, conviction, components:[Component]}. The score is the weighted
    vote re-aggregated under `mult` (the active champion config; {} = base
    weights, which reproduces the plain renormalized average)."""
    from .variants import score_variant

    und = ctx.get("underlying", "기초지수")
    comps = [
        c for c in (
            asia_lead(ctx.get("asia") or {}),
            trend(ctx["und_sma50_dist"], und),
            momentum_5d(ctx["und_ret5"], ctx["und_vol20"], und),
            vix_regime(ctx.get("vix_level"), ctx.get("vix_chg")),
            rates(ctx.get("tnx_chg")),
            mean_reversion(ctx["und_ret1"], ctx["und_vol20"]),
            futures(ctx.get("futures_ret")),
        )
        if c is not None
    ]
    score = score_variant(comps, mult or {})
    return {"score": score, "conviction": abs(score), "components": comps}
