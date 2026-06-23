"""Market-affecting legislation registry — the 'Sovereign Capital Layer'.

Design stance (deliberate, and the whole point): a law is a Layer-1 EVENT, not a
buy signal. DeepSeek proposed weighting it 30% of the score; ChatGPT correctly
warned "정책 수혜 ≠ 주가 수혜" (policy beneficiary ≠ price beneficiary) and that the
move only comes once smart-money + volume CONFIRM. This system agrees with the
discipline, so a policy is NOT a score booster. It is:
  1. a descriptive TAG on affected tickers (so the user sees the narrative), and
  2. a forward-validated CANDIDATE FACTOR (`policy_tagged`) — does a policy
     beneficiary actually hit +10%/T+5 more than the pool, since the law emerged?
     The gate answers with data instead of assuming. (Adding new legislation =
     append one entry here; the design 'reviews' it automatically via the gate.)

`tag_from` = the date the law became a confirmed market factor (passage), so the
backfill only tags sessions on/after it — no hindsight.
"""

from __future__ import annotations

# Each event: tickers are KRX 6-digit codes. `beneficiaries[code]` carries the
# qualitative read (theme / tier / reason) used for the descriptive tag only.
POLICY_EVENTS: list[dict] = [
    {
        "id": "kr_us_strategic_investment_act",
        "name": "대미투자특별법 (한·미 전략적투자 운영·관리 특별법)",
        "passed": "2026-03", "effective": "2026-06", "tag_from": "2026-03-01",
        "scale": "USD 3,500억 (반도체·AI·배터리·조선·원전·방산·핵심광물·전력)",
        "themes": ["반도체/HBM", "조선", "전력인프라", "방산/우주", "원전"],
        "beneficiaries": {
            # ── rotation/watch universe (scored by the policy_tagged factor) ──
            "000660": {"theme": "반도체/HBM", "tier": "S", "reason": "미국투자+CHIPS+HBM"},
            "095340": {"theme": "반도체/HBM", "tier": "S", "reason": "HBM+ASIC 테스트 교차점"},
            "357780": {"theme": "반도체/HBM", "tier": "S", "reason": "HBM 소재"},
            "089030": {"theme": "반도체/HBM", "tier": "A", "reason": "HBM 테스트 장비"},
            "042700": {"theme": "반도체/HBM", "tier": "A", "reason": "HBM 패키징"},
            "440110": {"theme": "AI 스토리지", "tier": "관찰", "reason": "SSD 컨트롤러(실적 확인 필요)"},
            "012450": {"theme": "방산/우주", "tier": "A", "reason": "방산 클러스터+수출"},
            "047810": {"theme": "방산/우주", "tier": "관찰", "reason": "내러티브>플로우"},
            "079550": {"theme": "방산", "tier": "A-", "reason": "정밀유도무기"},
            "034020": {"theme": "원전/전력", "tier": "A", "reason": "SMR·원전 자본흐름"},
            "000500": {"theme": "전력인프라", "tier": "A", "reason": "전력 공통분모"},
            # ── tagged for reference (not in the rotation chain universe) ──
            "042660": {"theme": "조선", "tier": "A", "reason": "미국 조선소 MRO"},
            "329180": {"theme": "조선", "tier": "A", "reason": "미국 조선소"},
            "010140": {"theme": "조선", "tier": "A", "reason": "미국 조선소"},
            "010120": {"theme": "전력인프라", "tier": "A", "reason": "전력 공통분모(LS ELECTRIC)"},
            "298040": {"theme": "전력인프라", "tier": "A", "reason": "중전기(효성중공업)"},
        },
    },
]


def beneficiary(ticker: str, asof: str | None = None) -> dict | None:
    """Policy tag for a ticker as of `asof` (ISO date) — None if not a beneficiary
    of any law yet in force. Returns {event, name, theme, tier, reason, tag_from}."""
    for ev in POLICY_EVENTS:
        if asof is not None and asof < ev["tag_from"]:
            continue
        b = ev["beneficiaries"].get(ticker)
        if b:
            return {"event": ev["id"], "name": ev["name"], "tag_from": ev["tag_from"],
                    **b}
    return None


def tagged_tickers(asof: str | None = None) -> dict[str, dict]:
    """All currently-tagged tickers → their tag info (for the screen/dashboard)."""
    out: dict[str, dict] = {}
    for ev in POLICY_EVENTS:
        if asof is not None and asof < ev["tag_from"]:
            continue
        for code, b in ev["beneficiaries"].items():
            out.setdefault(code, {"event": ev["id"], "name": ev["name"],
                                  "tag_from": ev["tag_from"], **b})
    return out
