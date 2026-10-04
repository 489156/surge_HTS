"""Meta-Labeling Signal Quality Filter & Kelly Sizing Engine (inspired by Microsoft Qlib & Marcos Lopez de Prado).

A secondary machine-learning/statistical filter that evaluates primary trade signals
to eliminate false breakouts (bull traps) before capital is committed.

Key Features:
1. Microstructure Win Probability Estimation (regime, RVOL, gap, float, trap flags)
2. Expected Value (EV) Gate: strictly rejects negative-EV setups
3. Half-Kelly Bet Sizing: dynamically scales capital allocation to maximize compounding
   while curbing drawdown.
"""

from __future__ import annotations

import math
from typing import Any
from loguru import logger

from .models import MacroRegime


class MetaLabelingFilter:
    """Evaluates whether a proposed directional setup possesses positive expectancy."""

    def __init__(
        self,
        min_win_prob: float = 0.52,
        half_kelly: float = 0.5,
        default_target_pct: float = 0.15,
        default_stop_pct: float = 0.07,
    ):
        self.min_win_prob = min_win_prob
        self.half_kelly = half_kelly
        self.default_target_pct = default_target_pct
        self.default_stop_pct = default_stop_pct

    def estimate_win_probability(self, ctx: dict, base_score: float) -> tuple[float, list[str]]:
        """Calibrates historical probability of hitting target before stop-loss.

        Returns (win_probability, positive_or_negative_factors).
        """
        # Base prior from committee/technical score (score 50 -> 50%, score 70 -> 58%)
        prob = 0.50 + (base_score - 50.0) * 0.004
        factors: list[str] = []

        snap = ctx.get("snapshot") or {}
        trap = ctx.get("trap") or {}
        macro = ctx.get("macro_regime", MacroRegime.NEUTRAL)

        # 1. Macro Regime Adjustment
        if macro == MacroRegime.RISK_ON:
            prob += 0.05
            factors.append("거시 Risk-On 환경(+5%p)")
        elif macro == MacroRegime.RISK_OFF:
            prob -= 0.08
            factors.append("거시 Risk-Off(VIX 고변동성, -8%p)")

        # 2. Volume & Liquidity Microstructure
        rvol = snap.get("rvol") or 1.0
        if rvol >= 3.0:
            prob += 0.06
            factors.append(f"강력한 거래량 수급 뒷받침(RVOL {rvol:.1f}x, +6%p)")
        elif rvol < 1.2:
            prob -= 0.05
            factors.append(f"거래량 부족(RVOL {rvol:.1f}x, 휩쏘 위험 -5%p)")

        # 3. Gap Fatigue & Exhaustion
        pct_chg = snap.get("pct_change") or 0.0
        if pct_chg >= 50.0:
            prob -= 0.07
            factors.append(f"과열 갭(+{pct_chg:.0f}%, 차익실현 출회 위험 -7%p)")

        # 4. Dilution & Trap Overhang (Hard negative penalization)
        if trap.get("pending_offering"):
            prob -= 0.15
            factors.append("유상증자 공시 오버행(-15%p)")
        if trap.get("exhausted"):
            prob -= 0.10
            factors.append("연속 상승 후 에너지 고갈(-10%p)")

        # Bound probability strictly between 0.05 and 0.95
        prob = max(0.05, min(0.95, prob))
        return prob, factors

    def evaluate(
        self,
        symbol: str,
        base_score: float,
        ctx: dict,
        target_pct: float | None = None,
        stop_pct: float | None = None,
    ) -> dict[str, Any]:
        """Adjudicates the trade signal and calculates Kelly-optimal bet sizing.

        Returns {pass_filter, win_prob, ev, kelly_size, reason}.
        """
        tgt = target_pct or self.default_target_pct
        stp = stop_pct or self.default_stop_pct
        payoff_b = tgt / stp if stp > 0 else 2.0

        win_prob, factors = self.estimate_win_probability(ctx, base_score)

        # Expected Value = p * target - (1 - p) * stop
        ev = win_prob * tgt - (1.0 - win_prob) * stp

        # Full Kelly f* = (p * b - (1 - p)) / b
        # Half-Kelly for robust downside protection
        raw_kelly = (win_prob * payoff_b - (1.0 - win_prob)) / payoff_b
        kelly_fraction = max(0.0, min(1.0, raw_kelly * self.half_kelly))

        passed = (win_prob >= self.min_win_prob) and (ev > 0.0)

        if not passed:
            reason = (
                f"메타필터 거부: 기대승률 {win_prob:.1%} < {self.min_win_prob:.0%} "
                f"또는 EV({ev*100:+.2f}%) 음수 — 가짜 돌파(False Breakout) 방지"
            )
            return {
                "symbol": symbol,
                "passed": False,
                "win_prob": round(win_prob, 3),
                "ev": round(ev, 4),
                "kelly_size": 0.0,
                "reason": reason,
                "factors": factors,
            }

        reason = (
            f"메타필터 승인: 기대승률 {win_prob:.1%}, EV {ev*100:+.2f}%, "
            f"켈리 권장비중 {kelly_fraction:.1%}"
        )
        return {
            "symbol": symbol,
            "passed": True,
            "win_prob": round(win_prob, 3),
            "ev": round(ev, 4),
            "kelly_size": round(kelly_fraction, 2),
            "reason": reason,
            "factors": factors,
        }
