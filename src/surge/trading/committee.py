"""Multi-Persona AI Investment Committee (inspired by virattt/ai-hedge-fund).

Decomposes investment appraisal into specialized legendary investor personas:
- Warren Buffett (Moat, ROE, balance-sheet safety)
- Cathie Wood (Exponential innovation, float turnover, explosive momentum)
- Michael Burry (Forensic short-risk, dilution traps, cash burn, fake gaps)
- Bill Ackman (High-conviction activist catalysts, turnaround triggers)
- Benjamin Graham (Deep value margin of safety, net-cash floor)

All personas operate on structured, auditable contracts (score, confidence,
recommendation, reasoning) for 100% reproducible execution.
"""

from __future__ import annotations

from .agents import Agent
from .models import AgentOpinion
from . import llm


class LLMPersonaAgent(Agent):
    persona_prompt: str = ""
    
    def evaluate(self, symbol: str, ctx: dict) -> AgentOpinion:
        # 1. Try LLM first if API key is set
        res = llm.analyze_persona(symbol, self.persona_prompt, ctx)
        if res:
            return self._op(symbol, res["score"], res["confidence"], f"[LLM] {res['reasoning']}")
        # 2. Fallback to rule-based logic
        return self.evaluate_rules(symbol, ctx)
        
    def evaluate_rules(self, symbol: str, ctx: dict) -> AgentOpinion:
        return self._op(symbol, 50, 10, "unimplemented rule fallback")



class BuffettQualityAgent(LLMPersonaAgent):
    """Quality & Moat evaluation: looks for positive operating margins, manageable debt,
    and business durability. Skeptical of pre-revenue or highly leveraged companies."""
    name = "buffett_agent"
    persona_prompt = (
        "You are Warren Buffett. Evaluate this stock for long-term compounding, economic moat, "
        "and margin of safety. Reject highly leveraged, purely speculative, or cash-burning companies. "
        "Look for durability and rational pricing."
    )

    def evaluate_rules(self, symbol: str, ctx: dict) -> AgentOpinion:
        snap = ctx.get("snapshot") or {}
        mc = snap.get("market_cap")
        trap = ctx.get("trap") or {}

        if not snap:
            return self._op(symbol, 45.0, 20.0, "펀더멘털 데이터 부재 — 투자 부적격(Moat 미확인)")

        # Buffett hates heavy dilution overhang and toxic debt
        if trap.get("pending_offering"):
            return self._op(symbol, 20.0, 85.0, "지속적 증자/희석 우려 — 자본잠식형 기업(버핏 비선호)")

        if mc and mc > 500_000_000:
            score = 65.0
            reasons = f"시총 ${mc/1e6:.0f}M 중대형급 — 상대적 사업 안정성 확보"
        elif mc and mc < 50_000_000:
            score = 35.0
            reasons = f"초소형 나노캡(${mc/1e6:.0f}M) — 경제적 해자(Moat) 부재, 투기적 특성"
        else:
            score = 48.0
            reasons = "중소형 밸류에이션 — 보통 수준의 품질"

        return self._op(symbol, score, 60.0, reasons)


class CathieMomentumAgent(LLMPersonaAgent):
    """Exponential Growth & Momentum: looks for explosive RVOL (>2.5), low float velocity,
    disruptive catalyst ignition, and parabolic potential."""
    name = "cathie_wood_agent"
    persona_prompt = (
        "You are Cathie Wood. Evaluate this stock for exponential disruptive innovation and momentum. "
        "Prioritize explosive relative volume (RVOL), massive catalysts, and high-beta momentum. "
        "You tolerate high volatility and risk if the upside potential is parabolic."
    )

    def evaluate_rules(self, symbol: str, ctx: dict) -> AgentOpinion:
        snap = ctx.get("snapshot") or {}
        rvol = snap.get("rvol") or 1.0
        pct_change = snap.get("pct_change") or 0.0
        shares_float = snap.get("shares_float")

        score = 50.0
        confidence = 50.0
        reasons = []

        if rvol >= 3.0:
            score += 25.0
            confidence += 20.0
            reasons.append(f"폭발적 거래량 점화(RVOL {rvol:.1f}x)")
        elif rvol >= 1.5:
            score += 12.0
            reasons.append(f"거래량 유입 확인(RVOL {rvol:.1f}x)")

        if shares_float and shares_float < 10_000_000:
            score += 15.0
            reasons.append(f"초소형 유통주식수({shares_float/1e6:.1f}M)로 폭발적 탄력성")

        if pct_change >= 20.0:
            score += 10.0
            reasons.append(f"당일 모멘텀 강세(+{pct_change:.0f}%)")

        why = " · ".join(reasons) if reasons else "모멘텀 유입 신호 미약"
        return self._op(symbol, score, confidence, why)


