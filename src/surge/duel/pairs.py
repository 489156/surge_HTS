"""Leveraged/inverse pair registry — the duel engine's tradable universe.

Curated to index-underlying pairs only (clean label = the underlying's
open→close sign; single-stock 2x products are excluded: idiosyncratic news
dominates and the inverse legs are often illiquid). All legs are ultra-liquid
Direxion/ProShares 3x products.

Note on signals: the Asia semiconductor lead is strongest for SOXX and is a
general US-tech-beta proxy for the others — per-pair ICs in `duel-backtest`
show how much it actually carries for each underlying.
"""

from __future__ import annotations

PAIRS: dict[str, dict] = {
    "soxl_soxs": {
        "id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS",
        "underlying": "SOXX", "name": "반도체 3x (ICE Semiconductor)",
    },
    "tqqq_sqqq": {
        "id": "tqqq_sqqq", "bull": "TQQQ", "bear": "SQQQ",
        "underlying": "QQQ", "name": "나스닥100 3x",
    },
    "tecl_tecs": {
        "id": "tecl_tecs", "bull": "TECL", "bear": "TECS",
        "underlying": "XLK", "name": "기술 셀렉트 3x",
    },
    "labu_labd": {
        "id": "labu_labd", "bull": "LABU", "bear": "LABD",
        "underlying": "XBI", "name": "바이오텍 3x",
    },
    # ── diversifying legs (lift the daily sample rate with LESS-correlated draws:
    # small-caps + financials are not mega-tech, so they add independent evidence
    # toward signal verification rather than near-duplicate tech bets) ──
    "tna_tza": {
        "id": "tna_tza", "bull": "TNA", "bear": "TZA",
        "underlying": "IWM", "name": "러셀2000 소형주 3x",
    },
    "fas_faz": {
        "id": "fas_faz", "bull": "FAS", "bear": "FAZ",
        "underlying": "XLF", "name": "금융 3x",
    },
}

DEFAULT_PAIR = "soxl_soxs"


def get_pair(pair_id: str) -> dict:
    if pair_id not in PAIRS:
        raise KeyError(f"unknown pair '{pair_id}' (choices: {list(PAIRS)})")
    return PAIRS[pair_id]


def all_symbols() -> list[str]:
    """Every leg + underlying across the registry (for archiving)."""
    out: list[str] = []
    for p in PAIRS.values():
        for k in ("bull", "bear", "underlying"):
            if p[k] not in out:
                out.append(p[k])
    return out
