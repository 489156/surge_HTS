"""Leveraged/inverse pair registry — the duel engine's tradable universe.

Curated to index-underlying pairs by default (clean label = the underlying's
open→close sign; single-stock 2x products are USUALLY excluded — idiosyncratic
news dominates and the inverse legs are often illiquid). All index legs are
ultra-liquid Direxion/ProShares 3x products.

Note on signals: the Asia semiconductor lead is strongest for SOXX and is a
general US-tech-beta proxy for the others — per-pair ICs in `duel-backtest`
show how much it actually carries for each underlying.

## Single-stock exception: NVDL/NVD (2026-07-03)

A researched exception to the single-stock exclusion. Screened against COIN
(CONL/CONI — 161x long/short liquidity gap), MSTR (MSTX/SMST — 27x gap),
PLTR (PLTU/PLTD — asymmetric 2x/1x leverage, not a matched pair), and TSLA
(TSLL/TSLQ — mismatched ISSUERS, Direxion vs Tradr). NVDL/NVD alone cleared
every bar: same issuer (GraniteShares), matched 2x/2x, ~3+ years of history
each, and the SHORT leg's volume (~64M sh/day) actually EXCEEDS the long
leg's (~14M sh/day) — the opposite of every other candidate's failure mode.
It also is not an orphan signal-wise: NVDA is already the `leader` ticker in
three existing baskets (soxl_soxs/tqqq_sqqq/tecl_tecs — see baskets.py), so
the asia_lead/AMVF machinery has a genuine (if unproven) causal story here,
unlike labu_labd's documented signal-transfer failure. No basket entry is
registered for this pair — NVDA IS the underlying, so "leadership vs its own
basket" would be circular; it races on the same signal set as every other
pair instead.
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
    # ── single-stock exception (see module docstring for the screening that
    # justified it): NVDA is both leg AND already the existing baskets' leader ──
    "nvdl_nvd": {
        "id": "nvdl_nvd", "bull": "NVDL", "bear": "NVD",
        "underlying": "NVDA", "name": "엔비디아 2x (단일종목 예외)",
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
