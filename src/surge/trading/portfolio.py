"""Portfolio manager — aggregates agent opinions + the debate into a single,
auditable decision (action, size, stop, take-profit). It proposes; the risk
engine disposes (final sizing/veto happens in the execution engine)."""

from __future__ import annotations

from ..config import settings
from .models import Action, AgentOpinion, Decision, TradingMode


class PortfolioManager:
    def decide(
        self,
        symbol: str,
        opinions: list[AgentOpinion],
        debate: dict,
        ref_price: float,
        mode: TradingMode,
    ) -> Decision:
        action = Action(debate["action"])
        size_pct = settings.max_position_pct * debate["size_factor"]
        stop = round(ref_price * (1 - settings.default_stop_pct), 4)
        target = round(ref_price * (1 + settings.default_target_pct), 4)
        expected_risk = round(size_pct * settings.default_stop_pct, 4)

        rationale = {
            "net_score": debate["net_score"],
            "judge": debate["judge"],
            "bull": debate["bull"],
            "bear": debate["bear"],
            "agents": [
                {"agent": o.agent, "score": o.score, "confidence": o.confidence,
                 "rec": o.recommendation.value, "why": o.reasoning}
                for o in opinions
            ],
        }
        return Decision(
            mode=mode, symbol=symbol, action=action,
            final_score=debate["net_score"], confidence=debate["confidence"],
            size_pct=round(size_pct, 4), stop_price=stop, target_price=target,
            expected_risk=expected_risk, rationale=rationale,
        )