class BurryForensicRiskAgent(LLMPersonaAgent):
    """Forensic Risk & Dilution Detection: scans for toxic death-spiral financing,
    ATM offerings, reverse split fatigue, float rotation traps, and bubble exhaustion."""
    name = "burry_short_agent"
    persona_prompt = (
        "You are Michael Burry. You are a forensic skeptic looking for hidden risks, toxic dilution, "
        "pump-and-dump mechanics, and exhausted rallies. Veto heavily if you see S-1/S-3 filings, "
        "warrants, or irrational price spikes without fundamentals."
    )

    def evaluate_rules(self, symbol: str, ctx: dict) -> AgentOpinion:
        trap = ctx.get("trap") or {}
        snap = ctx.get("snapshot") or {}

        # Burry detects dilution and trap flags with ultra-high conviction
        red_flags = []
        severity = 0.0

        if trap.get("pending_offering"):
            red_flags.append("S-1/S-3 유상증자 유통물량 폭탄(Dilution Overhang)")
            severity += 40.0

        if trap.get("exhausted"):
            red_flags.append("다일 연속 급등 후 차익실현 피로도 극대화(Exhaustion)")
            severity += 25.0

        if trap.get("illiquid"):
            red_flags.append("유동성 부족 — 엑시트 불가 슬리피지 함정")
            severity += 20.0

        rvol = snap.get("rvol") or 1.0
        pct_chg = snap.get("pct_change") or 0.0
        if pct_chg > 100.0 and rvol < 2.0:
            red_flags.append("거래량 없는 억지 갭상승 — 전형적인 펌프앤덤프 함정")
            severity += 30.0

        if red_flags:
            score = max(10.0, 50.0 - severity)
            confidence = min(95.0, 55.0 + severity)
            return self._op(symbol, score, confidence, "⚠ " + " · ".join(red_flags))

        return self._op(symbol, 60.0, 45.0, "구조적 분식/희석 적신호 없음(숏 관점 리스크 통과)")


class AckmanCatalystAgent(LLMPersonaAgent):
    """Activist & Event-Driven Catalyst: evaluates decisive external triggers such as
    contracts, earnings surprises, regulatory approvals, or strategic partnerships."""
    name = "ackman_catalyst_agent"
    persona_prompt = (
        "You are Bill Ackman. Look for decisive catalyst events: new contracts, FDA approvals, "
        "earnings beats, or activist interventions that unlock immediate shareholder value. "
        "You want high-conviction event-driven plays."
    )

    def evaluate_rules(self, symbol: str, ctx: dict) -> AgentOpinion:
        catalysts = ctx.get("catalysts") or []
        earnings = [c for c in catalysts if c.get("event_type") == "earnings"]
        contracts = [c for c in catalysts if c.get("event_type") in ("contract", "fda", "partner")]

        if contracts:
            return self._op(symbol, 75.0, 75.0,
                            f"핵심 전략적 모멘텀 발동({len(contracts)}건 촉매 공시 확인)")
        if earnings:
            return self._op(symbol, 55.0, 50.0, "실적 발표 이벤트 대기 — 변동성 확대 구간")

        return self._op(symbol, 50.0, 35.0, "명확한 펀더멘털 촉매(Catalyst) 부재")


class GrahamSafetyAgent(LLMPersonaAgent):
    """Deep Value & Margin of Safety: assesses downside protection and tangible liquidation floor."""
    name = "graham_value_agent"
    persona_prompt = (
        "You are Benjamin Graham. Look for deep value, tangible assets, and a massive margin of safety. "
        "You dislike overvalued momentum stocks and prefer companies trading below their net current asset value."
    )

    def evaluate_rules(self, symbol: str, ctx: dict) -> AgentOpinion:
        snap = ctx.get("snapshot") or {}
        mc = snap.get("market_cap")

        if not mc or mc < 100_000_000:
            return self._op(symbol, 42.0, 45.0, "순유동자산 대비 안전마진(Margin of Safety) 불충분")

        return self._op(symbol, 58.0, 55.0, f"시총 ${mc/1e6:.0f}M — 기초 자산 안전마진 일부 확보")


# Extended Committee: Core Technical/News/Macro/Risk + Legend Personas
COMMITTEE_AGENTS: list[Agent] = [
    BuffettQualityAgent(),
    CathieMomentumAgent(),
    BurryForensicRiskAgent(),
    AckmanCatalystAgent(),
    GrahamSafetyAgent(),
]

# Committee Weights for synthesis
COMMITTEE_WEIGHTS: dict[str, float] = {
    "buffett_agent": 0.9,
    "cathie_wood_agent": 1.2,
    "burry_short_agent": 1.4,       # High weight on risk/dilution veto
    "ackman_catalyst_agent": 1.1,
    "graham_value_agent": 0.8,
}
