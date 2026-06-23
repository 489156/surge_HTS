"""Independent analysis agents. Each is deterministic and rule-based (an LLM
may *augment* the news agent when a key is present, but never decides), and each
returns the same structured, auditable contract: score, confidence,
recommendation, reasoning.

Context dict (`ctx`) per symbol carries: snapshot (daily_snapshot row),
trap (trap_flags row), catalysts (list), macro_regime, portfolio_status.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .. import scoring
from ..config import settings
from . import llm
from .models import AgentOpinion, MacroRegime, Recommendation, RiskStatus


def _rec(score: float) -> Recommendation:
    if score >= 60:
        return Recommendation.BUY
    if score <= 40:
        return Recommendation.SELL
    return Recommendation.HOLD


class Agent(ABC):
    name: str = "agent"

    @abstractmethod
    def evaluate(self, symbol: str, ctx: dict) -> AgentOpinion: ...

    def _op(self, symbol: str, score: float, confidence: float,
            reasoning: str) -> AgentOpinion:
        score = max(0.0, min(100.0, score))
        confidence = max(0.0, min(100.0, confidence))
        return AgentOpinion(agent=self.name, ticker=symbol, score=round(score, 1),
                            confidence=round(confidence, 1),
                            recommendation=_rec(score), reasoning=reasoning)


class TechnicalAgent(Agent):
    """Reuses surge's transparent setup score (the ignition pre-conditions)."""
    name = "technical_agent"

    def evaluate(self, symbol: str, ctx: dict) -> AgentOpinion:
        snap = ctx.get("snapshot") or {}
        trap = ctx.get("trap") or {}
        if not snap:
            return self._op(symbol, 50, 10, "no price data")
        setup, reasons = scoring.setup_score(snap, trap)
        score = 50 + setup * 5          # setup ~0..12 → 50..110 (capped)
        has_struct = bool(snap.get("shares_float")) and bool(snap.get("rvol"))
        confidence = 70 if has_struct else 40
        why = "; ".join(reasons[:4]) or "neutral technicals"
        return self._op(symbol, score, confidence, f"setup={setup}: {why}")


class NewsAgent(Agent):
    """Rule-based from SEC catalysts/offerings, optionally AUGMENTED by an LLM
    that classifies real headlines (RAG). The LLM never decides: dilution stays
    a hard bearish override, and with no API key the behavior is identical to the
    pure rule-based path."""
    name = "news_agent"

    def evaluate(self, symbol: str, ctx: dict) -> AgentOpinion:
        trap = ctx.get("trap") or {}
        catalysts = ctx.get("catalysts") or []
        # Hard fact: a pending dilutive offering is bearish regardless of news.
        if trap.get("pending_offering"):
            return self._op(symbol, 25, 70,
                            "recent dilutive filing (S-1/S-3/424B) — supply overhang")

        # Optional LLM augmentation (only when a key is configured).
        if settings.anthropic_api_key:
            headlines = ctx.get("headlines") or llm.fetch_headlines(symbol)
            res = llm.analyze_news(symbol, headlines)
            if res:
                score = 50 + res["sentiment"] * 30          # -1..1 → 20..80
                confidence = 40 + res["impact"] * 40        # 0..1 → 40..80
                return self._op(symbol, score, confidence,
                                f"LLM news: {res['summary']}")

        # Rule-based fallback.
        offerings = [c for c in catalysts if c.get("event_type") == "offering"]
        earnings = [c for c in catalysts if c.get("event_type") == "earnings"]
        if offerings:
            return self._op(symbol, 45, 45,
                            f"{len(offerings)} historical offering filings on record")
        if earnings:
            return self._op(symbol, 52, 40, "earnings event scheduled — event risk")
        return self._op(symbol, 50, 30, "no material news signal")


class FundamentalAgent(Agent):
    """Honest about thin data: micro-caps are speculative, so quality score is
    capped and confidence is low unless real fundamentals are present."""
    name = "fundamental_agent"

    def evaluate(self, symbol: str, ctx: dict) -> AgentOpinion:
        snap = ctx.get("snapshot") or {}
        mc = snap.get("market_cap")
        if not mc:
            return self._op(symbol, 50, 15, "no fundamentals available")
        if mc < 50_000_000:
            return self._op(symbol, 42, 35,
                            f"nano-cap ${mc/1e6:.0f}M — speculative, weak quality")
        if mc < 300_000_000:
            return self._op(symbol, 48, 35, f"micro-cap ${mc/1e6:.0f}M")
        return self._op(symbol, 55, 40, f"small-cap ${mc/1e6:.0f}M")


class MacroAgent(Agent):
    """Applies the market-wide regime (computed once per cycle) to every name."""
    name = "macro_agent"

    def evaluate(self, symbol: str, ctx: dict) -> AgentOpinion:
        regime = ctx.get("macro_regime", MacroRegime.NEUTRAL)
        table = {
            MacroRegime.RISK_ON: (62, "risk-on: low VIX, speculation supported"),
            MacroRegime.RISK_OFF: (35, "risk-off: elevated VIX, avoid small-caps"),
            MacroRegime.NEUTRAL: (50, "neutral macro regime"),
        }
        score, why = table[regime]
        return self._op(symbol, score, 55, why)


class RiskAgent(Agent):
    """Portfolio-level view. Highest priority — when risk state is stressed it
    pushes HOLD/SELL with high confidence regardless of the setup."""
    name = "risk_agent"

    def evaluate(self, symbol: str, ctx: dict) -> AgentOpinion:
        status = ctx.get("portfolio_status", RiskStatus.OK)
        trap = ctx.get("trap") or {}
        if status in (RiskStatus.HALT, RiskStatus.LIQUIDATE):
            return self._op(symbol, 10, 95, f"risk status {status.value}: no new risk")
        if status == RiskStatus.HALT_NEW:
            return self._op(symbol, 30, 90, "daily loss limit: new entries halted")
        if trap.get("illiquid"):
            return self._op(symbol, 35, 70, "illiquid — execution/exit risk")
        if trap.get("exhausted"):
            return self._op(symbol, 38, 65, "extended run — adverse risk/reward")
        if status == RiskStatus.WARN:
            return self._op(symbol, 45, 60, "drawdown warning — reduce sizing")
        return self._op(symbol, 55, 50, "risk headroom available")


DEFAULT_AGENTS: list[Agent] = [
    TechnicalAgent(), NewsAgent(), FundamentalAgent(), MacroAgent(), RiskAgent(),
]

# Confidence-weighting emphasis per agent (risk gets the final say elsewhere).
AGENT_WEIGHTS = {
    "technical_agent": 1.3,
    "news_agent": 1.0,
    "fundamental_agent": 0.7,
    "macro_agent": 1.0,
    "risk_agent": 1.5,
}


def compute_macro_regime() -> MacroRegime:
    """VIX-based regime, computed once per cycle. Degrades to NEUTRAL offline."""
    try:
        import yfinance as yf

        vix = yf.Ticker("^VIX").history(period="5d")
        if len(vix):
            v = float(vix["Close"].iloc[-1])
            if v < 18:
                return MacroRegime.RISK_ON
            if v > 28:
                return MacroRegime.RISK_OFF
    except Exception:  # noqa: BLE001
        pass
    return MacroRegime.NEUTRAL
