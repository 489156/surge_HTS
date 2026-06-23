"""Value-chain graph (seed). Each chain is an ORDERED list of nodes; interest
rotates front→back. Seeded from the user's 6 in-sample hits + the query names.
Hand-curated; extend as themes evolve. Tickers are KRX codes (6-digit)."""

from __future__ import annotations

CHAINS: dict[str, dict] = {
    "ai_memory_hbm": {
        "name": "AI 메모리(HBM) 가치사슬",
        "nodes": [
            ("foundry", "HBM 제조", [("000660", "SK하이닉스"), ("005930", "삼성전자")]),
            ("packaging", "후공정/패키징", [("042700", "한미반도체")]),
            ("equipment", "장비", [("240810", "원익IPS")]),
            ("test", "테스트", [("095340", "ISC"), ("089030", "테크윙")]),
            ("materials", "소재", [("357780", "솔브레인"), ("014680", "한솔케미칼")]),
            ("power", "전력/케이블", [("000500", "가온전선"), ("034020", "두산에너빌리티")]),
        ],
    },
    "ai_substrate": {
        "name": "AI 기판/부품",
        "nodes": [
            ("driver", "AI 가속기", [("000660", "SK하이닉스")]),
            ("substrate", "기판", [("011070", "LG이노텍")]),
            ("fabless", "팹리스", [("440110", "파두")]),
        ],
    },
    "robotics": {
        "name": "AI 로봇",
        "nodes": [
            ("narrative", "AI 내러티브", [("005930", "삼성전자")]),
            ("robot", "로봇 본체", [("454910", "두산로보틱스")]),
            ("parts", "부품/감속기", [("108490", "로보티즈")]),
        ],
    },
    "space_defense": {
        "name": "우주/방산",
        "nodes": [
            ("event", "글로벌 우주(SpaceX)", [("047810", "KAI")]),
            ("airframe", "발사체/항공", [("047810", "KAI"), ("012450", "한화에어로스페이스")]),
            ("satellite", "위성/통신", [("272210", "한화시스템")]),
            ("defense", "방산", [("079550", "LIG넥스원")]),
        ],
    },
}

# query / coverage universe (KR names the analysis must consider)
EXTRA_TICKERS = {
    "000270": "기아", "140410": "메지온", "086520": "펩트론",
    "034020": "두산에너빌리티", "440110": "파두",
}


def ticker_index() -> dict[str, dict]:
    """ticker → {name, chain, node, order, n_nodes}. A ticker can appear in
    multiple chains; the first wins for the primary mapping."""
    idx: dict[str, dict] = {}
    for cid, c in CHAINS.items():
        for order, (node, _label, tickers) in enumerate(c["nodes"]):
            for code, name in tickers:
                idx.setdefault(code, {"name": name, "chain": cid, "node": node,
                                      "order": order, "n_nodes": len(c["nodes"])})
    for code, name in EXTRA_TICKERS.items():
        idx.setdefault(code, {"name": name, "chain": None, "node": None,
                              "order": None, "n_nodes": None})
    return idx


def universe() -> list[str]:
    return list(ticker_index().keys())


def chain_tickers(chain_id: str) -> list[tuple[int, str, str]]:
    """(order, ticker, name) for a chain, front→back."""
    out = []
    for order, (_node, _label, tickers) in enumerate(CHAINS[chain_id]["nodes"]):
        for code, name in tickers:
            out.append((order, code, name))
    return out
