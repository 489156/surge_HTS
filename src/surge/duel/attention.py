"""Attention-layer collectors (AMVF Lead-Attention / NGRF news-sentiment).

The frameworks' top layer is *market attention* — news volume + sentiment around
the value-chain leader. The free news APIs lack deep history, so unlike the price
factors these candidate factors **accumulate FORWARD only** (recorded each
session, judged by the same gate once n ≥ 30) — there is no instant 2y backfill.

Economical by design: ONE leader ticker per pair, one API call per pair per night
(Alpha Vantage NEWS_SENTIMENT gives both sentiment AND article buzz in a single
call), so it stays inside the free tiers. Absent key ⇒ silent (factor not
recorded). Never touches the live decision — shadow-only, like every candidate.
"""

from __future__ import annotations

import math

import httpx
from loguru import logger

from ..config import settings
from ..db import connect, upsert, utc_now

# pair → the value-chain leader whose attention proxies the sector's
LEADERS = {"soxl_soxs": "NVDA", "tqqq_sqqq": "NVDA",
           "tecl_tecs": "NVDA", "labu_labd": "VRTX"}


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _na(x) -> bool:
    return x is None or x != x


def us_attention(ticker: str) -> dict | None:
    """{sentiment: relevance-weighted ticker sentiment [-~0.35,0.35], buzz: article
    count} from Alpha Vantage NEWS_SENTIMENT (1 call). None on missing key / rate
    limit / no coverage."""
    key = settings.alphavantage_api_key
    if not key:
        return None
    try:
        r = httpx.get("https://www.alphavantage.co/query", timeout=10.0,
                      params={"function": "NEWS_SENTIMENT", "tickers": ticker,
                              "limit": "50", "sort": "LATEST", "apikey": key})
        feed = (r.json() or {}).get("feed") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("AV attention {} failed: {}", ticker, exc)
        return None
    num = den = 0.0
    n = 0
    for art in feed:
        for ts in art.get("ticker_sentiment", []):
            if ts.get("ticker") == ticker:
                try:
                    rel = float(ts.get("relevance_score", 0))
                    sc = float(ts.get("ticker_sentiment_score", 0))
                except (TypeError, ValueError):
                    continue
                num += rel * sc
                den += rel
                n += 1
    if not n:
        return None
    return {"sentiment": (num / den) if den else 0.0, "buzz": n}


# ── attention candidate factors (read an attention dict) ──────────────────────
def _att_sentiment(a: dict) -> float | None:
    """NGRF news sentiment around the leader → direction."""
    s = a.get("sentiment")
    return None if _na(s) else _clip(math.tanh(s / 0.15))


def _att_news_thrust(a: dict) -> float | None:
    """AMVF lead-attention: sentiment DIRECTION confirmed by news volume (buzz).
    A sentiment tilt on a flood of coverage is a stronger read than on a trickle."""
    s, bz = a.get("sentiment"), a.get("buzz")
    if _na(s) or _na(bz):
        return None
    return _clip((1.0 if s > 0 else -1.0) * max(0.0, min(1.0, (bz - 10) / 40)))


ATTENTION_FACTORS = {"att_sentiment": _att_sentiment,
                     "att_news_thrust": _att_news_thrust}

# leader → (expiry_ts, attention) — dedupes the per-pair calls within one nightly
# run (NVDA leads 3 of 4 pairs; Alpha Vantage's free tier is only 25 calls/day).
_memo: dict[str, tuple[float, dict]] = {}


def _cached_attention(leader: str, ttl: int = 1800) -> dict | None:
    import time

    now = time.time()
    hit = _memo.get(leader)
    if hit and hit[0] > now:
        return hit[1]
    a = us_attention(leader)
    if a is not None:                       # don't cache misses → transient retry
        _memo[leader] = (now + ttl, a)
    return a


def record_attention(pair: dict, date: str) -> int:
    """Fetch the pair leader's attention and persist its candidate factors for
    (pair, date). Best-effort & idempotent; returns rows written (0 if no key /
    no coverage). Scored forward by the shared factors.score_pending."""
    leader = LEADERS.get(pair["id"])
    if not leader:
        return 0
    a = _cached_attention(leader)
    if not a:
        return 0
    now = utc_now()
    rows = []
    for name, fn in ATTENTION_FACTORS.items():
        try:
            v = fn(a)
        except Exception:  # noqa: BLE001
            v = None
        if v is None:
            continue
        rows.append({"factor": name, "pair": pair["id"], "decision_date": date,
                     "value": round(v, 4), "captured_at": now})
    if rows:
        with connect() as conn:
            upsert(conn, "duel_factor_shadow", rows, immutable=("captured_at",))
    return len(rows)
