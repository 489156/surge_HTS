"""Debate layer — bull vs bear, adjudicated by a judge. Operates on the agents'
structured opinions (no free-form LLM), so the outcome is reproducible. The
risk agent holds a hard veto: a confident risk objection forces HOLD regardless
of how bullish the rest are."""

from __future__ import annotations

from .agents import AGENT_WEIGHTS
from .committee import COMMITTEE_WEIGHTS
from .metafilter import MetaLabelingFilter
from .models import AgentOpinion, Recommendation

ALL_WEIGHTS = {**AGENT_WEIGHTS, **COMMITTEE_WEIGHTS}


def run_debate(opinions: list[AgentOpinion], ctx: dict | None = None) -> dict:
    if not opinions:
        return {"net_score": 50.0, "confidence": 0.0, "action": "HOLD",
                "size_factor": 0.0, "bull": [], "bear": [], "judge": "no opinions"}

    num = den = 0.0
    for op in opinions:
        w = ALL_WEIGHTS.get(op.agent, 1.0) * (op.confidence / 100.0)
        num += op.score * w
        den += w
    net = num / den if den else 50.0
    confidence = sum(op.confidence for op in opinions) / len(opinions)

    bull = [f"{op.agent}: {op.reasoning}" for op in opinions
            if op.recommendation == Recommendation.BUY]
    bear = [f"{op.agent}: {op.reasoning}" for op in opinions
            if op.recommendation == Recommendation.SELL]

    # Risk veto: a confident risk/forensic objection overrides the debate.
    risk_veto = any(
        op.agent in ("risk_agent", "burry_short_agent")
        and op.recommendation == Recommendation.SELL
        and op.confidence >= 80
        for op in opinions
    )

    if risk_veto:
        action, size_factor, judge = "HOLD", 0.0, "risk/forensic veto — stand down"
    elif net >= 55:
        action = "BUY"
        base_size = 1.0 if net >= 65 else 0.5
        judge = f"net {net:.0f} {'≥ 65' if net >= 65 else 'in 55–65'} — committee buy"

        if ctx:
            symbol = ctx.get("symbol", opinions[0].ticker if opinions else "UNKNOWN")
            meta = MetaLabelingFilter().evaluate(symbol, net, ctx)
            if not meta["passed"]:
                action, size_factor, judge = "HOLD", 0.0, f"meta-filter veto: {meta['reason']}"
            else:
                size_factor = min(base_size, max(0.2, meta["kelly_size"] or base_size))
                judge = f"{judge} | {meta['reason']}"
        else:
            size_factor = base_size
    elif net <= 35:
        action, size_factor, judge = "SELL", 0.0, f"net {net:.0f} ≤ 35 — exit/avoid"
    else:
        action, size_factor, judge = "HOLD", 0.0, f"net {net:.0f} — no edge, hold"

    return {
        "net_score": round(net, 1),
        "confidence": round(confidence, 1),
        "action": action,
        "size_factor": size_factor,
        "bull": bull,
        "bear": bear,
        "judge": judge,
    }
